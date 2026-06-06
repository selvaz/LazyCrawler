# -*- coding: utf-8 -*-
"""
lazycrawler.text
================
Text preprocessing (regex, no LLM) + link / date / canonical URL extraction
from HTML.

All pure functions, used in both pure and smart mode.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple
from urllib.parse import urljoin

from ._log import log
from .http import get_base_domain, is_excluded_url, normalize_url


# =============================================================================
# WHITESPACE
# =============================================================================

def normalize_whitespace(s: str) -> str:
    """Normalize spaces, tabs and repeated newlines."""
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


# =============================================================================
# ARTICLE PRECLEAN (regex)
# =============================================================================

_LINE_NOISE = re.compile(
    r"cookie|gdpr|we use cookies|accept all cookies|reject all cookies"
    r"|privacy policy|terms of (service|use)"
    r"|share (this|on|via) (twitter|facebook|linkedin|email|whatsapp)"
    r"|follow us on"
    r"|subscribe (to|for) (our|the|a)?\s*(newsletter|email|updates)"
    r"|sign up (for|to)? (our|the|a)?\s*(newsletter|email|updates)"
    r"|newsletter signup|join our newsletter"
    r"|all rights reserved"
    r"|^\s*©\s*\d{4}"
    r"|^\s*read more\s*$|^\s*continue reading\s*$|^\s*see more\s*$"
    r"|^\s*load more\s*$|^\s*show more\s*$"
    r"|comment(s)? \(\d+\)|leave a comment|post a comment|add a comment"
    r"|advertisement|sponsored content|paid partnership",
    re.IGNORECASE,
)
_PIPE_NAV = re.compile(r"^[^.\n]{0,200}(\|[^.\n]{0,60}){2,}$")
_BREADCRUMB = re.compile(r"^[^.\n]{0,30}\s*[>»›]\s*[^.\n]{0,30}\s*[>»›]")
_URL_ONLY = re.compile(r"^\s*https?://\S+\s*$")


def preprocess_text(raw: str) -> str:
    """
    Regex cleanup (no LLM) of a page's raw text.

    Removes: cookie/GDPR, social sharing, unsubscribe/newsletter, pipe-nav
    (Home | About | ...), breadcrumbs, URL-only lines, copyright.
    """
    if not raw:
        return raw
    s = raw.replace("\r\n", "\n").replace("\r", "\n")
    filtered = []
    for line in s.split("\n"):
        stripped = line.strip()
        if not stripped:
            filtered.append(line)
            continue
        if _LINE_NOISE.search(stripped):
            continue
        if _PIPE_NAV.match(stripped):
            continue
        if _BREADCRUMB.match(stripped):
            continue
        if _URL_ONLY.match(stripped):
            continue
        filtered.append(line)
    return normalize_whitespace("\n".join(filtered))


# =============================================================================
# LINK EXTRACTION
# =============================================================================

def extract_candidate_links(
    html: str,
    base_url: str,
    start_domain: str = "",
    same_domain_only: bool = True,
    max_links: int = 300,
) -> List[Tuple[str, str]]:
    """
    Extract candidate links from an HTML page.

    Returns
    -------
    List[Tuple[str, str]]
        List of (anchor_text, absolute_url), deduplicated, at most ``max_links``.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        log.warning("beautifulsoup4 not installed - no link extraction "
                    "(pip install beautifulsoup4)")
        return []

    soup = BeautifulSoup(html, "html.parser")
    seen: set = set()
    result: List[Tuple[str, str]] = []
    n_raw = 0
    n_offdom = 0
    n_excluded = 0
    n_dup = 0

    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        text = a.get_text(separator=" ", strip=True)
        if not href or href.startswith("#"):
            continue
        n_raw += 1
        try:
            href = urljoin(base_url, href)
        except Exception:
            n_offdom += 1
            continue
        if not href.startswith(("http://", "https://")):
            n_offdom += 1
            continue
        if same_domain_only and start_domain:
            link_domain = get_base_domain(href)
            if not (
                link_domain == start_domain
                or link_domain.endswith("." + start_domain)
                or start_domain.endswith("." + link_domain)
            ):
                n_offdom += 1
                continue
        if is_excluded_url(href, text):
            n_excluded += 1
            continue
        norm = normalize_url(href)
        if norm in seen:
            n_dup += 1
            continue
        seen.add(norm)
        result.append((text or "(no text)", href))
        if len(result) >= max_links:
            break

    log.debug(
        "  links: %d <a> tags | -%d off-domain | -%d excluded | -%d dup -> %d candidates",
        n_raw, n_offdom, n_excluded, n_dup, len(result),
    )
    return result


