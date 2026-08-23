# -*- coding: utf-8 -*-
"""HTTP utilities, text extraction, exclusion, FetchResult, retry, robots."""

from __future__ import annotations

import requests

from lazycrawler.config import HTTPConfig
from lazycrawler.http import (
    DEFAULT_EXCLUDE_PATTERNS,
    FetchResult,
    HTTPClient,
    RobotsChecker,
    compile_exclude,
    content_hash,
    is_excluded_url,
    url_hash,
)

# -- URL normalization / hashing ---------------------------------------------


def test_url_hash_strips_tracking_params():
    assert url_hash("https://x.example/a?utm_source=z") == url_hash("https://x.example/a")


def test_url_hash_lowercases_host_and_trims_slash():
    assert url_hash("https://X.EXAMPLE/a/") == url_hash("https://x.example/a")


def test_normalize_keeps_non_tracking_query():
    # x=1 is not a tracking param -> stays, so it's a different URL
    assert url_hash("https://x.example/a?x=1") != url_hash("https://x.example/a")


def test_content_hash_normalizes_whitespace():
    assert content_hash("a  b\n\n\nc") == content_hash("a b\n\nc")


# -- exclusion (configurable, more permissive default) -----------------------


def test_default_exclude_blocks_auth_and_commerce():
    pat = compile_exclude()
    for u in (
        "https://e.org/login",
        "https://e.org/cart",
        "https://e.org/checkout",
        "https://e.org/account",
        "https://facebook.com/x",
    ):
        assert is_excluded_url(u, pattern=pat), u


def test_default_exclude_allows_content_paths():
    # /about, /contact, /tag/, /category/, /author/ are no longer excluded
    pat = compile_exclude()
    for u in (
        "https://e.org/about",
        "https://e.org/contact",
        "https://e.org/tag/x",
        "https://e.org/category/y",
        "https://e.org/author/jane",
    ):
        assert not is_excluded_url(u, pattern=pat), u


def test_custom_exclude_overrides_default():
    pat = compile_exclude([r"/secret"])
    assert is_excluded_url("https://e.org/secret/page", pattern=pat)
    assert not is_excluded_url("https://e.org/login", pattern=pat)  # default frag not present


def test_empty_exclude_matches_nothing():
    pat = compile_exclude([])
    assert not is_excluded_url("https://e.org/login", pattern=pat)


def test_dedicated_user_agent_default():
    assert "LazyCrawler" in HTTPConfig().user_agent
    assert "Chrome" not in HTTPConfig().user_agent


# -- text extraction threshold (configurable, was hardcoded 200) -------------


def test_short_page_accepted_with_low_threshold():
    client = HTTPClient(HTTPConfig(min_text_chars=10))
    html = "<html><body><p>Short but valid documentation note.</p></body></html>"
    text = client._extract_text(html)
    assert text and "Short but valid" in text


def test_short_page_rejected_with_high_threshold():
    client = HTTPClient(HTTPConfig(min_text_chars=10_000))
    html = "<html><body><p>Tiny.</p></body></html>"
    assert client._extract_text(html) is None


def test_threshold_is_inclusive():
    # exactly min_text_chars characters should be accepted (>=, not >)
    client = HTTPClient(HTTPConfig(min_text_chars=20))
    body = "x" * 20
    html = f"<html><body><p>{body}</p></body></html>"
    assert client._extract_text(html) == body


# -- FetchResult --------------------------------------------------------------


def test_fetchresult_unpacks_as_triple():
    fr = FetchResult(html="<p>h</p>", text="h", status=200)
    html, text, status = fr
    assert (html, text, status) == ("<p>h</p>", "h", 200)


def test_decode_falls_back_on_unknown_charset():
    # Regression: an unknown/legacy charset token raised LookupError (not
    # suppressed by errors="replace"), turning a good 200 into a retried failure.
    body = "café résumé".encode("utf-8")
    assert HTTPClient._decode(body, "text/html; charset=x-user-defined") == "café résumé"
    assert HTTPClient._decode(body, "text/html; charset=none") == "café résumé"
    # A valid charset is still honored.
    assert HTTPClient._decode("ünïcode".encode("utf-8"), "text/html; charset=utf-8") == "ünïcode"


