# -*- coding: utf-8 -*-
"""Artifact extraction (tables/images/charts/svg), DB round-trip, crawler wiring."""

from __future__ import annotations

from lazycrawler.artifacts import (
    Artifact,
    artifact_anchor,
    bytes_sha256,
    extract_html_artifacts,
    extract_html_artifacts_anchored,
    sniff_image,
)
from lazycrawler.http import url_hash
from lazycrawler.markdown import html_to_markdown, render_for_rag

TABLE_HTML = (
    "<table><caption>Q1</caption>"
    "<tr><th>Region</th><th>Rev</th></tr>"
    "<tr><td>EU</td><td>10</td></tr>"
    "<tr><td>US</td><td>20</td></tr></table>"
)
CHART_HTML = (
    '<figure><img src="/img/sales-chart.png" alt="sales" width="600" height="400">'
    "<figcaption>Quarterly sales</figcaption></figure>"
)
LOGO_HTML = '<img src="/logo.png" alt="logo" width="32" height="32">'
PHOTO_HTML = '<img src="/photos/team.jpg" alt="the team" width="800" height="600">'


# -- pure HTML extraction -----------------------------------------------------


def test_table_to_markdown_and_rows():
    arts = extract_html_artifacts(f"<body>{TABLE_HTML}</body>", "https://e.org/p")
    assert len(arts) == 1
    t = arts[0]
    assert t.artifact_type == "table" and t.caption == "Q1"
    assert "| Region | Rev |" in t.content and "| EU | 10 |" in t.content
    assert t.data == [["Region", "Rev"], ["EU", "10"], ["US", "20"]]
    assert t.content_hash


def test_chart_vs_image_classification():
    arts = extract_html_artifacts(f"<body>{CHART_HTML}{PHOTO_HTML}</body>", "https://e.org/p")
    by_type = {a.artifact_type for a in arts}
    assert by_type == {"chart", "image"}
    chart = next(a for a in arts if a.artifact_type == "chart")
    assert chart.src_url == "https://e.org/img/sales-chart.png"
    assert chart.caption == "Quarterly sales"
    photo = next(a for a in arts if a.artifact_type == "image")
    assert photo.src_url.endswith("/photos/team.jpg")


def test_noise_and_tiny_images_filtered():
    arts = extract_html_artifacts(f"<body>{LOGO_HTML}</body>", "https://e.org/p")
    assert arts == []


def test_data_uri_images_skipped():
    html = '<body><img src="data:image/png;base64,iVBOR" alt="x" width="500" height="500"></body>'
    assert extract_html_artifacts(html, "https://e.org/p") == []


def test_same_domain_images_filter():
    html = '<body><img src="https://cdn.other.com/p.jpg" alt="x" width="500" height="500"></body>'
    assert extract_html_artifacts(html, "https://e.org/p", same_domain_images=True) == []
    assert len(extract_html_artifacts(html, "https://e.org/p", same_domain_images=False)) == 1


def test_types_filter():
    arts = extract_html_artifacts(
        f"<body>{TABLE_HTML}{PHOTO_HTML}</body>", "https://e.org/p", types={"table"}
    )
    assert [a.artifact_type for a in arts] == ["table"]


def test_image_context_when_no_caption():
    html = f"<body><p>Before text here.</p>{PHOTO_HTML}<p>After text here.</p></body>"
    art = extract_html_artifacts(html, "https://e.org/p")[0]
    assert art.context and ("Before" in art.context or "After" in art.context)


# -- byte helpers -------------------------------------------------------------


def test_sniff_image_mime_from_magic():
    mime, w, h = sniff_image(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16, "")
    assert mime == "image/png"


def test_bytes_sha256_stable():
    assert bytes_sha256(b"abc") == bytes_sha256(b"abc")
    assert bytes_sha256(b"abc") != bytes_sha256(b"abd")


# -- DB round-trip ------------------------------------------------------------


