"""Session provenance shared by writers and recap readers."""

from __future__ import annotations

import os
from typing import Optional

from ._log import log


def valid_session_origin(value: object) -> bool:
    """Whether a value is a printable, whitespace-free label of 1–64 characters."""
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 64
        and value.isprintable()
        and not any(char.isspace() for char in value)
    )


def resolve_session_origin(origin: Optional[str] = None) -> str:
    """Explicit label, then LAZYCRAWLER_SESSION_ORIGIN, then ``unknown``.

    An invalid selected value warns and becomes ``unknown``; it does not fall
    through to a lower-priority value. None means no explicit label was supplied.
    """
    value = (
        origin if origin is not None else os.environ.get("LAZYCRAWLER_SESSION_ORIGIN", "unknown")
    )
    if valid_session_origin(value):
        return value
    log.warning(
        "Invalid session origin %r; using 'unknown' (expected 1–64 printable characters, no whitespace)",
        value,
    )
    return "unknown"


def production_session_filter() -> str:
    """SQL predicate for sessions: ``origin LIKE 'job:%'``.

    Use against the sessions table (or in a sessions subquery when joining
    pages via crawl_edges). NULL, unknown and interactive labels are excluded.
    This follows SQLite's LIKE semantics, including its default ASCII case folding.
    """
    return "origin LIKE 'job:%'"
