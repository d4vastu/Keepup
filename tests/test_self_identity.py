"""Tests for app.self_identity."""

from app.self_identity import get_self_container_id, is_self_on_proxmox_node


def test_returns_none_when_hostname_absent(monkeypatch):
    monkeypatch.delenv("HOSTNAME", raising=False)
    assert get_self_container_id() is None


def test_returns_none_for_regular_hostname(monkeypatch):
    monkeypatch.setenv("HOSTNAME", "myserver")
    assert get_self_container_id() is None


def test_returns_none_for_partial_hex(monkeypatch):
    monkeypatch.setenv("HOSTNAME", "abc123")
    assert get_self_container_id() is None


def test_returns_none_for_uppercase_hex(monkeypatch):
    # Docker short IDs are always lowercase; uppercase is not a container ID.
    monkeypatch.setenv("HOSTNAME", "ABC123DEF456")
    assert get_self_container_id() is None


def test_returns_short_id_for_docker_hostname(monkeypatch):
    monkeypatch.setenv("HOSTNAME", "a1b2c3d4e5f6")
    assert get_self_container_id() == "a1b2c3d4e5f6"


def test_returns_none_for_13_char_hex(monkeypatch):
    monkeypatch.setenv("HOSTNAME", "a1b2c3d4e5f60")
    assert get_self_container_id() is None


def test_returns_none_for_11_char_hex(monkeypatch):
    monkeypatch.setenv("HOSTNAME", "a1b2c3d4e5f")
    assert get_self_container_id() is None


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
