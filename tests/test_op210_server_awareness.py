"""Tests for server-aware client construction and dispatch (OP#210).

OP#209 introduced the ``proxmox_servers`` data model but left every reader
resolving to the first server. This covers the layer that actually picks the
*owning* server for a given host — the point at which two Proxmox servers stop
being indistinguishable.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml


def _two_servers(config_file, hosts=None):
    """Two servers whose nodes are both named 'pve' — the ambiguity to resolve."""
    config_file.write_text(
        yaml.dump(
            {
                "proxmox_servers": [
                    {"id": "srv-a", "url": "https://a.lan:8006", "verify_ssl": True},
                    {"id": "srv-b", "url": "https://b.lan:8006", "verify_ssl": False},
                ],
                "hosts": hosts or [],
            }
        )
    )


# ---------------------------------------------------------------------------
# Token assembly — one place only
# ---------------------------------------------------------------------------


def test_assemble_token_prefers_token_id_and_secret():
    from app.proxmox_client import assemble_token

    assert assemble_token({"token_id": "root@pam!keepup", "secret": "s"}) == (
        "root@pam!keepup=s"
    )


def test_assemble_token_falls_back_to_legacy_api_user():
    from app.proxmox_client import assemble_token

    assert assemble_token({"api_user": "root@pam", "api_token": "t"}) == "root@pam!t"


def test_assemble_token_bare_api_token():
    from app.proxmox_client import assemble_token

    assert assemble_token({"api_token": "t"}) == "t"


def test_assemble_token_empty_when_nothing_stored():
    from app.proxmox_client import assemble_token

    assert assemble_token({}) == ""


# ---------------------------------------------------------------------------
# client_from_config(server_id)
# ---------------------------------------------------------------------------


def test_client_from_config_builds_the_named_server(config_file, data_dir):
    from app.credentials import save_integration_credentials
    from app.proxmox_client import client_from_config

    _two_servers(config_file)
    save_integration_credentials("proxmox_srv-a", token_id="a@pam!k", secret="sa")
    save_integration_credentials("proxmox_srv-b", token_id="b@pam!k", secret="sb")

    client = client_from_config("srv-b")

    assert client.base == "https://b.lan:8006"
    assert client.headers["Authorization"] == "PVEAPIToken=b@pam!k=sb"
    assert client._verify_ssl is False


def test_client_from_config_without_id_uses_first_server(config_file, data_dir):
    """No id given: the OP#209 shim still resolves, for admin/wizard callers."""
    from app.credentials import save_integration_credentials
    from app.proxmox_client import client_from_config

    _two_servers(config_file)
    save_integration_credentials("proxmox_srv-a", token_id="a@pam!k", secret="sa")

    assert client_from_config().base == "https://a.lan:8006"


def test_client_from_config_unknown_server_raises(config_file, data_dir):
    from app.proxmox_client import client_from_config

    _two_servers(config_file)
    with pytest.raises(RuntimeError, match="not configured"):
        client_from_config("nope")


# ---------------------------------------------------------------------------
# host_ops: the owning server
# ---------------------------------------------------------------------------


def test_build_proxmox_client_resolves_the_hosts_server(config_file, data_dir):
    """THE ambiguity: both servers have a node called 'pve'."""
    import asyncio

    from app.credentials import save_integration_credentials
    from app.host_ops import build_proxmox_client

    _two_servers(config_file)
    save_integration_credentials("proxmox_srv-a", token_id="a@pam!k", secret="sa")
    save_integration_credentials("proxmox_srv-b", token_id="b@pam!k", secret="sb")

    host_a = {"host": "10.0.0.1", "proxmox_node": "pve", "proxmox_vmid": 100,
              "proxmox_server": "srv-a"}
    host_b = {"host": "10.0.0.2", "proxmox_node": "pve", "proxmox_vmid": 100,
              "proxmox_server": "srv-b"}

    assert asyncio.run(build_proxmox_client(host_a)).base == "https://a.lan:8006"
    assert asyncio.run(build_proxmox_client(host_b)).base == "https://b.lan:8006"


def test_lxc_ssh_context_uses_owning_server(config_file, data_dir):
    from app.credentials import save_integration_credentials
    from app.host_ops import _lxc_ssh_context

    _two_servers(config_file)
    save_integration_credentials("proxmox_srv-b", ssh_user="pveadmin", ssh_password="pw")

    host = {"host": "10.0.0.2", "proxmox_node": "pve", "proxmox_vmid": 100,
            "proxmox_server": "srv-b"}
    ssh_host, ssh_creds = _lxc_ssh_context(host)

    assert ssh_host == "b.lan"
    assert ssh_creds["user"] == "pveadmin"
    assert ssh_creds["ssh_password"] == "pw"


