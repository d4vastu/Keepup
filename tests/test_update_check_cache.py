"""Tests for update_check_cache: TTL-based apt-update skip logic."""

import time
from unittest.mock import patch

from app import update_check_cache


def setup_function():
    update_check_cache.clear()


def test_cache_empty_returns_false():
    assert update_check_cache.is_cache_fresh("host1", 15) is False


def test_cache_fresh_within_ttl():
    update_check_cache.mark_refreshed("host1")
    assert update_check_cache.is_cache_fresh("host1", 15) is True


def test_cache_stale_outside_ttl():
    update_check_cache.mark_refreshed("host1")
    # Simulate time passing: 16 minutes ago
    with patch("app.update_check_cache.time.time", return_value=time.time() + 16 * 60):
        assert update_check_cache.is_cache_fresh("host1", 15) is False


def test_cache_per_key_isolation():
    update_check_cache.mark_refreshed("host1")
    assert update_check_cache.is_cache_fresh("host2", 15) is False


def test_ttl_zero_disables_cache():
    update_check_cache.mark_refreshed("host1")
    assert update_check_cache.is_cache_fresh("host1", 0) is False


def test_clear_removes_all_entries():
    update_check_cache.mark_refreshed("host1")
    update_check_cache.mark_refreshed("host2")
    update_check_cache.clear()
    assert update_check_cache.is_cache_fresh("host1", 15) is False
    assert update_check_cache.is_cache_fresh("host2", 15) is False
