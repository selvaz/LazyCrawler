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
import ssl
from typing import List, Optional, Tuple, Union
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from ._log import log

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


def _ssl_context(verify: Union[bool, str]) -> Optional[ssl.SSLContext]:
    """
    Build an SSL context honoring the same ``verify`` semantics as requests:
    False -> no verification; a path -> custom CA bundle; True -> system default.
    """
    if verify is False:
        return ssl._create_unverified_context()
    if isinstance(verify, str) and verify:
        return ssl.create_default_context(cafile=verify)
    return None  # urllib uses its default (system) verification


def fetch_pdf_bytes(
    url: str,
    timeout: int = 60,
    user_agent: str = "Mozilla/5.0",
    verify: Union[bool, str] = True,
    max_bytes: int = 50_000_000,
) -> bytes:
    """
    Download the raw bytes of a PDF, capped at ``max_bytes`` (a huge/hostile PDF
    cannot exhaust memory).

    ``verify`` mirrors HTTPConfig (verify_ssl / ca_bundle) so PDF downloads work
    in SSL-inspection environments (Avast / corporate proxies) just like HTML.
    """
    req = Request(
        url,
        headers={"User-Agent": user_agent, "Accept": "application/pdf,*/*"},
        method="GET",
    )
    with urlopen(req, timeout=timeout, context=_ssl_context(verify)) as resp:
        data = resp.read(max_bytes + 1)
        if len(data) > max_bytes:
            log.warning("PDF exceeded the %d-byte cap for %s - truncating", max_bytes, url)
            data = data[:max_bytes]
        return data


# =============================================================================
# PARSERS (each degrades to "" if the library is missing)
# =============================================================================


def _extract_with_pymupdf(data: bytes) -> Tuple[str, str, Optional[str]]:
    """Return (text, title, published_iso) via PyMuPDF. ("", "", None) if absent."""
    try:
        import fitz  # PyMuPDF
    except Exception:
        log.debug("PyMuPDF (fitz) not available - trying pypdf fallback")
        return "", "", None
    try:
        doc = fitz.open(stream=data, filetype="pdf")
        texts: List[str] = []
        for page in doc:
            try:
                page_text = page.get_text("text") or ""
            except Exception:
                log.debug("PyMuPDF page extraction failed", exc_info=True)
                page_text = ""
            if page_text.strip():
                texts.append(page_text)
        meta = doc.metadata or {}
        title = (meta.get("title") or "").strip()
        published_iso = _normalize_pdf_date(
            str(meta.get("creationDate") or "")
        ) or _normalize_pdf_date(str(meta.get("modDate") or ""))
        return "\n\n".join(texts).strip(), title, published_iso
    except Exception:
        log.warning("PyMuPDF failed to parse PDF", exc_info=True)
        return "", "", None


def _extract_with_pypdf(data: bytes) -> Tuple[str, str, Optional[str]]:
    """Return (text, title, published_iso) via pypdf. ("", "", None) if absent."""
    try:
        from pypdf import PdfReader
    except Exception:
        log.debug("pypdf not available (pip install pypdf)")
        return "", "", None
    try:
        reader = PdfReader(io.BytesIO(data))
        texts: List[str] = []
        for page in reader.pages:
            try:
                page_text = page.extract_text() or ""
            except Exception:
                log.debug("pypdf page extraction failed", exc_info=True)
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
        log.warning("pypdf failed to parse PDF", exc_info=True)
        return "", "", None


def _extract_tables_with_pdfplumber(data: bytes, max_tables: int = 10) -> str:
    """Extract tables as TSV text, to embed into the document text."""
    try:
        import pdfplumber
    except Exception:
        log.debug("pdfplumber not available - skipping PDF tables")
        return ""
    except BaseException:
        # A broken optional native dependency (e.g. a cryptography build with a
        # missing _cffi_backend) can raise a low-level panic rather than
        # ImportError. Never let an optional table extractor crash the whole
        # PDF pipeline — degrade to text-only.
        log.warning(
            "pdfplumber import raised a non-standard error - skipping PDF tables", exc_info=True
        )
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
                        chunks.append(
                            f"[TABLE page={page_idx} index={tbl_idx}]\n" + "\n".join(rows)
                        )
                        table_count += 1
                        if table_count >= max_tables:
                            break
        return "\n\n".join(chunks).strip()
    except Exception:
        log.debug("pdfplumber table extraction failed", exc_info=True)
        return ""


# =============================================================================
# PUBLIC API
# =============================================================================