def test_node_ssh_context_uses_owning_server(config_file, data_dir):
    from app.credentials import save_integration_credentials
    from app.host_ops import _node_ssh_context

    _two_servers(config_file)
    save_integration_credentials("proxmox_srv-b", ssh_user="pveadmin", ssh_password="pw")

    host = {"name": "Node B", "host": "10.0.0.2", "proxmox_node": "pve",
            "proxmox_server": "srv-b"}
    host_entry, ssh_creds = _node_ssh_context(host)

    # A node targets its own address, not the integration URL's host.
    assert host_entry["host"] == "10.0.0.2"
    assert host_entry["user"] == "pveadmin"
    assert ssh_creds["ssh_password"] == "pw"


def test_unmigrated_host_without_server_id_still_resolves(config_file, data_dir):
    """A host predating the migration has no proxmox_server — fall back, don't fail."""
    from app.credentials import save_integration_credentials
    from app.host_ops import _lxc_ssh_context

    _two_servers(config_file)
    save_integration_credentials("proxmox_srv-a", ssh_user="root")

    host = {"host": "10.0.0.1", "proxmox_node": "pve", "proxmox_vmid": 100}
    ssh_host, ssh_creds = _lxc_ssh_context(host)

    assert ssh_host == "a.lan"
    assert ssh_creds["user"] == "root"


# ---------------------------------------------------------------------------
# Missing server degrades to plain instead of crashing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_os_update_missing_server_degrades_to_plain(config_file, data_dir):
    """A hand-edited config can reference a server that no longer exists."""
    from app.host_ops import run_os_update

    _two_servers(config_file)
    host = {"slug": "ghost", "name": "Ghost", "host": "10.0.0.9", "user": "root",
            "proxmox_node": "pve", "proxmox_vmid": 100, "proxmox_server": "gone"}

    with patch(
        "app.host_ops.run_host_update_buffered", new=AsyncMock(return_value=["ok"])
    ) as ssh, patch("app.host_ops.build_proxmox_client") as client:
        lines = await run_os_update(host, {"ssh_password": "p"})

    assert lines == ["ok"]
    client.assert_not_called()
    ssh.assert_awaited_once_with(host, {"ssh_password": "p"})


@pytest.mark.asyncio
async def test_reboot_typed_missing_server_degrades_to_plain(config_file, data_dir):
    from app.host_ops import reboot_host_typed

    _two_servers(config_file)
    host = {"slug": "ghost", "host": "10.0.0.9", "user": "root",
            "proxmox_node": "pve", "proxmox_vmid": None, "proxmox_server": "gone"}

    with patch(
        "app.host_ops.reboot_host", new=AsyncMock(return_value=["rebooting"])
    ) as ssh, patch("app.host_ops.build_proxmox_client") as client:
        lines = await reboot_host_typed(host, {})

    assert lines == ["rebooting"]
    client.assert_not_called()
    ssh.assert_awaited_once()


@pytest.mark.asyncio
async def test_reboot_required_missing_server_degrades_to_plain(config_file, data_dir):
    from app.host_ops import reboot_required_typed

    _two_servers(config_file)
    host = {"slug": "ghost", "host": "10.0.0.9", "user": "root",
            "proxmox_node": "pve", "proxmox_vmid": None, "proxmox_server": "gone"}

    with patch(
        "app.host_ops.check_host_updates",
        new=AsyncMock(return_value={"reboot_required": True}),
    ), patch("app.host_ops.build_proxmox_client") as client:
        assert await reboot_required_typed(host, {}) is True

    client.assert_not_called()


def test_resolve_kind_keeps_proxmox_kind_when_server_exists(config_file, data_dir):
    from app.host_ops import resolve_kind

    _two_servers(config_file)
    host = {"proxmox_node": "pve", "proxmox_vmid": 100, "proxmox_server": "srv-a"}
    assert resolve_kind(host) == "lxc"


def test_resolve_kind_plain_host_is_untouched(config_file, data_dir):
    from app.host_ops import resolve_kind

    _two_servers(config_file)
    assert resolve_kind({"host": "1.2.3.4", "user": "root"}) == "plain"


# ---------------------------------------------------------------------------
# Dispatch still routes correctly with a valid server
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lxc_upgrade_dispatches_to_owning_server(config_file, data_dir):
    from app.credentials import save_integration_credentials
    from app.host_ops import run_os_update

    _two_servers(config_file)
    save_integration_credentials("proxmox_srv-b", ssh_user="root", ssh_password="pw")

    host = {"slug": "ct", "host": "10.0.0.2", "proxmox_node": "pve",
            "proxmox_vmid": 101, "proxmox_type": "lxc", "proxmox_server": "srv-b"}
    client = MagicMock()
    client.upgrade_lxc = AsyncMock(return_value=["upgraded"])

    with patch("app.host_ops.build_proxmox_client",
               new=AsyncMock(return_value=client)):
        lines = await run_os_update(host, {})

    assert lines == ["upgraded"]
    node, vmid, ssh_host, _ = client.upgrade_lxc.await_args.args
    assert (node, vmid, ssh_host) == ("pve", 101, "b.lan")
