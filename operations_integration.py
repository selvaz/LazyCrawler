"""Best-effort bridge from LazyCrawler jobs to LazyTools' operations catalog."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


def start(task_name: str, *, parameters: dict[str, Any], source_db: str | None = None) -> tuple[Any, str | None]:
    try:
        from lazytools.operations import OperationsCatalog
    except ImportError:
        print("Operations catalog unavailable; continuing without central run registration.", file=sys.stderr)
        return None, None
    try:
        catalog = OperationsCatalog()
        run_id = catalog.start_run(task_name, parameters=parameters, source_repo="LazyCrawler", source_db=source_db)
    except Exception as exc:  # noqa: BLE001 - central catalog is optional
        print(f"Operations catalog unavailable; continuing without registration: {exc}", file=sys.stderr)
        return None, None
    print(f"OPERATIONS_RUN_ID={run_id}")
    return catalog, run_id


def finish(catalog: Any, run_id: str | None, *, ok: bool, error: str | None = None) -> None:
    if catalog is None or run_id is None:
        return
    try:
        catalog.finish_run(run_id, "succeeded" if ok else "failed", error=error)
    except Exception as exc:  # noqa: BLE001 - cataloging must not break the crawl
        print(f"Operations catalog update failed: {exc}", file=sys.stderr)


def register_file(catalog: Any, run_id: str | None, path: str | Path, *, kind: str = "artifact",
                  role: str | None = None) -> None:
    if catalog is None or run_id is None or not Path(path).is_file():
        return
    try:
        catalog.register_file(run_id, path, kind=kind, role=role)
    except Exception as exc:  # noqa: BLE001
        print(f"Operations artifact registration failed for {path}: {exc}", file=sys.stderr)


def register_json(catalog: Any, run_id: str | None, name: str, value: Any, *, kind: str = "result") -> None:
    if catalog is None or run_id is None:
        return
    try:
        catalog.register_json(run_id, name, value, kind=kind, role=kind)
    except Exception as exc:  # noqa: BLE001
        print(f"Operations JSON registration failed for {name}: {exc}", file=sys.stderr)
