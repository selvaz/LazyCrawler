# -*- coding: utf-8 -*-
"""
Tests for the block_private_addresses -> allow_private_networks migration
(LazyCrawler 0.14.1, ecosystem stabilization plan Train B / ECO-008).

0.14.1 keeps today's default behavior (private networks reachable) but adds
the new, inverse-polarity flag and starts warning so callers can migrate
before 0.15.0 flips the default to blocked.
"""

import warnings

import pytest

from lazycrawler.config import HTTPConfig


def test_default_construction_is_unguarded_and_warns():
    with pytest.warns(DeprecationWarning, match="allow_private_networks"):
        cfg = HTTPConfig()
    assert cfg.block_private_addresses is False
    assert cfg.allow_private_networks is True


def test_old_flag_true_is_deprecated_but_still_works():
    with pytest.warns(DeprecationWarning, match="block_private_addresses=True"):
        cfg = HTTPConfig(block_private_addresses=True)
    assert cfg.block_private_addresses is True
    assert cfg.allow_private_networks is False


def test_new_flag_false_blocks_without_warning():
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        cfg = HTTPConfig(allow_private_networks=False)
    assert cfg.allow_private_networks is False
    assert cfg.block_private_addresses is True


def test_new_flag_true_allows_without_warning():
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        cfg = HTTPConfig(allow_private_networks=True)
    assert cfg.allow_private_networks is True
    assert cfg.block_private_addresses is False


def test_new_flag_takes_precedence_over_old_flag():
    # Conflicting old/new values: allow_private_networks wins, no crash.
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        cfg = HTTPConfig(block_private_addresses=False, allow_private_networks=False)
    assert cfg.allow_private_networks is False
    assert cfg.block_private_addresses is True
