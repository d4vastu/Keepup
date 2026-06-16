"""Tests for app.host_ops — type-aware OS-update / reboot dispatch.

These cover the regression behind OP#176: the auto-update path must use the
same per-host-type logic as the manual path (Proxmox node reboots through the
graceful API, LXCs upgraded via `pct exec`, plain/VM hosts over direct SSH).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# classify_host
# ---------------------------------------------------------------------------


def test_classify_host_plain():
    from app.host_ops import classify_host

    assert classify_host({"host": "1.2.3.4"}) == "plain"


def test_classify_host_node():
    from app.host_ops import classify_host

    assert classify_host({"proxmox_node": "pve", "proxmox_vmid": None}) == "node"


def test_classify_host_lxc():
    from app.host_ops import classify_host

    host = {"proxmox_node": "pve", "proxmox_vmid": 101, "proxmox_type": "lxc"}
    assert classify_host(host) == "lxc"


def test_classify_host_lxc_defaults_when_type_absent():
    from app.host_ops import classify_host

    # A Proxmox guest with a vmid but no explicit type is treated as an LXC.
    host = {"proxmox_node": "pve", "proxmox_vmid": 101}
    assert classify_host(host) == "lxc"


def test_classify_host_vm():
    from app.host_ops import classify_host

    host = {"proxmox_node": "pve", "proxmox_vmid": 200, "proxmox_type": "vm"}
    assert classify_host(host) == "vm"


# ---------------------------------------------------------------------------
# run_os_update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_os_update_plain_uses_ssh():
    from app.host_ops import run_os_update

    host = {"slug": "web", "host": "1.2.3.4", "user": "root"}
    creds = {"ssh_key": "k"}
    with patch(
        "app.host_ops.run_host_update_buffered",
        new=AsyncMock(return_value=["done"]),
    ) as mock_ssh:
        lines = await run_os_update(host, creds)

    assert lines == ["done"]
    mock_ssh.assert_awaited_once_with(host, creds)


@pytest.mark.asyncio
async def test_run_os_update_node_uses_ssh():
    """A Proxmox node is a Debian host — its OS upgrade runs over SSH, same as manual."""
    from app.host_ops import run_os_update

    host = {"slug": "pve", "host": "1.2.3.4", "user": "root",
            "proxmox_node": "pve", "proxmox_vmid": None}
    with patch(
        "app.host_ops.run_host_update_buffered",
        new=AsyncMock(return_value=["node upgraded"]),
    ) as mock_ssh:
        lines = await run_os_update(host, {})

    assert lines == ["node upgraded"]
    mock_ssh.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_os_update_lxc_uses_pct_not_ssh():
    """LXC upgrades go through pct exec (Proxmox API helper), never direct SSH."""
    from app.host_ops import run_os_update

    host = {"slug": "ct", "proxmox_node": "pve", "proxmox_vmid": 101,
            "proxmox_type": "lxc"}

    client = MagicMock()
    client.upgrade_lxc = AsyncMock(return_value=["lxc upgraded"])

    with patch("app.host_ops.run_host_update_buffered", new=AsyncMock()) as mock_ssh, \
         patch("app.host_ops.build_proxmox_client", new=AsyncMock(return_value=client)), \
         patch("app.host_ops.get_proxmox_config",
               return_value={"url": "https://pve.example:8006"}), \
         patch("app.host_ops.get_integration_credentials",
               return_value={"ssh_user": "root", "ssh_key": "id_ed25519", "ssh_port": 22}):
        lines = await run_os_update(host, {})

    assert lines == ["lxc upgraded"]
    mock_ssh.assert_not_called()
    # node, vmid, ssh_host derived from the proxmox URL hostname
    args = client.upgrade_lxc.await_args.args
    assert args[0] == "pve" and args[1] == 101 and args[2] == "pve.example"
    # The stored `ssh_key` filename is resolved into a file-based key_path that
    # ssh_client._connect honours (key-based Proxmox SSH — OP#182).
    ssh_creds = args[3]
    assert ssh_creds["key_path"].endswith("/app/keys/id_ed25519")


# ---------------------------------------------------------------------------
# reboot_host_typed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reboot_typed_plain_uses_ssh():
    from app.host_ops import reboot_host_typed

    host = {"slug": "web", "host": "1.2.3.4", "user": "root"}
    with patch(
        "app.host_ops.reboot_host", new=AsyncMock(return_value=["rebooting"])
    ) as mock_reboot:
        lines = await reboot_host_typed(host, {})

    assert lines == ["rebooting"]
    mock_reboot.assert_awaited_once()


@pytest.mark.asyncio
async def test_reboot_typed_node_uses_api_not_ssh():
    """THE bug: node auto-reboot must use the graceful API, never an SSH hard reboot."""
    from app.host_ops import reboot_host_typed

    host = {"slug": "pve", "host": "1.2.3.4", "user": "root",
            "proxmox_node": "pve", "proxmox_vmid": None}
    client = MagicMock()
    client.reboot_node = AsyncMock()

    with patch("app.host_ops.reboot_host", new=AsyncMock()) as mock_ssh, \
         patch("app.host_ops.build_proxmox_client", new=AsyncMock(return_value=client)):
        await reboot_host_typed(host, {})

    mock_ssh.assert_not_called()
    client.reboot_node.assert_awaited_once_with("pve")


@pytest.mark.asyncio
async def test_reboot_typed_lxc_uses_api_not_ssh():
    from app.host_ops import reboot_host_typed

    host = {"slug": "ct", "proxmox_node": "pve", "proxmox_vmid": 101,
            "proxmox_type": "lxc"}
    client = MagicMock()
    client.reboot_lxc = AsyncMock()

    with patch("app.host_ops.reboot_host", new=AsyncMock()) as mock_ssh, \
         patch("app.host_ops.build_proxmox_client", new=AsyncMock(return_value=client)):
        await reboot_host_typed(host, {})

    mock_ssh.assert_not_called()
    client.reboot_lxc.assert_awaited_once_with("pve", 101)


@pytest.mark.asyncio
async def test_reboot_typed_node_refuses_self():
    """Self-reboot guard: never reboot the Proxmox node Keepup itself runs on."""
    from app.host_ops import reboot_host_typed

    host = {"slug": "pve", "proxmox_node": "pve", "proxmox_vmid": None}
    client = MagicMock()
    client.reboot_node = AsyncMock()

    with patch("app.host_ops.build_proxmox_client", new=AsyncMock(return_value=client)), \
         patch("app.host_ops.is_self_on_proxmox_node", return_value=True):
        with pytest.raises(ValueError, match="self"):
            await reboot_host_typed(host, {})

    client.reboot_node.assert_not_called()


# ---------------------------------------------------------------------------
# reboot_required_typed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reboot_required_typed_node_uses_api():
    from app.host_ops import reboot_required_typed

    host = {"slug": "pve", "proxmox_node": "pve", "proxmox_vmid": None}
    client = MagicMock()
    client.get_node_reboot_required = AsyncMock(return_value=True)

    with patch("app.host_ops.build_proxmox_client", new=AsyncMock(return_value=client)):
        assert await reboot_required_typed(host, {}) is True
    client.get_node_reboot_required.assert_awaited_once_with("pve")


@pytest.mark.asyncio
async def test_reboot_required_typed_plain_uses_ssh_check():
    from app.host_ops import reboot_required_typed

    host = {"slug": "web", "host": "1.2.3.4", "user": "root"}
    with patch(
        "app.host_ops.check_host_updates",
        new=AsyncMock(return_value={"reboot_required": True}),
    ):
        assert await reboot_required_typed(host, {}) is True


@pytest.mark.asyncio
async def test_build_proxmox_client_delegates_to_factory():
    from app import host_ops

    sentinel = object()
    with patch("app.host_ops.client_from_config", return_value=sentinel):
        assert await host_ops.build_proxmox_client() is sentinel


@pytest.mark.asyncio
async def test_reboot_required_typed_lxc_is_false():
    """LXCs share the host kernel — no LXC-level reboot is ever required."""
    from app.host_ops import reboot_required_typed

    host = {"slug": "ct", "proxmox_node": "pve", "proxmox_vmid": 101,
            "proxmox_type": "lxc"}
    assert await reboot_required_typed(host, {}) is False