def extract_pdf(
    url: str,
    timeout: int = 60,
    user_agent: str = "Mozilla/5.0",
    verify: Union[bool, str] = True,
    max_bytes: int = 50_000_000,
) -> Tuple[str, str, Optional[str]]:
    """
    Download and extract a remote PDF (download capped at ``max_bytes``).

    ``verify`` mirrors HTTPConfig (verify_ssl / ca_bundle).

    Returns
    -------
    (text, title, published_iso) : Tuple[str, str, Optional[str]]
        Empty text if the download fails or no parser is available.
    """
    try:
        data = fetch_pdf_bytes(
            url, timeout=timeout, user_agent=user_agent, verify=verify, max_bytes=max_bytes
        )
    except Exception as e:
        log.warning("PDF download failed for %s: %s: %s", url, type(e).__name__, e)
        return "", "", None
    return extract_pdf_bytes(data)


def extract_pdf_artifacts(
    data: bytes,
    *,
    want: Optional[set] = None,
    max_artifacts: int = 100,
    min_image_dim: int = 48,
) -> List[dict]:
    """
    Extract artifacts (tables via pdfplumber, images via PyMuPDF) from PDF bytes.

    Returns a list of plain dicts shaped like ``Artifact`` fields (the crawler
    turns them into ``Artifact`` objects). Degrades to [] if the optional parsers
    are missing.
    """
    want = want or {"table", "image", "figure", "svg", "chart"}
    out: List[dict] = []
    pos = 0

    # -- tables (pdfplumber) --------------------------------------------------
    if "table" in want:
        try:
            import pdfplumber
        except BaseException:  # noqa: BLE001 - a broken native dep may panic (BaseException)
            pdfplumber = None  # type: ignore
            log.debug("pdfplumber unavailable - no PDF table artifacts", exc_info=True)
        if pdfplumber is not None:
            try:
                with pdfplumber.open(io.BytesIO(data)) as pdf:
                    for page_idx, page in enumerate(pdf.pages, 1):
                        if len(out) >= max_artifacts:
                            break
                        try:
                            tables = page.extract_tables() or []
                        except Exception:
                            tables = []
                        for table in tables:
                            rows = [
                                [re.sub(r"\s+", " ", str(c or "")).strip() for c in row]
                                for row in table
                                if row
                            ]
                            rows = [r for r in rows if any(r)]
                            if not rows:
                                continue
                            out.append(
                                {
                                    "artifact_type": "table",
                                    "position": pos,
                                    "caption": f"PDF page {page_idx}",
                                    "content": _rows_to_md(rows),
                                    "content_format": "markdown",
                                    "data": rows,
                                    "meta": {"page": page_idx, "rows": len(rows)},
                                }
                            )
                            pos += 1
                            if len(out) >= max_artifacts:
                                break
            except Exception:
                log.debug("PDF table artifact extraction failed", exc_info=True)

    # -- images (PyMuPDF) -----------------------------------------------------
    if want & {"image", "chart"}:
        try:
            import fitz  # PyMuPDF
        except BaseException:  # noqa: BLE001 - a broken native dep may panic (BaseException)
            fitz = None  # type: ignore
            log.debug("PyMuPDF unavailable - no PDF image artifacts", exc_info=True)
        if fitz is not None:
            try:
                doc = fitz.open(stream=data, filetype="pdf")
                seen: set = set()
                for page_idx in range(doc.page_count):
                    if len(out) >= max_artifacts:
                        break
                    for img in doc.get_page_images(page_idx, full=True):
                        xref = img[0]
                        if xref in seen:
                            continue
                        seen.add(xref)
                        try:
                            info = doc.extract_image(xref)
                        except Exception:
                            continue
                        blob = info.get("image")
                        w, h = info.get("width"), info.get("height")
                        if not blob or (w and h and (w < min_image_dim or h < min_image_dim)):
                            continue
                        ext = (info.get("ext") or "png").lower()
                        out.append(
                            {
                                "artifact_type": "image",
                                "position": pos,
                                "caption": f"PDF page {page_idx + 1}",
                                "content_format": "bytes",
                                "mime": f"image/{ext}",
                                "width": w,
                                "height": h,
                                "size_bytes": len(blob),
                                "blob": blob,
                                "meta": {"page": page_idx + 1, "xref": xref},
                            }
                        )
                        pos += 1
                        if len(out) >= max_artifacts:
                            break
                doc.close()
            except Exception:
                log.debug("PDF image artifact extraction failed", exc_info=True)

    return out


def _rows_to_md(rows: List[List[str]]) -> str:
    """Minimal Markdown table renderer for PDF table rows."""
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    norm = [[*r, *([""] * (width - len(r)))] for r in rows]
    lines = ["| " + " | ".join(norm[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
    for r in norm[1:]:
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines)


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
