# -*- coding: utf-8 -*-
"""pdf.py: detection, title/date helpers, byte-level extraction."""

from __future__ import annotations

import pytest

from lazycrawler.pdf import (
    _normalize_pdf_date,
    extract_pdf_bytes,
    looks_like_pdf,
    title_from_url,
)


def test_looks_like_pdf_by_extension():
    assert looks_like_pdf("https://e.org/report.pdf")
    assert looks_like_pdf("https://e.org/report.pdf?x=1")


def test_looks_like_pdf_by_magic_bytes():
    assert looks_like_pdf("https://e.org/x", raw_text="%PDF-1.7 ...")
    assert not looks_like_pdf("https://e.org/x", html="<html>")


def test_title_from_url():
    assert title_from_url("https://e.org/docs/my-annual-report.pdf") == "my annual report"


def test_normalize_pdf_date():
    assert _normalize_pdf_date("D:20260314123000Z") == "2026-03-14"
    assert _normalize_pdf_date("2026-03-14") == "2026-03-14"
    assert _normalize_pdf_date("") is None


def test_extract_pdf_bytes_roundtrip():
    fitz = pytest.importorskip("fitz")  # PyMuPDF (pdf extra)
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Hello from a generated PDF document for testing.")
    data = doc.tobytes()
    doc.close()
    text, title, _published = extract_pdf_bytes(data)
    assert "Hello from a generated PDF" in text
