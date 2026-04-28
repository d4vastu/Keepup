"""Tests for app.self_identity."""

from io import StringIO
from unittest.mock import patch

from app.self_identity import (
    _container_id_from_cgroup,
    get_self_container_id,
    get_self_container_name,
    is_self_on_proxmox_node,
)

_PATCH_CGROUP = patch("app.self_identity._container_id_from_cgroup", return_value=None)

# ---------------------------------------------------------------------------
# get_self_container_id — HOSTNAME path
# ---------------------------------------------------------------------------


def test_returns_none_when_hostname_absent(monkeypatch):
    monkeypatch.delenv("HOSTNAME", raising=False)
    with _PATCH_CGROUP:
        assert get_self_container_id() is None


def test_returns_none_for_regular_hostname(monkeypatch):
    monkeypatch.setenv("HOSTNAME", "myserver")
    with _PATCH_CGROUP:
        assert get_self_container_id() is None


def test_returns_none_for_partial_hex(monkeypatch):
    monkeypatch.setenv("HOSTNAME", "abc123")
    with _PATCH_CGROUP:
        assert get_self_container_id() is None


def test_returns_none_for_uppercase_hex(monkeypatch):
    # Docker short IDs are always lowercase; uppercase is not a container ID.
    monkeypatch.setenv("HOSTNAME", "ABC123DEF456")
    with _PATCH_CGROUP:
        assert get_self_container_id() is None


def test_returns_short_id_for_docker_hostname(monkeypatch):
    monkeypatch.setenv("HOSTNAME", "a1b2c3d4e5f6")
    assert get_self_container_id() == "a1b2c3d4e5f6"


def test_returns_none_for_13_char_hex(monkeypatch):
    monkeypatch.setenv("HOSTNAME", "a1b2c3d4e5f60")
    with _PATCH_CGROUP:
        assert get_self_container_id() is None


def test_returns_none_for_11_char_hex(monkeypatch):
    monkeypatch.setenv("HOSTNAME", "a1b2c3d4e5f")
    with _PATCH_CGROUP:
        assert get_self_container_id() is None


# ---------------------------------------------------------------------------
# _container_id_from_cgroup
# ---------------------------------------------------------------------------

# 64-char lowercase hex container ID, short form = "abc123def456"
_CONTAINER_LONG_ID = "abc123def4567890abcdef12345678901234567890abcdef1234567890abcdef"

_CGROUP_V1 = (
    f"12:cpuset:/docker/{_CONTAINER_LONG_ID}\n"
    f"11:memory:/docker/{_CONTAINER_LONG_ID}\n"
)

_CGROUP_V2 = f"0::/system.slice/docker-{_CONTAINER_LONG_ID}.scope\n"


def test_cgroup_v1_returns_short_id():
    with patch("builtins.open", return_value=StringIO(_CGROUP_V1)):
        result = _container_id_from_cgroup()
    assert result == "abc123def456"


def test_cgroup_v2_returns_short_id():
    with patch("builtins.open", return_value=StringIO(_CGROUP_V2)):
        result = _container_id_from_cgroup()
    assert result == "abc123def456"


def test_cgroup_no_id_returns_none():
    with patch("builtins.open", return_value=StringIO("0::/\n")):
        result = _container_id_from_cgroup()
    assert result is None


def test_cgroup_oserror_returns_none():
    with patch("builtins.open", side_effect=OSError("no such file")):
        result = _container_id_from_cgroup()
    assert result is None


# ---------------------------------------------------------------------------
# get_self_container_id — cgroup fallback path
# ---------------------------------------------------------------------------


def test_cgroup_fallback_used_when_hostname_is_plain(monkeypatch):
    """When HOSTNAME is not a hex ID, the cgroup fallback should provide the ID."""
    monkeypatch.setenv("HOSTNAME", "keepup")
    with patch(
        "app.self_identity._container_id_from_cgroup",
        return_value="aabbccddeeff",
    ):
        assert get_self_container_id() == "aabbccddeeff"


def test_cgroup_fallback_returns_none_outside_docker(monkeypatch):
    """Bare-metal: both HOSTNAME and cgroup return nothing → None."""
    monkeypatch.setenv("HOSTNAME", "homelab")
    with patch("app.self_identity._container_id_from_cgroup", return_value=None):
        assert get_self_container_id() is None


# ---------------------------------------------------------------------------
# get_self_container_name
# ---------------------------------------------------------------------------


def test_container_name_from_env(monkeypatch):
    monkeypatch.setenv("KEEPUP_CONTAINER_NAME", "keepup")
    assert get_self_container_name() == "keepup"


def test_container_name_strips_whitespace(monkeypatch):
    monkeypatch.setenv("KEEPUP_CONTAINER_NAME", "  keepup  ")
    assert get_self_container_name() == "keepup"


def test_container_name_returns_none_when_unset(monkeypatch):
    monkeypatch.delenv("KEEPUP_CONTAINER_NAME", raising=False)
    assert get_self_container_name() is None


def test_container_name_returns_none_for_empty_string(monkeypatch):
    monkeypatch.setenv("KEEPUP_CONTAINER_NAME", "")
    assert get_self_container_name() is None


# ---------------------------------------------------------------------------
# is_self_on_proxmox_node
# ---------------------------------------------------------------------------


def test_is_self_on_proxmox_node_env_not_set(monkeypatch):
    monkeypatch.delenv("KEEPUP_PROXMOX_NODE", raising=False)
    assert is_self_on_proxmox_node("pve") is False


def test_is_self_on_proxmox_node_matches(monkeypatch):
    monkeypatch.setenv("KEEPUP_PROXMOX_NODE", "pve")
    assert is_self_on_proxmox_node("pve") is True


def test_is_self_on_proxmox_node_no_match(monkeypatch):
    monkeypatch.setenv("KEEPUP_PROXMOX_NODE", "pve")
    assert is_self_on_proxmox_node("pve2") is False


def test_is_self_on_proxmox_node_empty_env(monkeypatch):
    monkeypatch.setenv("KEEPUP_PROXMOX_NODE", "")
    assert is_self_on_proxmox_node("pve") is False


def test_is_self_on_proxmox_node_case_sensitive(monkeypatch):
    monkeypatch.setenv("KEEPUP_PROXMOX_NODE", "PVE")
    assert is_self_on_proxmox_node("pve") is False