def test_decode_falls_back_on_a_registered_but_not_text_codec():
    """A real Codex-review finding: codecs.lookup() (used to pre-validate a
    declared charset) accepts bytes-to-bytes-only registry codecs like
    "base64" -- but bytes.decode("base64") itself then refuses with its OWN
    LookupError ("not a text encoding"). Pre-validation succeeding does not
    guarantee the actual decode call will; this must still fall through to
    sniffing rather than raise and lose an otherwise good 200 response."""
    body = "café résumé".encode("utf-8")
    assert HTTPClient._decode(body, "text/html; charset=base64") == "café résumé"


def test_decode_sniffs_the_real_charset_when_the_header_is_silent():
    """Measured live: a German site (goldesel.de) served Windows-1252 with
    no charset in its Content-Type header at all. The old code then always
    guessed utf-8, and errors="replace" silently turned every accented
    letter into "�" -- "Cloud wächst 45%" came back as "Cloud w�chst
    45%" in a real extracted page title, with no warning anywhere that
    anything had gone wrong."""
    corpo = "Alibaba: Cloud wächst 45%".encode("cp1252")
    assert HTTPClient._decode(corpo, "text/html") == "Alibaba: Cloud wächst 45%"


def test_decode_sniffs_charset_from_a_bare_content_type_with_no_charset_param():
    """The same silent-header case, but with a Content-Type present and just
    missing the charset parameter -- must not be treated as "charset=" found
    (it isn't) and must still fall through to sniffing, not a blind utf-8
    guess."""
    corpo = "Ökonomie größer wächst".encode("iso-8859-1")
    assert HTTPClient._decode(corpo, "text/html; boundary=something") == "Ökonomie größer wächst"


def test_decode_honors_the_pages_own_meta_charset_declaration():
    """A real Codex-review finding: byte 0x97 (an earlier version of this
    test used an em dash there) decodes to the SAME em dash under both
    windows-1252 AND windows-1250, so it never actually proved the meta
    declaration -- as opposed to generic statistical sniffing -- drove the
    result. Byte 0x9C is genuinely different between them (U+0153 "œ" vs
    U+015B "ś"): the SAME raw byte, declared two different ways, must
    decode two different (and each individually correct) ways --
    conclusive proof the <meta charset=...> declaration itself (what
    charset_normalizer's preemptive_behaviour looks for), not luck,
    determined the outcome."""
    corpo = bytes([0x9C])
    pagina_1252 = b'<html><head><meta charset="windows-1252"></head><body>' + corpo + b"</body></html>"
    pagina_1250 = b'<html><head><meta charset="windows-1250"></head><body>' + corpo + b"</body></html>"
    assert chr(0x153) in HTTPClient._decode(pagina_1252, "text/html")  # U+0153 "oe" ligature
    assert chr(0x15B) in HTTPClient._decode(pagina_1250, "text/html")  # U+015B "s" with acute


def test_decode_strips_quotes_around_a_declared_charset(monkeypatch):
    """A real Codex-review finding: charset="windows-1252" (quoted, valid
    per RFC 7231) was treated as an unknown codec name (the literal quote
    characters are not part of any codec name) and silently routed to
    sniffing instead of using the authoritative, explicitly-declared
    charset. Asserted two ways: the decoded text is correct, AND sniffing
    was never even invoked -- "Grüße" alone could otherwise pass this test
    for the wrong reason (sniffing guessing right by coincidence) without
    proving the quotes were actually stripped."""
    import charset_normalizer

    chiamato = {"n": 0}
    originale = charset_normalizer.from_bytes

    def _spia(*a, **k):
        chiamato["n"] += 1
        return originale(*a, **k)

    monkeypatch.setattr(charset_normalizer, "from_bytes", _spia)
    corpo = "Grüße".encode("windows-1252")
    assert HTTPClient._decode(corpo, 'text/html; charset="windows-1252"') == "Grüße"
    assert chiamato["n"] == 0


def test_decode_does_not_sniff_plausible_text_out_of_binary_content():
    """A real Codex-review finding: content_type is caller-supplied and can
    be wrong -- a binary payload mislabeled as text/html must not have
    from_bytes()'s fallback matching turn arbitrary bytes into a plausible-
    looking (but meaningless) string that then gets indexed as real page
    content. is_binary() (stricter than from_bytes() alone -- fallback
    matching disabled) gates this."""
    spazzatura = bytes(range(256)) * 4
    risultato = HTTPClient._decode(spazzatura, "text/html")
    # Not asserting an exact string (that's an implementation detail of the
    # utf-8-replace fallback) -- asserting it did NOT get treated as a
    # confident text match, i.e. it's dominated by replacement characters
    # rather than resembling coherent text.
    assert risultato.count("�") > len(risultato) / 4