# =============================================================================
# CANONICAL URL
# =============================================================================

def extract_canonical_url(html: str, base_url: str) -> Optional[str]:
    """Canonical URL from <link rel="canonical" href="...">, or None."""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        link = soup.find("link", rel=lambda v: v and "canonical" in str(v).lower())
        if link and link.get("href"):
            return urljoin(base_url, link["href"].strip())
    except Exception:
        log.debug("canonical URL extraction failed for %s", base_url, exc_info=True)
        return None
    return None


# =============================================================================
# HTML TITLE
# =============================================================================

def extract_page_title(html: str) -> str:
    """Title from <title> (or <h1> fallback)."""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if m:
        return re.sub(r"<[^>]+>", "", m.group(1)).strip()
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.IGNORECASE | re.DOTALL)
    if m:
        return re.sub(r"<[^>]+>", "", m.group(1)).strip()
    return ""


# =============================================================================
# PUBLISH DATE
# =============================================================================

def _parse_datetime_any(value: str) -> Optional[datetime]:
    """Best-effort parse of a datetime string -> UTC-aware datetime."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    if not v:
        return None
    try:
        from dateutil import parser as dtparser  # type: ignore
        dt = dtparser.parse(v)
    except Exception:
        try:
            if v.endswith("Z") and "T" in v:
                dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            else:
                dt = datetime.fromisoformat(v)
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def extract_published_datetime(html: str, base_url: str = "") -> Optional[str]:
    """
    Extract the publish date from meta tags, <time datetime>, JSON-LD.

    Returns
    -------
    str | None
        ISO 8601 UTC date (e.g. "2026-01-15T14:30:00+00:00") or None.
    """
    if not html:
        return None
    try:
        from bs4 import BeautifulSoup
    except Exception:
        log.debug("beautifulsoup4 not available - skipping publish-date extraction")
        return None

    soup = BeautifulSoup(html, "html.parser")
    candidates: List[str] = []

    meta_props = [
        ("property", "article:published_time"), ("property", "og:published_time"),
        ("name", "pubdate"), ("name", "publishdate"), ("name", "publish_date"),
        ("name", "publication_date"), ("name", "date"), ("name", "DC.date.issued"),
        ("name", "DC.Date"), ("name", "dcterms.created"), ("name", "dcterms.issued"),
        ("name", "parsely-pub-date"), ("name", "sailthru.date"),
        ("itemprop", "datePublished"), ("itemprop", "dateCreated"), ("itemprop", "dateModified"),
    ]
    for attr, key in meta_props:
        tag = soup.find("meta", attrs={attr: key})
        if tag and tag.get("content"):
            candidates.append(tag["content"].strip())

    for t in soup.find_all("time"):
        dtv = t.get("datetime")
        if dtv:
            candidates.append(dtv.strip())

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        txt = (script.string or script.get_text() or "").strip()
        if not txt:
            continue
        try:
            obj = json.loads(txt)
        except Exception:
            log.debug("skipping unparseable JSON-LD block", exc_info=True)
            continue

        def _walk(o: Any) -> None:
            if isinstance(o, dict):
                for k in ("datePublished", "dateCreated", "dateModified"):
                    v = o.get(k)
                    if isinstance(v, str):
                        candidates.append(v.strip())
                for v in o.values():
                    _walk(v)
            elif isinstance(o, list):
                for v in o:
                    _walk(v)

        _walk(obj)

    now = datetime.now(timezone.utc)
    for cand in candidates:
        dt = _parse_datetime_any(cand)
        if not dt or dt.year < 1990:
            continue
        if dt > now.replace(year=now.year + 2):
            continue
        return dt.isoformat()
    return None
