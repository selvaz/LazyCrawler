# -*- coding: utf-8 -*-
"""Send the news-monitor digest + full report to Telegram.

Uses LazyTools' Telegram connector (same pattern as market-data-hub's
send_telegram_run_report.py and LazyRay's send_telegram_report.py).
Configuration comes from environment:

    TELEGRAM_BOT_TOKEN   Bot token from BotFather
    TELEGRAM_CHAT_ID     Target chat id or @channel username

Sends the DeepSeek digest (short, synthesized) as one document, then the
full article dump as one document **per geographic region**
(news_full_<session>_<region>.md, written by make_news_report.py), each
further split into size-bounded parts if it exceeds Telegram's per-file
limit.

Usage:
    python send_telegram_news_report.py
    python send_telegram_news_report.py --session-id news_20260723_070000
    python send_telegram_news_report.py --dry-run
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lazytools.connectors.telegram import TelegramClient  # noqa: E402

ROOT = Path(__file__).resolve().parent
REPORT_DIR = ROOT / "reports" / "news"

# Telegram's bot-API document cap is 50 MB; stay comfortably under it.
MAX_PART_BYTES = 45 * 1024 * 1024


def _latest_session_id() -> str | None:
    # session_id itself contains underscores (news_YYYYmmdd_HHMMSS), so it
    # can't be recovered by splitting a "news_full_<session>_<region>.md"
    # filename unambiguously -- read it off the meta sidecar's name instead,
    # which run_news_crawl.py writes as exactly "<session_id>_meta.json".
    metas = sorted(REPORT_DIR.glob("news_*_meta.json"), key=lambda p: p.stat().st_mtime)
    if not metas:
        return None
    return metas[-1].name[: -len("_meta.json")]


def _region_reports(session_id: str) -> list[Path]:
    return sorted(REPORT_DIR.glob(f"news_full_{session_id}_*.md"))


def _split_text(text: str, max_bytes: int) -> list[str]:
    """Split on paragraph boundaries ('\\n---\\n' article separators) into
    chunks no bigger than max_bytes (UTF-8), keeping articles intact."""
    articles = text.split("\n---\n")
    parts: list[str] = []
    current: list[str] = []
    current_size = 0
    for article in articles:
        piece = article + "\n---\n"
        piece_size = len(piece.encode("utf-8"))
        if current and current_size + piece_size > max_bytes:
            parts.append("".join(current))
            current, current_size = [], 0
        current.append(piece)
        current_size += piece_size
    if current:
        parts.append("".join(current))
    return parts or [text]


def send_document(client: TelegramClient, *, chat_id: str, filename: str,
                   content: bytes, caption: str) -> None:
    client.send_document(chat_id=chat_id, document=content, filename=filename,
                          caption=caption[:1024])


def main() -> int:
    p = argparse.ArgumentParser(description="Send the news-monitor report via Telegram")
    p.add_argument("--session-id", help="Defaults to the latest session found in reports/news/")
    p.add_argument("--dry-run", action="store_true", help="Resolve files and print, but do not send")
    args = p.parse_args()

    session_id = args.session_id or _latest_session_id()
    if not session_id:
        print(f"No news report found in {REPORT_DIR}; run make_news_report.py first.", file=sys.stderr)
        return 1

    digest_path = REPORT_DIR / f"news_digest_{session_id}.md"
    cost_path = REPORT_DIR / f"news_cost_{session_id}.md"
    region_paths = _region_reports(session_id)
    if not region_paths:
        print(f"No news_full_{session_id}_*.md files found; run make_news_report.py first.", file=sys.stderr)
        return 1

    region_counts = {
        rp.stem[len(f"news_full_{session_id}_"):]: rp.read_text(encoding="utf-8").count("\n---\n")
        for rp in region_paths
    }
    n_articles = sum(region_counts.values())

    if args.dry_run:
        print(f"Would send: {digest_path if digest_path.exists() else '(no digest)'}")
        print(f"Would send: {cost_path if cost_path.exists() else '(no cost report)'}")
        for region, count in region_counts.items():
            print(f"Would send [{region}]: {count} articles")
        print(f"Total: {n_articles} articles across {len(region_paths)} regions")
        return 0

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Telegram not configured: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.", file=sys.stderr)
        return 2

    with TelegramClient.from_token(token) as client:
        if digest_path.exists():
            send_document(
                client, chat_id=chat_id, filename=digest_path.name,
                content=digest_path.read_bytes(),
                caption=f"News digest | {session_id} | {n_articles} articles crawled",
            )
            print(f"Sent Telegram document: {digest_path.name}")
        else:
            client.send_message(
                chat_id=chat_id,
                text=f"News crawl {session_id}: {n_articles} articles (digest generation was skipped)",
            )

        if cost_path.exists():
            client.send_message(chat_id=chat_id, text=cost_path.read_text(encoding="utf-8")[:4000])
            print(f"Sent Telegram message: {cost_path.name}")

        for region_path in region_paths:
            region = region_path.stem[len(f"news_full_{session_id}_"):]
            region_text = region_path.read_text(encoding="utf-8")
            parts = _split_text(region_text, MAX_PART_BYTES)
            for i, part in enumerate(parts, start=1):
                suffix = f"_part{i}of{len(parts)}" if len(parts) > 1 else ""
                filename = f"news_full_{session_id}_{region}{suffix}.md"
                caption = f"{region} | {region_counts[region]} articles | {session_id}"
                if len(parts) > 1:
                    caption = f"{region} {i}/{len(parts)} | {session_id}"
                send_document(
                    client, chat_id=chat_id, filename=filename,
                    content=part.encode("utf-8"), caption=caption,
                )
                print(f"Sent Telegram document: {filename}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
