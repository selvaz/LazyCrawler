"""Best-effort bridge from LazyCrawler jobs to LazyTools' operations catalog.

The actual start/finish/register_* logic lives once in
``lazytools.operations.integration`` (shared with market-data-hub's
identically shaped shim) so a fix only has to happen in one place. This file
only needs to exist locally because the import of ``lazytools`` itself must
stay optional: a scheduled crawl must keep running even if LazyTools isn't
installed in this environment.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_SOURCE_REPO = "LazyCrawler"

try:
    from lazytools.operations.integration import finish, register_file, register_json
    from lazytools.operations.integration import start as _start
except ImportError:
    def start(task_name: str, *, parameters: dict[str, Any], source_db: str | None = None) -> tuple[Any, str | None]:
        print("Operations catalog unavailable; continuing without central run registration.", file=sys.stderr)
        return None, None

    def finish(catalog: Any, run_id: str | None, *, ok: bool, error: str | None = None) -> None:
        return None

    def register_file(catalog: Any, run_id: str | None, path: str | Path, *, kind: str = "artifact",
                      role: str | None = None) -> None:
        return None

    def register_json(catalog: Any, run_id: str | None, name: str, value: Any, *, kind: str = "result") -> None:
        return None
else:
    def start(task_name: str, *, parameters: dict[str, Any], source_db: str | None = None) -> tuple[Any, str | None]:
        return _start(task_name, source_repo=_SOURCE_REPO, parameters=parameters, source_db=source_db)
