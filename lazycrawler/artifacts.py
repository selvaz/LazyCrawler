# -*- coding: utf-8 -*-
"""
lazycrawler.artifacts
=====================
Extraction of non-textual page content — **tables, images, figures, charts,
SVG** — as structured ``Artifact`` records.

Design (best-practice driven):
- Each artifact is a self-contained unit with provenance, kept whole (a table is
  never split; an image keeps its caption / surrounding context).
- Extraction here is **pure** (regex/BeautifulSoup, no LLM, no network): it
  identifies artifacts and renders a text representation. Image *bytes download*
  and *vision-LLM enrichment* are separate, optional layers driven by the crawler
  (so pure mode pays nothing).
- Tables -> Markdown + structured rows (header↔value joins preserved).
- Images/charts -> absolute src URL + alt + caption + ±N chars of surrounding
  context; tiny/spacer/tracking images are filtered out.
- Inline SVG -> markup captured (chart candidate).

The ``Artifact.blob`` field (raw image bytes) is excluded from serialization so
it never leaks into agent-facing JSON; the DB layer reads it directly.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, List, Literal, Optional, Tuple
from urllib.parse import urljoin

from pydantic import BaseModel, Field

from ._log import log
from .http import get_hostname, normalize_url, sha256_hex


def bytes_sha256(data: bytes) -> str:
    """SHA256 hex of raw bytes (image content hash)."""
    return hashlib.sha256(data or b"").hexdigest()


_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),  # WEBP (RIFF....WEBP)
    (b"BM", "image/bmp"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
)


def sniff_image(
    data: bytes, content_type: str = ""
) -> Tuple[Optional[str], Optional[int], Optional[int]]:
    """
    Return (mime, width, height) for image bytes. MIME comes from the response
    Content-Type when it is an image type, else from magic bytes. Dimensions use
    Pillow when available (``pip install lazycrawler[image]``), else None.
    """
    mime = content_type if content_type.startswith("image/") else None
    if not mime:
        for sig, m in _MAGIC:
            if data[: len(sig)] == sig:
                mime = m
                break
    width = height = None
    try:
        import io

        from PIL import Image  # type: ignore

        with Image.open(io.BytesIO(data)) as im:
            width, height = im.size
            if not mime and im.format:
                mime = f"image/{im.format.lower()}"
    except Exception:
        log.debug("sniff_image: Pillow unavailable or undecodable - dims omitted", exc_info=True)
    return mime, width, height


ArtifactType = Literal["table", "image", "figure", "svg", "chart"]

# Heuristic markers that an image/figure is a chart/graph rather than a photo.
_CHART_HINT = re.compile(r"chart|graph|plot|diagram|figure|fig\.|infographic", re.IGNORECASE)
# Obvious non-content images (logos, spacers, icons, tracking pixels).
_NOISE_IMG = re.compile(r"spacer|pixel|1x1|blank|logo|icon|sprite|avatar|badge", re.IGNORECASE)


class Artifact(BaseModel):
    """A non-textual page element (table / image / figure / chart / svg)."""

    artifact_type: ArtifactType
    position: int = 0  # order of appearance on the page
    src_url: Optional[str] = None  # absolute URL (images)
    alt: Optional[str] = None
    caption: Optional[str] = None
    context: Optional[str] = None  # surrounding text when no caption
    content: Optional[str] = None  # text representation (markdown table / svg markup)
    content_format: Optional[str] = None  # markdown | svg | url | csv
    data: Optional[Any] = None  # structured rows (tables) / chart data (vision)
    summary: Optional[str] = None  # vision/LLM enrichment (smart layer)
    mime: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    bytes_hash: Optional[str] = None  # sha256 of downloaded image bytes
    size_bytes: Optional[int] = None
    content_hash: Optional[str] = None  # dedup key (per page)
    meta: dict = Field(default_factory=dict)
    # Raw image bytes — kept off the wire (DB reads it directly).
    blob: Optional[bytes] = Field(default=None, exclude=True, repr=False)

    def ensure_content_hash(self) -> "Artifact":
        if not self.content_hash:
            basis = (
                self.src_url
                or self.bytes_hash
                or self.content
                or (repr(self.data) if self.data else None)
                or self.alt
                or ""
            )
            self.content_hash = sha256_hex(f"{self.artifact_type}:{self.position}:{basis}")
        return self


# =============================================================================
# HELPERS
# =============================================================================


def _clean(text: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _surrounding_text(el: Any, chars: int) -> str:
    """±``chars`` of plain text around an element (context when no caption)."""
    before = after = ""
    try:
        prev = el.find_all_previous(string=True)
        before = _clean(" ".join(reversed([str(s) for s in prev[:20]])))[-chars:]
    except Exception:
        log.debug("artifact: failed reading preceding context", exc_info=True)
    try:
        nxt = el.find_all_next(string=True)
        after = _clean(" ".join(str(s) for s in nxt[:20]))[:chars]
    except Exception:
        log.debug("artifact: failed reading following context", exc_info=True)
    ctx = f"{before} … {after}".strip(" …")
    return ctx or ""


def _figure_caption(el: Any) -> str:
    """The <figcaption> of the nearest enclosing <figure>, if any."""
    try:
        fig = el.find_parent("figure")
        if fig is not None:
            cap = fig.find("figcaption")
            if cap is not None:
                return _clean(cap.get_text(" ", strip=True))
    except Exception:
        log.debug("artifact: figcaption lookup failed", exc_info=True)
    return ""


def _rows_to_markdown(rows: List[List[str]]) -> str:
    """Render parsed table rows to a GitHub-flavored Markdown table."""
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    norm = [[*(c or "" for c in r), *([""] * (width - len(r)))] for r in rows]
    header = norm[0]
    body = norm[1:] if len(norm) > 1 else []
    lines = ["| " + " | ".join(_clean(c) for c in header) + " |"]
    lines.append("| " + " | ".join(["---"] * width) + " |")
    for r in body:
        lines.append("| " + " | ".join(_clean(c) for c in r) + " |")
    return "\n".join(lines)


def _parse_table(table: Any) -> Optional[List[List[str]]]:
    """Extract a table's cells as rows of text. None for layout/empty tables."""
    if (table.get("role") or "").lower() in ("presentation", "none"):
        return None
    # ignore nested tables: only parse a table if it isn't inside another table
    if table.find_parent("table") is not None:
        return None
    rows: List[List[str]] = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["th", "td"], recursive=False) or tr.find_all(["th", "td"])
        if not cells:
            continue
        rows.append([_clean(c.get_text(" ", strip=True)) for c in cells])
    # drop trivially small tables (often layout): need >=2 rows or >=2 cols
    if not rows or (len(rows) < 2 and max((len(r) for r in rows), default=0) < 2):
        return None
    if not any(any(c for c in r) for r in rows):
        return None
    return rows


