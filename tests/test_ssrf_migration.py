# -*- coding: utf-8 -*-
"""
Tests for the block_private_addresses -> allow_private_networks migration
(ecosystem stabilization plan Train B / ECO-008).

- 0.14.1 kept the historical default (private networks reachable) and only
  warned to prepare callers for the flip.
- 0.15.0 (this file, post-flip) blocks private networks by DEFAULT. The
  deprecated field still works but only warns on the path that now matters:
  explicitly disabling the guard via ``block_private_addresses=False``.
"""

import warnings

import pytest

from lazycrawler.config import HTTPConfig


def test_default_construction_is_guarded_and_silent():
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        cfg = HTTPConfig()
    assert cfg.block_private_addresses is True
    assert cfg.allow_private_networks is False


def test_old_flag_true_is_redundant_but_silent():
    # Matches the new default -> no need to nag; the safe path never warns.
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        cfg = HTTPConfig(block_private_addresses=True)
    assert cfg.block_private_addresses is True
    assert cfg.allow_private_networks is False


def test_old_flag_false_is_deprecated_but_still_works():
    with pytest.warns(DeprecationWarning, match="allow_private_networks=True"):
        cfg = HTTPConfig(block_private_addresses=False)
    assert cfg.block_private_addresses is False
    assert cfg.allow_private_networks is True


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
        cfg = HTTPConfig(block_private_addresses=True, allow_private_networks=True)
    assert cfg.allow_private_networks is True
    assert cfg.block_private_addresses is False
