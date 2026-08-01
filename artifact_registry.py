# -*- coding: utf-8 -*-
"""artifact_registry.py — best-effort cataloging of news report artifacts
into the shared LazyTools artifact registry (``lazytools.registry``).

This is entirely optional plumbing: the news-monitor pipeline
(run_news_crawl.py / make_news_report.py / send_telegram_news_report.py)
works identically whether or not it fires. Three independent reasons it may
be a no-op, all of them fine:

1. ``lazytools`` is not installed in this environment at all (it is not a
   declared dependency of this package — see pyproject.toml/requirements.txt
   — only imported opportunistically, same as the existing Telegram
   integration in send_telegram_news_report.py).
2. ``lazytools.registry.resolve_db("crawler_artifacts")`` returns ``None``
   because ``CRAWLER_ARTIFACTS_DB`` is unset — this DB is declared
   ``required=False`` in LazyTools' ``KNOWN_DBS``, i.e. opt-in per
   deployment.
3. Anything else goes wrong while registering (a locked/corrupt sqlite file,
   an unexpected exception inside lazytools, ...).

In every case the calling script (make_news_report.py) must keep running:
registering a report as an artifact is a nice-to-have index entry, never a
condition for the report-generation job's success. Mirrors
market-data-hub's ``market_data_hub/artifact_registry.py``.
"""

from __future__ import annotations

import sys

try:
    from lazytools.registry import register_artifact, resolve_db
except ImportError:
    resolve_db = None  # type: ignore[assignment]
    register_artifact = None  # type: ignore[assignment]


def register_report_artifact(
    *, kind: str, title: str, summary: str, tags: list[str], content_uri: str
) -> str | None:
    """Catalog one news report file as a ``lazycrawler`` artifact.

    Best-effort only: swallows every exception (import errors, missing/unset
    ``CRAWLER_ARTIFACTS_DB``, sqlite errors, ...) and prints a warning to
    stderr instead of raising, so callers never need to guard this call.

    Follows the ecosystem-wide searchability convention (see
    market-data-hub's ``market_data_hub/artifact_registry.py`` for the same
    pattern applied there): ``kind`` is one of a small shared enum
    (``"report"`` for a full document, ``"digest"`` for a condensed
    executive summary), ``tags`` mixes the shared ``"daily"`` cadence tag
    with repo-specific domain tags, and ``title``/``summary`` are written
    keyword-dense since ``search_artifacts``/``search_everywhere`` only ever
    ``LIKE``-search those two fields plus ``tags`` -- never ``content``.

    Args:
        kind: Artifact kind -- ``"report"`` for a full generated document,
            ``"digest"`` for a condensed executive-summary-style document.
        title: Short human-readable title, ``"<type> <session/date>"``
            (e.g. ``"News digest news_20260801_070000"``).
        summary: Keyword-dense summary -- topic/theme/region/source names,
            not just counts, since ``content`` is never searched.
        tags: Free-text tags (e.g. ``["daily", "region:us"]``).
        content_uri: Path/URI to the actual report file (the .md report).

    Returns:
        The new artifact's id, or ``None`` if registration was skipped or
        failed.
    """
    if resolve_db is None or register_artifact is None:
        return None
    try:
        db_path = resolve_db("crawler_artifacts")
        if not db_path:
            return None  # CRAWLER_ARTIFACTS_DB unset -- optional, skip silently
        return register_artifact(
            db_path,
            repo="lazycrawler",
            kind=kind,
            title=title,
            summary=summary,
            tags=tags,
            content_uri=content_uri,
        )
    except Exception as e:
        print(f"WARNING: artifact registration failed (non-fatal): {e}", file=sys.stderr)
        return None