def test_db_add_and_get_artifacts(tmp_db):
    uh = url_hash("https://e.org/p")
    tmp_db.upsert_page({"url": "https://e.org/p", "url_hash": uh, "status": "done", "mode": "pure"})
    arts = extract_html_artifacts(f"<body>{TABLE_HTML}{PHOTO_HTML}</body>", "https://e.org/p")
    n = tmp_db.add_artifacts(uh, arts)
    assert n == 2
    # idempotent: same content_hashes -> no new rows
    assert tmp_db.add_artifacts(uh, arts) == 0
    got = tmp_db.get_artifacts(url_hash=uh)
    assert len(got) == 2
    table = next(g for g in got if g["artifact_type"] == "table")
    assert table["data"] == [["Region", "Rev"], ["EU", "10"], ["US", "20"]]
    assert "blob" not in table  # bytes omitted by default
    # type filter
    assert len(tmp_db.get_artifacts(url_hash=uh, artifact_type="image")) == 1


def test_db_stores_blob_when_requested(tmp_db):
    uh = url_hash("https://e.org/img")
    tmp_db.upsert_page(
        {"url": "https://e.org/img", "url_hash": uh, "status": "done", "mode": "pure"}
    )
    a = Artifact(
        artifact_type="image", src_url="https://e.org/x.png", blob=b"\x89PNG\r\n\x1a\nDATA"
    )
    a.bytes_hash = bytes_sha256(a.blob)
    a.ensure_content_hash()
    tmp_db.add_artifacts(uh, [a])
    got = tmp_db.get_artifacts(url_hash=uh, include_blob=True)
    assert got[0]["blob"] == b"\x89PNG\r\n\x1a\nDATA"


# -- crawler integration ------------------------------------------------------


def test_crawler_extracts_and_persists_artifacts(stub_fetch, tmp_db, make_crawler):
    url = "https://site.example/report"
    stub_fetch(links_map={url: TABLE_HTML + CHART_HTML})
    c = make_crawler(db=tmp_db, extract_artifacts=True)
    r = c.crawl(url, mode="pure", session_id="a1")[0]
    types = sorted(a.artifact_type for a in r.artifacts)
    assert "table" in types and "chart" in types
    # persisted + reachable by session
    assert len(tmp_db.get_artifacts(session_id="a1")) == len(r.artifacts)


def test_crawler_artifacts_off_by_default(stub_fetch, make_crawler):
    url = "https://site.example/report2"
    stub_fetch(links_map={url: TABLE_HTML})
    r = make_crawler().crawl(url, mode="pure")[0]
    assert r.artifacts == []


def test_crawler_reloads_artifacts_from_cache(stub_fetch, tmp_db, make_crawler):
    url = "https://site.example/report3"
    stub_fetch(links_map={url: TABLE_HTML})
    c = make_crawler(db=tmp_db, extract_artifacts=True)
    c.crawl(url, mode="pure", session_id="cold")
    r2 = c.crawl(url, mode="pure", session_id="warm")[0]
    assert r2.from_cache is True
    assert any(a.artifact_type == "table" for a in r2.artifacts)


# -- Markdown anchoring + render_for_rag --------------------------------------


def test_anchored_extraction_replaces_with_placeholders():
    arts, anchored = extract_html_artifacts_anchored(
        f"<body><p>Intro.</p>{TABLE_HTML}<p>Mid.</p>{CHART_HTML}</body>", "https://e.org/p"
    )
    assert len(arts) == 2
    for a in arts:
        assert artifact_anchor(a.content_hash) in anchored
    # original table cells should be gone from the anchored HTML
    assert "Region" not in anchored and "<table" not in anchored


def test_anchored_markdown_has_anchors_not_tables():
    arts, anchored = extract_html_artifacts_anchored(
        f"<body>{TABLE_HTML}</body>", "https://e.org/p"
    )
    md = html_to_markdown(anchored, "https://e.org/p")
    assert artifact_anchor(arts[0].content_hash) in md
    assert "| Region |" not in md  # table not duplicated inline


def test_crawler_markdown_anchors(stub_fetch, tmp_db, make_crawler):
    url = "https://site.example/anchored"
    stub_fetch(links_map={url: TABLE_HTML + CHART_HTML})
    c = make_crawler(
        db=tmp_db, extract_artifacts=True, emit_markdown=True, markdown_artifact_anchors=True
    )
    r = c.crawl(url, mode="pure", session_id="anch")[0]
    assert r.markdown and "[[artifact:" in r.markdown
    assert "| Region |" not in r.markdown  # externalized, not inline
    # every artifact's anchor appears in the markdown
    for a in r.artifacts:
        assert artifact_anchor(a.content_hash) in r.markdown