def _table_caption(table: Any) -> str:
    cap = table.find("caption")
    if cap is not None:
        t = _clean(cap.get_text(" ", strip=True))
        if t:
            return t
    return _figure_caption(table)


def _img_dims(tag: Any) -> tuple[Optional[int], Optional[int]]:
    def _int(v: Any) -> Optional[int]:
        try:
            return int(re.sub(r"[^\d].*$", "", str(v)))
        except Exception:
            return None

    return _int(tag.get("width")), _int(tag.get("height"))


def _is_noise_image(src: str, alt: str, w: Optional[int], h: Optional[int], min_dim: int) -> bool:
    if not src:
        return True
    if _NOISE_IMG.search(src) or (alt and _NOISE_IMG.search(alt)):
        return True
    if w is not None and h is not None and (w < min_dim or h < min_dim):
        return True
    return False


def _looks_like_chart(src: str, alt: str, caption: str, classes: str) -> bool:
    blob = " ".join((src, alt, caption, classes))
    return bool(_CHART_HINT.search(blob))


# =============================================================================
# HTML EXTRACTION
# =============================================================================


def extract_html_artifacts(
    html: str,
    base_url: str,
    *,
    types: Optional[set] = None,
    min_image_dim: int = 48,
    context_chars: int = 200,
    max_artifacts: int = 100,
    max_svg_chars: int = 20_000,
    same_domain_images: bool = False,
) -> List[Artifact]:
    """
    Extract tables / images / figures / charts / SVG from an HTML page.

    Parameters
    ----------
    types : set[str] | None
        Which artifact types to collect (default: all).
    min_image_dim : int
        Drop images whose declared width/height is below this (filters icons).
    context_chars : int
        Characters of surrounding text captured for images lacking a caption.
    max_artifacts : int
        Hard cap on artifacts returned per page.
    same_domain_images : bool
        If True, keep only images hosted on the page's own domain.
    """
    if not html:
        return []
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        log.warning("beautifulsoup4 not installed - artifact extraction disabled")
        return []

    want = types or {"table", "image", "figure", "svg", "chart"}
    soup = BeautifulSoup(html, "html.parser")
    out: List[Artifact] = []
    pos = 0
    page_host = get_hostname(base_url)

    # -- tables ---------------------------------------------------------------
    if "table" in want:
        for table in soup.find_all("table"):
            rows = _parse_table(table)
            if rows is None:
                continue
            caption = _table_caption(table)
            out.append(
                Artifact(
                    artifact_type="table",
                    position=pos,
                    caption=caption or None,
                    content=_rows_to_markdown(rows),
                    content_format="markdown",
                    data=rows,
                    meta={"rows": len(rows), "cols": max(len(r) for r in rows)},
                ).ensure_content_hash()
            )
            pos += 1
            if len(out) >= max_artifacts:
                return out

    # -- images / charts ------------------------------------------------------
    if want & {"image", "chart"}:
        for img in soup.find_all("img"):
            raw_src = (
                img.get("src") or img.get("data-src") or img.get("data-original") or ""
            ).strip()
            if not raw_src or raw_src.startswith("data:"):
                continue
            try:
                src = normalize_url(urljoin(base_url, raw_src))
            except Exception:
                continue
            if not src.startswith(("http://", "https://")):
                continue
            alt = _clean(img.get("alt"))
            w, h = _img_dims(img)
            if _is_noise_image(src, alt, w, h, min_image_dim):
                continue
            if same_domain_images and page_host and get_hostname(src) != page_host:
                continue
            caption = _figure_caption(img)
            classes = " ".join(img.get("class") or [])
            is_chart = _looks_like_chart(src, alt, caption, classes)
            atype: ArtifactType = "chart" if is_chart else "image"
            if atype not in want:
                continue
            out.append(
                Artifact(
                    artifact_type=atype,
                    position=pos,
                    src_url=src,
                    alt=alt or None,
                    caption=caption or None,
                    context=None if caption else (_surrounding_text(img, context_chars) or None),
                    content_format="url",
                    width=w,
                    height=h,
                ).ensure_content_hash()
            )
            pos += 1
            if len(out) >= max_artifacts:
                return out

    # -- inline SVG (often charts) -------------------------------------------
    if want & {"svg", "chart"}:
        for svg in soup.find_all("svg"):
            markup = str(svg)[:max_svg_chars]
            classes = " ".join(svg.get("class") or [])
            caption = _figure_caption(svg)
            # treat as chart when it has many drawing primitives or a chart hint
            n_prims = len(svg.find_all(["path", "rect", "circle", "line", "polyline"]))
            is_chart = n_prims >= 5 or _looks_like_chart("", "", caption, classes)
            atype = "chart" if (is_chart and "chart" in want) else "svg"
            if atype not in want:
                continue
            out.append(
                Artifact(
                    artifact_type=atype,
                    position=pos,
                    caption=caption or None,
                    context=None if caption else (_surrounding_text(svg, context_chars) or None),
                    content=markup,
                    content_format="svg",
                    meta={"primitives": n_prims},
                ).ensure_content_hash()
            )
            pos += 1
            if len(out) >= max_artifacts:
                return out

    log.debug("artifacts: extracted %d from %s", len(out), base_url[:80])
    return out
