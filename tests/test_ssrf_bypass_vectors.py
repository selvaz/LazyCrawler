# -*- coding: utf-8 -*-
"""
SSRF guard bypass-vector coverage (ecosystem stabilization plan Train B /
ECO-008), filling gaps left by test_http.py's existing suite: explicit IPv6
loopback, explicit RFC-1918 ranges beyond the one already covered, legacy
numeric-host forms, IPv4-mapped IPv6, credentials-in-URL host confusion, and
a multi-hop redirect chain.

``is_blocked_address`` resolves the host via ``socket.getaddrinfo`` and
classifies whatever IP comes back; these tests mock that resolution so the
classification logic is verified deterministically, independent of whether
this platform's resolver happens to accept a given literal host form.
"""

from __future__ import annotations

import pytest

from lazycrawler import http as http_mod
from lazycrawler.config import HTTPConfig
from lazycrawler.http import HTTPClient, is_blocked_address


def _fake_getaddrinfo(ip: str, family: int = 2):
    def _gai(host, *a, **kw):
        return [(family, 1, 6, "", (ip, 0) if family == 2 else (ip, 0, 0, 0))]

    return _gai


# -- explicit address classes --------------------------------------------


def test_blocks_ipv6_loopback(monkeypatch):
    monkeypatch.setattr(http_mod.socket, "getaddrinfo", _fake_getaddrinfo("::1", family=10))
    assert is_blocked_address("http://anything.example/x")


def test_blocks_rfc1918_172_range(monkeypatch):
    monkeypatch.setattr(http_mod.socket, "getaddrinfo", _fake_getaddrinfo("172.16.5.1"))
    assert is_blocked_address("http://internal.example/")


def test_blocks_rfc1918_192_168_range(monkeypatch):
    monkeypatch.setattr(http_mod.socket, "getaddrinfo", _fake_getaddrinfo("192.168.1.1"))
    assert is_blocked_address("http://internal.example/")


def test_blocks_ipv4_mapped_ipv6_loopback(monkeypatch):
    # ::ffff:127.0.0.1 -- classic IPv4-mapped-IPv6 bypass of naive IPv4-only checks.
    monkeypatch.setattr(
        http_mod.socket, "getaddrinfo", _fake_getaddrinfo("::ffff:127.0.0.1", family=10)
    )
    assert is_blocked_address("http://anything.example/x")


def test_blocks_ipv4_mapped_ipv6_private(monkeypatch):
    monkeypatch.setattr(
        http_mod.socket, "getaddrinfo", _fake_getaddrinfo("::ffff:10.0.0.5", family=10)
    )
    assert is_blocked_address("http://internal.example/")


# -- legacy numeric host forms (classic inet_aton-style SSRF bypasses) ----
# These simulate a resolver/stack that accepts the literal as an IPv4
# address and resolves it to loopback -- our classification must still
# catch whatever IP getaddrinfo hands back, regardless of the host string
# that produced it.


def test_blocks_ipv4_as_decimal_integer_host(monkeypatch):
    # http://2130706433/ == http://127.0.0.1/ under legacy inet_aton parsing.
    monkeypatch.setattr(http_mod.socket, "getaddrinfo", _fake_getaddrinfo("127.0.0.1"))
    assert is_blocked_address("http://2130706433/")


def test_blocks_ipv4_as_hex_host(monkeypatch):
    # http://0x7f000001/ == http://127.0.0.1/ under legacy inet_aton parsing.
    monkeypatch.setattr(http_mod.socket, "getaddrinfo", _fake_getaddrinfo("127.0.0.1"))
    assert is_blocked_address("http://0x7f000001/")


# -- credentials in URL: host must be the real target, not the userinfo ---


def test_credentials_do_not_hide_the_real_private_host(monkeypatch):
    monkeypatch.setattr(http_mod.socket, "getaddrinfo", _fake_getaddrinfo("127.0.0.1"))
    assert is_blocked_address("http://user:pass@127.0.0.1:8080/admin")


def test_userinfo_lookalike_host_does_not_fool_the_guard(monkeypatch):
    # The userinfo looks like a public hostname; the real (private) host
    # after '@' must be what gets resolved and blocked.
    monkeypatch.setattr(http_mod.socket, "getaddrinfo", _fake_getaddrinfo("10.0.0.5"))
    assert is_blocked_address("http://public-looking-name.example@10.0.0.5/")


def test_credentials_do_not_block_a_genuinely_public_host(monkeypatch):
    monkeypatch.setattr(http_mod.socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34"))
    assert not is_blocked_address("http://user:pass@example.com/")


# -- multi-hop redirect chain: every hop re-validated ----------------------


def test_fetch_blocks_multi_hop_redirect_to_private(monkeypatch):
    """public -> public -> private: the guard must catch the LAST hop, proving
    each hop is re-validated rather than only the first/last one checked."""
    client = HTTPClient(HTTPConfig(allow_private_networks=False, verify_ssl=False))

    hops = {
        "https://public-a.example/start": "https://public-b.example/next",
        "https://public-b.example/next": "http://127.0.0.1/admin",
    }
    visited = []

    def fake_blocked(u):
        return "127.0.0.1" in u

    monkeypatch.setattr(http_mod, "is_blocked_address", fake_blocked)

    class _Redir:
        def __init__(self, location):
            self.status_code = 302
            self.is_redirect = True
            self.headers = {"Location": location, "Content-Type": "text/html"}

        def close(self):
            pass

    def fake_get(url, **kw):
        visited.append(url)
        if url not in hops:
            pytest.fail(f"unexpected hop visited: {url}")
        return _Redir(hops[url])

    monkeypatch.setattr(client._session, "get", fake_get)
    fr = client.fetch("https://public-a.example/start")

    assert fr.html is None and fr.status is None  # final private hop blocked
    # both public hops were actually traversed (guard re-checked per hop,
    # not just validated once up front against the seed URL)
    assert visited == ["https://public-a.example/start", "https://public-b.example/next"]
