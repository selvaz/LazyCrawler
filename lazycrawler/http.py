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
import threading
import time
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import requests

from ._log import log
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
        log.warning("openpyxl not installed - Excel blacklist ignored "
                    "(pip install openpyxl)")
        return []

    try:
        wb = load_workbook(excel_path, read_only=True, data_only=True)
    except Exception as e:
        log.warning("error opening Excel blacklist %s: %s: %s",
                    excel_path, type(e).__name__, e)
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
            log.warning("blacklist column '%s' not found - using first column", column_name)
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
                log.debug("could not disable urllib3 InsecureRequestWarning", exc_info=True)
        self._session = self._make_session()
        self._browser = None

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

    @staticmethod
    def _extract_text(html: str) -> Optional[str]:
        """Extract main text via trafilatura, with a basic HTML-strip fallback."""
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
                log.debug("  text: trafilatura -> %d chars", len(content.strip()))
                return content.strip()
            log.debug("  text: trafilatura returned %s chars (<200) -> trying basic strip",
                      len(content.strip()) if content else 0)
        except ImportError:
            log.debug("  text: trafilatura not installed -> basic HTML strip "
                      "(pip install trafilatura for better extraction)")
        except Exception:
            log.debug("  text: trafilatura.extract failed -> basic HTML strip", exc_info=True)
        plain = html_to_text_basic(html)
        if plain and len(plain) > 200:
            log.debug("  text: basic HTML strip (fallback) -> %d chars", len(plain))
            return plain
        log.debug("  text: no extractable content (<200 chars from both trafilatura and basic strip)")
        return None

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

        If cfg.render_js is True, the HTML is obtained from a headless browser
        (Playwright); on browser failure/unavailability it falls back to requests.
        """
        cfg = self.cfg

        # JavaScript rendering path (opt-in).
        if cfg.render_js:
            html = self._browser_renderer().render(url)
            if html:
                return html, self._extract_text(html), 200
            # browser unavailable/failed -> fall through to requests

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
                return html, self._extract_text(html), status

            except Exception as e:
                last_exc = e
                if attempt < cfg.max_retries:
                    log.debug("fetch attempt %d/%d for %s failed: %s",
                              attempt, cfg.max_retries, url, e)
                    time.sleep(cfg.backoff_base_sec * (2 ** (attempt - 1)))
                else:
                    log.warning("fetch failed for %s after %d attempts: %s: %s",
                                url, cfg.max_retries, type(e).__name__, e)
                    return None, None, None

        log.warning("fetch failed for %s: %s", url, last_exc)
        return None, None, None

    def get_text(self, url: str) -> Optional[str]:
        """
        Fetch a URL and return the raw response text (no extraction), honoring
        the SSL/verify configuration. Used for robots.txt. None on any failure.
        """
        try:
            resp = self._session.get(
                url, timeout=(self.cfg.timeout_connect, self.cfg.timeout_read),
                allow_redirects=True, verify=self._verify,
            )
            if resp.status_code >= 400:
                return None
            return resp.text or ""
        except Exception as e:
            log.debug("get_text failed for %s: %s", url, e)
            return None

    def close(self) -> None:
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                log.debug("failed closing browser renderer", exc_info=True)
            self._browser = None
        self._session.close()

    def _browser_renderer(self):
        if self._browser is None:
            from .browser import BrowserRenderer
            cfg = self.cfg
            self._browser = BrowserRenderer(
                user_agent=cfg.user_agent,
                headless=cfg.browser_headless,
                wait_until=cfg.browser_wait_until,
                timeout_ms=cfg.browser_timeout_ms,
            )
        return self._browser


# =============================================================================
# ROBOTS.TXT
# =============================================================================

class RobotsChecker:
    """
    Thread-safe robots.txt gate. Fetches each host's robots.txt once (honoring
    the SSL config via HTTPClient) and answers can_fetch for the configured
    User-Agent. A missing/unreachable/unparseable robots.txt means "allow"
    (standard convention).
    """

    def __init__(self, http: HTTPClient, user_agent: str):
        self._http = http
        self._ua = user_agent or "*"
        self._cache: Dict[str, Optional[RobotFileParser]] = {}
        self._lock = threading.Lock()

    def allowed(self, url: str) -> bool:
        try:
            p = urlparse(url)
        except Exception:
            return True
        host = (p.netloc or "").lower()
        if not host:
            return True
        rp = self._get(p.scheme or "https", host)
        if rp is None:
            return True
        try:
            return rp.can_fetch(self._ua, url)
        except Exception:
            log.debug("robots can_fetch errored for %s - allowing", url, exc_info=True)
            return True

    def _get(self, scheme: str, host: str) -> Optional[RobotFileParser]:
        with self._lock:
            if host in self._cache:
                return self._cache[host]
        robots_url = urljoin(f"{scheme}://{host}", "/robots.txt")
        rp: Optional[RobotFileParser] = None
        text = self._http.get_text(robots_url)
        if text:
            rp = RobotFileParser()
            try:
                rp.parse(text.splitlines())
            except Exception:
                log.debug("failed to parse robots.txt at %s - allowing", robots_url, exc_info=True)
                rp = None
        with self._lock:
            self._cache[host] = rp
        return rp
