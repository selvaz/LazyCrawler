# -*- coding: utf-8 -*-
"""
lazycrawler.pdf
===============
PDF text + metadata extraction, with graceful degradation.

Extraction pipeline:
  1. PyMuPDF (fitz)  -> best text quality + metadata
  2. pypdf           -> fallback when PyMuPDF is missing or returns no text
  3. pdfplumber      -> tables embedded as TSV (optional)

All dependencies are optional: if missing, the function degrades without raising
(returns empty text / whatever it can extract).

    pip install pymupdf pypdf pdfplumber
"""

from __future__ import annotations

import io
import re
from typing import List, Optional, Tuple
from urllib.parse import urlparse
from urllib.request import Request, urlopen


# =============================================================================
# PDF DETECTION
# =============================================================================

def looks_like_pdf(url: str, html: str = "", raw_text: str = "") -> bool:
    """
    Heuristic to decide whether a resource is a PDF.

    Checks: a .pdf extension in the URL, or the "%PDF-" magic bytes at the start
    of the fetched content.
    """
    if url.lower().split("?")[0].endswith(".pdf"):
        return True
    head = (html or "")[:20]
    raw_head = (raw_text or "")[:20]
    return head.startswith("%PDF-") or raw_head.startswith("%PDF-")


# =============================================================================
# TITLE / DATE HELPERS
# =============================================================================

def title_from_pdf_text(text: str) -> str:
    """First non-trivial line of the PDF text, used as a fallback title."""
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if len(line) >= 5:
            return line[:200]
    return ""


def title_from_url(url: str) -> str:
    """Derive a readable title from the last segment of the URL path."""
    path = (urlparse(url).path or "").strip("/")
    if not path:
        return ""
    name = path.split("/")[-1]
    name = re.sub(r"\.pdf$", "", name, flags=re.IGNORECASE)
    return name.replace("-", " ").replace("_", " ").strip()


def _normalize_pdf_date(value: str) -> Optional[str]:
    """Normalize a PDF date (e.g. 'D:20260314123000Z') to 'YYYY-MM-DD'."""
    value = (value or "").strip()
    if not value:
        return None
    m = re.match(r"D:(\d{4})(\d{2})(\d{2})", value)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", value)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


# =============================================================================
# DOWNLOAD
# =============================================================================

def fetch_pdf_bytes(url: str, timeout: int = 60, user_agent: str = "Mozilla/5.0") -> bytes:
    """Download the raw bytes of a PDF (urllib, independent of requests)."""
    req = Request(
        url,
        headers={"User-Agent": user_agent, "Accept": "application/pdf,*/*"},
        method="GET",
    )
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()


# =============================================================================
# PARSERS (each degrades to "" if the library is missing)
# =============================================================================

def _extract_with_pymupdf(data: bytes) -> Tuple[str, str, Optional[str]]:
    """Return (text, title, published_iso) via PyMuPDF. ("", "", None) if absent."""
    try:
        import fitz  # PyMuPDF
    except Exception:
        return "", "", None
    try:
        doc = fitz.open(stream=data, filetype="pdf")
        texts: List[str] = []
        for page in doc:
            try:
                page_text = page.get_text("text") or ""
            except Exception:
                page_text = ""
            if page_text.strip():
                texts.append(page_text)
        meta = doc.metadata or {}
        title = (meta.get("title") or "").strip()
        published_iso = (
            _normalize_pdf_date(str(meta.get("creationDate") or ""))
            or _normalize_pdf_date(str(meta.get("modDate") or ""))
        )
        return "\n\n".join(texts).strip(), title, published_iso
    except Exception:
        return "", "", None


def _extract_with_pypdf(data: bytes) -> Tuple[str, str, Optional[str]]:
    """Return (text, title, published_iso) via pypdf. ("", "", None) if absent."""
    try:
        from pypdf import PdfReader
    except Exception:
        return "", "", None
    try:
        reader = PdfReader(io.BytesIO(data))
        texts: List[str] = []
        for page in reader.pages:
            try:
                page_text = page.extract_text() or ""
            except Exception:
                page_text = ""
            if page_text.strip():
                texts.append(page_text)
        meta = reader.metadata or {}
        title = ""
        for key in ("/Title", "title"):
            value = meta.get(key)
            if value:
                title = str(value).strip()
                break
        published_iso = None
        for key in ("/CreationDate", "/ModDate", "creation_date", "mod_date"):
            value = meta.get(key)
            if value:
                published_iso = _normalize_pdf_date(str(value))
                if published_iso:
                    break
        return "\n\n".join(texts).strip(), title, published_iso
    except Exception:
        return "", "", None


def _extract_tables_with_pdfplumber(data: bytes, max_tables: int = 10) -> str:
    """Extract tables as TSV text, to embed into the document text."""
    try:
        import pdfplumber
    except Exception:
        return ""
    try:
        chunks: List[str] = []
        table_count = 0
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page_idx, page in enumerate(pdf.pages, 1):
                if table_count >= max_tables:
                    break
                try:
                    tables = page.extract_tables() or []
                except Exception:
                    tables = []
                for tbl_idx, table in enumerate(tables, 1):
                    if not table:
                        continue
                    rows = []
                    for row in table:
                        if not row:
                            continue
                        cells = [re.sub(r"\s+", " ", str(cell or "")).strip() for cell in row]
                        if any(cells):
                            rows.append("\t".join(cells))
                    if rows:
                        chunks.append(f"[TABLE page={page_idx} index={tbl_idx}]\n" + "\n".join(rows))
                        table_count += 1
                        if table_count >= max_tables:
                            break
        return "\n\n".join(chunks).strip()
    except Exception:
        return ""


# =============================================================================
# PUBLIC API
# =============================================================================

def extract_pdf(url: str, timeout: int = 60, user_agent: str = "Mozilla/5.0") -> Tuple[str, str, Optional[str]]:
    """
    Download and extract a remote PDF.

    Returns
    -------
    (text, title, published_iso) : Tuple[str, str, Optional[str]]
        Empty text if the download fails or no parser is available.
    """
    try:
        data = fetch_pdf_bytes(url, timeout=timeout, user_agent=user_agent)
    except Exception:
        return "", "", None
    return extract_pdf_bytes(data)


def extract_pdf_bytes(data: bytes) -> Tuple[str, str, Optional[str]]:
    """
    Extract text + metadata from already-downloaded PDF bytes.

    PyMuPDF primary -> pypdf fallback -> pdfplumber for tables.
    """
    text, title, published_iso = _extract_with_pymupdf(data)

    if not text.strip():
        text2, title2, published_iso2 = _extract_with_pypdf(data)
        if text2.strip():
            text = text2
        if not title and title2:
            title = title2
        if not published_iso and published_iso2:
            published_iso = published_iso2

    tables_text = _extract_tables_with_pdfplumber(data)
    if tables_text:
        text = f"{text}\n\n{tables_text}" if text.strip() else tables_text

    return text.strip(), title.strip(), published_iso
