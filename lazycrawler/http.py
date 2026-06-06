# -*- coding: utf-8 -*-
"""
lazycrawler.http
================
HTTP client with retry/backoff + URL utilities (normalization, hashing,
domain blacklist).

Hash functions used by the 3-level dedup (see db.py):
  - url_hash(url)        -> sha256(normalize_url(url))   [level 1 dedup, URL]
  - content_hash(text)   -> sha256(normalize(text))      [level 2 dedup, content]
"""

from __future__ import annotations

import hashlib
import re
import time
from typing import List, Optional, Set, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

from .config import HTTPConfig


# =============================================================================
# URL CONSTANTS
# =============================================================================

_EXCLUDE_RE = re.compile(
    r"unsubscribe|manage.prefer|opt.out|privacy.polic|terms.of"
    r"|facebook\.com|twitter\.com|x\.com/|instagram\.com"
    r"|youtube\.com|substack\.com/subscribe|mailto:|tel:"
    r"|/login|/signin|/register|/signup|/cart|/checkout|/account"
    r"|/search\?|/tag/|/category/|/author/|/about|/contact",
    re.IGNORECASE,
)

_TRACKING_PARAMS: Set[str] = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_name", "utm_reader", "utm_viz_id",
    "gclid", "fbclid", "mc_cid", "mc_eid", "cmpid", "icid", "iid",
    "ref", "referrer", "source", "ns_campaign", "ns_mchannel", "ns_source",
}


# =============================================================================
# URL UTILITIES
# =============================================================================

def get_base_domain(url: str) -> str:
    """Lowercase domain (netloc) from a URL."""
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def get_hostname(url: str) -> str:
    """Lowercase hostname (no port) from a URL."""
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def strip_tracking_params(url: str) -> str:
    """Remove UTM/tracking params and sort the query for stability."""
    try:
        p = urlparse(url)
        q = [
            (k, v) for (k, v) in parse_qsl(p.query, keep_blank_values=True)
            if k.lower() not in _TRACKING_PARAMS
        ]
        q.sort(key=lambda kv: kv[0].lower())
        return urlunparse((p.scheme, p.netloc, p.path, p.params, urlencode(q, doseq=True), p.fragment))
    except Exception:
        return url


def normalize_url(url: str) -> str:
    """
    Normalize a URL: lowercase scheme/host, drop fragment, strip tracking,
    normalize the trailing slash of the path.
    """
    try:
        url = strip_tracking_params(url.strip())
        p = urlparse(url)
        path = p.path.rstrip("/") or "/"
        return urlunparse((p.scheme.lower(), p.netloc.lower(), path, p.params, p.query, ""))
    except Exception:
        return url.strip()


def is_excluded_url(url: str, text: str = "") -> bool:
    """True if the URL or anchor text matches the exclusion pattern."""
    if _EXCLUDE_RE.search(url):
        return True
    if text and _EXCLUDE_RE.search(text):
        return True
    return False


# =============================================================================
# HASHING (dedup)
# =============================================================================

def sha256_hex(s: str) -> str:
    """SHA256 hex of a UTF-8 string."""
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()


def url_hash(url: str) -> str:
    """Level-1 dedup key (URL): sha256(normalize_url(url))."""
    return sha256_hex(normalize_url(url))


def _normalize_for_hash(text: str) -> str:
    """Normalize whitespace for a stable content hash."""
    s = (text or "").strip()
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    return s


def content_hash(text: str) -> str:
    """Level-2 dedup key (content): sha256(normalize(text))."""
    return sha256_hex(_normalize_for_hash(text))


# =============================================================================
# DOMAIN BLACKLIST
# =============================================================================

def is_blacklisted_domain(url: str, blacklist: Optional[List[str]] = None) -> bool:
    """
    True if the URL host is in the blacklist (exact match or subdomain).
    E.g. "example.com" also blocks "www.example.com" and "news.example.com".
    """
    if not blacklist:
        return False
    host = get_hostname(url)
    if not host:
        return False
    blocked = {str(d).lower().strip().lstrip(".") for d in blacklist if d}
    return any(host == d or host.endswith(f".{d}") for d in blocked)