def test_fetch_retries_then_succeeds(monkeypatch):
    client = HTTPClient(HTTPConfig(max_retries=3, backoff_base_sec=0, verify_ssl=False))
    calls = {"n": 0}

    class _Resp:
        status_code = 200
        headers = {"Content-Type": "text/html"}
        content = b"<html><body><p>" + b"ok " * 50 + b"</p></body></html>"
        text = "<html><body><p>" + "ok " * 50 + "</p></body></html>"
        is_redirect = False
        url = "https://e.org/x"

        def iter_content(self, chunk_size=0):
            yield self.content

        def close(self):
            pass

        def raise_for_status(self):
            pass

    def flaky_get(url, **kw):
        calls["n"] += 1
        if calls["n"] < 2:
            raise requests.ConnectionError("boom")
        return _Resp()

    monkeypatch.setattr(client._session, "get", flaky_get)
    fr = client.fetch("https://e.org/x")
    assert calls["n"] == 2
    assert fr.status == 200 and fr.text


def test_fetch_gives_up_after_max_retries(monkeypatch):
    client = HTTPClient(HTTPConfig(max_retries=2, backoff_base_sec=0, verify_ssl=False))

    def always_fail(url, **kw):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(client._session, "get", always_fail)
    fr = client.fetch("https://e.org/x")
    assert fr.html is None and fr.text is None and fr.status is None


def test_fetch_permanent_4xx_not_retried(monkeypatch):
    # A 404 (or any non-429 4xx) is terminal: exactly one attempt, status preserved.
    client = HTTPClient(HTTPConfig(max_retries=4, backoff_base_sec=0, verify_ssl=False))
    calls = {"n": 0}

    class _Resp:
        status_code = 404
        headers = {"Content-Type": "text/html"}
        content = b"not found"
        text = "not found"
        is_redirect = False

        def iter_content(self, chunk_size=0):
            yield self.content

        def close(self):
            pass

        def raise_for_status(self):  # should never be reached for 4xx now
            raise requests.HTTPError("404")

    def get(url, **kw):
        calls["n"] += 1
        return _Resp()

    monkeypatch.setattr(client._session, "get", get)
    fr = client.fetch("https://e.org/missing")
    assert calls["n"] == 1  # not retried
    assert fr.status == 404 and fr.html is None and fr.text is None


def test_fetch_429_is_retried(monkeypatch):
    # 429 stays retryable: it is attempted max_retries times before giving up.
    client = HTTPClient(HTTPConfig(max_retries=3, backoff_base_sec=0, verify_ssl=False))
    calls = {"n": 0}

    class _Resp:
        status_code = 429
        headers = {"Content-Type": "text/html"}
        content = b"slow down"
        text = "slow down"
        is_redirect = False

        def iter_content(self, chunk_size=0):
            yield self.content

        def close(self):
            pass

        def raise_for_status(self):
            pass

    def get(url, **kw):
        calls["n"] += 1
        return _Resp()

    monkeypatch.setattr(client._session, "get", get)
    fr = client.fetch("https://e.org/x")
    assert calls["n"] == 3  # retried up to max_retries
    assert fr.html is None and fr.status is None


# -- SSRF guard ---------------------------------------------------------------


def _fake_getaddrinfo(ip):
    def _gai(host, *a, **kw):
        return [(2, 1, 6, "", (ip, 0))]

    return _gai


def test_is_blocked_address_blocks_private(monkeypatch):
    from lazycrawler import http as http_mod
    from lazycrawler.http import is_blocked_address

    # loopback / link-local / RFC-1918 by resolved IP
    monkeypatch.setattr(http_mod.socket, "getaddrinfo", _fake_getaddrinfo("127.0.0.1"))
    assert is_blocked_address("http://anything.example/x")
    monkeypatch.setattr(http_mod.socket, "getaddrinfo", _fake_getaddrinfo("169.254.169.254"))
    assert is_blocked_address("http://metadata.example/latest/")
    monkeypatch.setattr(http_mod.socket, "getaddrinfo", _fake_getaddrinfo("10.0.0.5"))
    assert is_blocked_address("http://internal.example/")