def test_render_for_rag_pairs_anchors_with_content():
    page = {"title": "Report", "markdown": "Intro [[artifact:X]] outro.", "clean_text": "Intro"}
    arts = extract_html_artifacts(f"<body>{TABLE_HTML}</body>", "https://e.org/p")
    doc = render_for_rag(page, artifacts=arts)
    assert "# Report" in doc
    assert "## Artifacts" in doc
    # the appendix resolves the table anchor to its Markdown content
    assert artifact_anchor(arts[0].content_hash) in doc
    assert "| Region | Rev |" in doc


def test_render_for_rag_from_pageresult(stub_fetch, make_crawler):
    url = "https://site.example/rag"
    stub_fetch(links_map={url: TABLE_HTML})
    c = make_crawler(extract_artifacts=True, emit_markdown=True, markdown_artifact_anchors=True)
    r = c.crawl(url, mode="pure")[0]
    doc = render_for_rag(r)
    # inline anchor in body + resolvable block in the appendix
    assert doc.count(artifact_anchor(r.artifacts[0].content_hash)) >= 2
    assert "## Artifacts" in doc and "| Region | Rev |" in doc


def test_crawler_downloads_artifact_bytes(stub_fetch, tmp_db, make_crawler, monkeypatch):
    import lazycrawler.http as http_mod

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

    def fake_fetch_bytes(self, url):
        return png, "image/png", 200

    monkeypatch.setattr(http_mod.HTTPClient, "fetch_bytes", fake_fetch_bytes)
    url = "https://site.example/gallery"
    stub_fetch(links_map={url: PHOTO_HTML})
    c = make_crawler(db=tmp_db, extract_artifacts=True, download_artifact_bytes=True)
    r = c.crawl(url, mode="pure", session_id="g1")[0]
    img = next(a for a in r.artifacts if a.artifact_type == "image")
    assert img.bytes_hash == bytes_sha256(png)
    assert img.size_bytes == len(png) and img.mime == "image/png"
    stored = tmp_db.get_artifacts(url_hash=url_hash(url), include_blob=True)
    assert stored[0]["blob"] == png


def test_figure_not_in_default_artifact_types():
    from lazycrawler import CrawlerConfig

    # "figure" was advertised but never emitted as its own artifact (audit #9)
    assert "figure" not in CrawlerConfig().artifact_types


def test_pdf_image_only_yields_artifacts(stub_fetch, tmp_db):
    """Regression: a text-less PDF (image-only / scanned) must still yield its
    artifacts — previously it hit the no_text early-return before extraction."""
    import io

    import pytest

    fitz = pytest.importorskip("fitz")  # PyMuPDF (the `pdf` extra)

    from lazycrawler import CrawlerConfig, HTTPConfig, WebCrawler

    buf = io.BytesIO()
    doc = fitz.open()
    page = doc.new_page()
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 90, 90))
    pix.set_rect(pix.irect, (200, 50, 50))
    page.insert_image(fitz.Rect(50, 50, 140, 140), pixmap=pix)
    doc.save(buf)

    u = "https://e.org/scan.pdf"
    stub_fetch(pdf_map={u: buf.getvalue()})
    with WebCrawler(
        CrawlerConfig(
            max_depth=0,
            respect_robots=False,
            extract_artifacts=True,
            artifact_types=("image",),
            min_image_dim=10,
        ),
        HTTPConfig(verify_ssl=False, link_delay=0, allow_private_networks=True),
        db=tmp_db,
    ) as c:
        r = c.crawl(u, mode="pure")[0]

    assert r.is_pdf and r.status == "no_text"  # no text, but…
    assert len(r.artifacts) == 1  # …the image artifact is extracted
    assert r.artifacts[0].artifact_type == "image"
    assert len(tmp_db.get_artifacts(url_hash=r.url_hash)) == 1  # and persisted