def load_blacklist_from_excel(
    excel_path: str,
    sheet_name: Optional[str] = None,
    column_name: Optional[str] = None,
) -> List[str]:
    """
    Load a list of domains from an .xlsx file.

    If column_name is None, look for a header among {domain, domains, blacklist,
    blacklisted_domain(s), blocked_domain(s)}, otherwise use the first column.
    Requires openpyxl (``pip install openpyxl``). Errors -> empty list.
    """
    try:
        from openpyxl import load_workbook
    except Exception:
        print("  [BLACKLIST] openpyxl not installed - Excel blacklist ignored")
        return []

    try:
        wb = load_workbook(excel_path, read_only=True, data_only=True)
    except Exception as e:
        print(f"  [BLACKLIST] Error opening Excel: {type(e).__name__}: {e}")
        return []

    ws = wb[sheet_name] if (sheet_name and sheet_name in wb.sheetnames) else wb[wb.sheetnames[0]]
    rows = ws.iter_rows(values_only=True)

    try:
        header = next(rows)
    except StopIteration:
        return []

    header_lower = [str(v).strip().lower() if v is not None else "" for v in header]
    target_idx = None
    if column_name:
        wanted = column_name.strip().lower()
        target_idx = next((i for i, n in enumerate(header_lower) if n == wanted), None)
        if target_idx is None:
            print(f"  [BLACKLIST] Column '{column_name}' not found - using first column")
            target_idx = 0
    else:
        candidates = {
            "domain", "domains", "blacklisted_domain", "blacklisted_domains",
            "blacklist", "blocked_domain", "blocked_domains",
        }
        target_idx = next((i for i, n in enumerate(header_lower) if n in candidates), 0)

    domains: List[str] = []
    seen = set()
    for row in rows:
        if row is None or target_idx >= len(row):
            continue
        value = row[target_idx]
        if value is None:
            continue
        domain = str(value).strip().lower().lstrip(".")
        if not domain:
            continue
        host = get_hostname(domain if "://" in domain else f"https://{domain}")
        domain = host or domain
        if domain not in seen:
            seen.add(domain)
            domains.append(domain)
    return domains


# =============================================================================
# HTML -> TEXT (basic fallback)
# =============================================================================

def html_to_text_basic(html: str) -> str:
    """Convert HTML to plain text via regex (fallback when trafilatura is absent)."""
    html = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", html)
    html = re.sub(r"(?is)<br\s*/?>", "\n", html)
    html = re.sub(r"(?is)</p\s*>", "\n\n", html)
    html = re.sub(r"(?is)<.*?>", "", html)
    html = (
        html.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
        .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'")
    )
    return html.strip()


# =============================================================================
# HTTP CLIENT
# =============================================================================

class HTTPClient:
    """
    HTTP client with exponential retry/backoff and text extraction via
    trafilatura (falls back to a basic HTML strip).
    """

    def __init__(self, cfg: Optional[HTTPConfig] = None):
        self.cfg = cfg or HTTPConfig()
        # verify=: path to the CA bundle if provided, otherwise the verify_ssl bool
        self._verify = self.cfg.ca_bundle or self.cfg.verify_ssl
        if self._verify is False:
            try:
                import urllib3
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            except Exception:
                pass
        self._session = self._make_session()

    def _make_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update({
            "User-Agent": self.cfg.user_agent,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
        })
        return s

    @property
    def session(self) -> requests.Session:
        return self._session

    def fetch(
        self,
        url: str,
        extra_headers: Optional[dict] = None,
    ) -> Tuple[Optional[str], Optional[str], Optional[int]]:
        """
        Fetch with retry. Returns (html, text, status_code).

        - html:        raw HTML (None if the fetch fails)
        - text:        text extracted via trafilatura / fallback (None if none)
        - status_code: HTTP status (None on network error)
        """
        cfg = self.cfg
        last_exc = None

        for attempt in range(1, cfg.max_retries + 1):
            try:
                headers = extra_headers or None
                resp = self._session.get(
                    url,
                    timeout=(cfg.timeout_connect, cfg.timeout_read),
                    allow_redirects=True,
                    headers=headers,
                    verify=self._verify,
                )
                status = resp.status_code
                if status in (429, 500, 502, 503, 504):
                    raise requests.HTTPError(f"HTTP {status}")
                resp.raise_for_status()
                html = resp.text or ""

                try:
                    import trafilatura  # type: ignore
                    content = trafilatura.extract(
                        html,
                        include_comments=False,
                        include_tables=False,
                        no_fallback=False,
                        favor_recall=True,
                    )
                    if content and len(content.strip()) > 200:
                        return html, content.strip(), status
                except ImportError:
                    pass
                except Exception:
                    pass

                plain = html_to_text_basic(html)
                if plain and len(plain) > 200:
                    return html, plain, status
                return html, None, status

            except Exception as e:
                last_exc = e
                if attempt < cfg.max_retries:
                    time.sleep(cfg.backoff_base_sec * (2 ** (attempt - 1)))
                else:
                    print(f"    [FETCH] {type(e).__name__}: {str(e)[:160]}")
                    return None, None, None

        print(f"    [FETCH] Failed: {last_exc}")
        return None, None, None

    def close(self) -> None:
        self._session.close()