def test_is_blocked_address_string_hosts():
    from lazycrawler.http import is_blocked_address

    # these short-circuit before DNS
    assert is_blocked_address("http://localhost/x")
    assert is_blocked_address("http://foo.local/x")
    assert is_blocked_address("http://metadata.google.internal/")


def test_is_blocked_address_allows_public(monkeypatch):
    from lazycrawler import http as http_mod
    from lazycrawler.http import is_blocked_address

    monkeypatch.setattr(http_mod.socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34"))
    assert not is_blocked_address("https://example.com/")


def test_fetch_blocks_private_when_guard_on(monkeypatch):
    client = HTTPClient(HTTPConfig(block_private_addresses=True, verify_ssl=False))

    def boom(url, **kw):
        raise AssertionError("network must not be touched when SSRF-blocked")

    monkeypatch.setattr(client._session, "get", boom)
    fr = client.fetch("http://127.0.0.1/admin")
    assert fr.html is None and fr.text is None and fr.status is None


def test_fetch_blocks_redirect_to_private(monkeypatch):
    # A public host that 30x-redirects to a private address must NOT be followed.
    from lazycrawler import http as http_mod

    client = HTTPClient(HTTPConfig(block_private_addresses=True, verify_ssl=False))

    def fake_blocked(u):
        return any(p in u for p in ("127.0.0.1", "169.254", "10.0.0"))

    monkeypatch.setattr(http_mod, "is_blocked_address", fake_blocked)

    class _Redir:
        status_code = 302
        is_redirect = True
        headers = {"Location": "http://127.0.0.1/admin", "Content-Type": "text/html"}
        url = "https://public.example/redirect"

        def close(self):
            pass

    monkeypatch.setattr(client._session, "get", lambda url, **kw: _Redir())
    fr = client.fetch("https://public.example/redirect")
    assert fr.html is None and fr.status is None  # redirect to private not followed


def test_fetch_caps_html_bytes(monkeypatch):
    client = HTTPClient(HTTPConfig(verify_ssl=False, max_html_bytes=100))
    big = b"<html><body>" + b"a" * 10000 + b"</body></html>"

    class _Resp:
        status_code = 200
        is_redirect = False
        headers = {"Content-Type": "text/html"}
        url = "https://e.org/big"

        def iter_content(self, chunk_size=0):
            for i in range(0, len(big), 32):
                yield big[i : i + 32]

        def close(self):
            pass

    monkeypatch.setattr(client._session, "get", lambda url, **kw: _Resp())
    fr = client.fetch("https://e.org/big")
    assert fr.html is not None
    assert len(fr.html.encode("utf-8", "replace")) <= 100  # body hard-capped


def test_fetch_returns_pdf_bytes_by_content_type(monkeypatch):
    client = HTTPClient(HTTPConfig(verify_ssl=False))

    class _Resp:
        status_code = 200
        headers = {"Content-Type": "application/pdf"}
        content = b"%PDF-1.7 minimal"
        text = "garbage"
        is_redirect = False
        url = "https://e.org/doc"

        def iter_content(self, chunk_size=0):
            yield self.content

        def close(self):
            pass

        def raise_for_status(self):
            pass

    monkeypatch.setattr(client._session, "get", lambda url, **kw: _Resp())
    fr = client.fetch("https://e.org/doc")
    assert fr.content == b"%PDF-1.7 minimal"
    assert fr.html is None  # text extraction skipped for PDFs


def test_fetch_skips_indexing_binary_content_mislabeled_as_html(monkeypatch):
    """A real Codex-review finding: content_type is the server's own header
    and can simply be wrong. Decoding a binary payload with
    errors="replace" still hands fetch()'s caller A string -- with nothing
    to say it isn't real page text, it would otherwise reach
    _extract_text() and be indexed as if it were. html/text must stay None,
    the same 'nothing usable' shape a permanent HTTP error already returns."""
    client = HTTPClient(HTTPConfig(verify_ssl=False))
    spazzatura = bytes(range(256)) * 4

    class _Resp:
        status_code = 200
        headers = {"Content-Type": "text/html"}
        content = spazzatura
        text = "garbage"
        is_redirect = False
        url = "https://e.org/blob"

        def iter_content(self, chunk_size=0):
            yield self.content

        def close(self):
            pass

        def raise_for_status(self):
            pass

    monkeypatch.setattr(client._session, "get", lambda url, **kw: _Resp())
    fr = client.fetch("https://e.org/blob")
    assert fr.status == 200
    assert fr.html is None
    assert fr.text is None


def test_fetch_trusts_a_declared_charset_over_the_binary_heuristic(monkeypatch):
    """A real Codex-review finding: is_binary() is a heuristic and can
    misfire on legitimate but noisy/legacy text -- it must never be
    consulted (let alone override) when the server has already told us,
    explicitly, how to decode the body. Forces is_binary() to say "yes,
    this is binary" and confirms an explicitly declared charset still
    wins -- the page is decoded, not dropped."""
    client = HTTPClient(HTTPConfig(verify_ssl=False))
    monkeypatch.setattr(HTTPClient, "_looks_binary", staticmethod(lambda body: True))

    class _Resp:
        status_code = 200
        headers = {"Content-Type": "text/html; charset=windows-1252"}
        content = "Grüße".encode("windows-1252")
        text = "garbage"
        is_redirect = False
        url = "https://e.org/page"

        def iter_content(self, chunk_size=0):
            yield self.content

        def close(self):
            pass

        def raise_for_status(self):
            pass

    monkeypatch.setattr(client._session, "get", lambda url, **kw: _Resp())
    fr = client.fetch("https://e.org/page")
    assert fr.html == "Grüße"


def test_fetch_still_gates_binary_content_declared_with_a_bogus_charset(monkeypatch):
    """A real Codex-review finding: a registered-but-not-text codec
    (charset=base64) used to make fetch()'s own binary gate look
    "authoritative" and skip is_binary() entirely, even for genuinely
    binary content -- the bogus declaration itself is never actually
    decode-usable, so it must not be trusted as if it were. The declared
    codec being bogus is exactly the case fetch()'s binary gate must still
    catch; html/text must stay None, not fall through to a UTF-8-replaced
    garbage string that reaches _extract_text()."""
    client = HTTPClient(HTTPConfig(verify_ssl=False))
    spazzatura = bytes(range(256)) * 4

    class _Resp:
        status_code = 200
        headers = {"Content-Type": "text/html; charset=base64"}
        content = spazzatura
        text = "garbage"
        is_redirect = False
        url = "https://e.org/blob"

        def iter_content(self, chunk_size=0):
            yield self.content

        def close(self):
            pass

        def raise_for_status(self):
            pass

    monkeypatch.setattr(client._session, "get", lambda url, **kw: _Resp())
    fr = client.fetch("https://e.org/blob")
    assert fr.html is None
    assert fr.text is None


# -- robots --------------------------------------------------------------------


class _FakeHTTP:
    """Minimal HTTPClient stand-in for RobotsChecker that counts robots fetches."""

    def __init__(self, body):
        self.body = body
        self.calls = 0

    def get_text(self, url):
        self.calls += 1
        return self.body


def test_robots_disallow_and_allow():
    body = "User-agent: *\nDisallow: /private\n"
    rc = RobotsChecker(_FakeHTTP(body), "LazyCrawler")
    assert rc.allowed("https://e.org/public")
    assert not rc.allowed("https://e.org/private/x")


def test_robots_fetched_once_per_host():
    fake = _FakeHTTP("User-agent: *\nDisallow:\n")
    rc = RobotsChecker(fake, "LazyCrawler")
    for _ in range(5):
        rc.allowed("https://e.org/page")
    assert fake.calls == 1  # cached after the first fetch (no TOCTOU double-fetch)


def test_robots_crawl_delay_parsed():
    fake = _FakeHTTP("User-agent: *\nCrawl-delay: 3\n")
    rc = RobotsChecker(fake, "LazyCrawler")
    assert rc.crawl_delay("https://e.org/x") == 3.0


def test_default_exclude_patterns_dropped_content_paths():
    joined = " ".join(DEFAULT_EXCLUDE_PATTERNS)
    for frag in ("/about", "/contact", "/tag/", "/category/", "/author/"):
        assert frag not in joined
