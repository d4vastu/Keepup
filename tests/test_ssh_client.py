"""Tests for ssh_client.py."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ssh_client import (
    check_host_updates,
    reboot_host,
    run_host_update_buffered,
    verify_connection,
)

HOST_KEY = {"name": "Test", "host": "10.0.0.1"}
HOST_PW = {"name": "Test", "host": "10.0.0.1", "password": "secret"}
SSH_CFG = {"default_user": "root", "default_port": 22, "default_key": "/app/keys/id_ed25519",
           "connect_timeout": 15, "command_timeout": 60}


def _make_conn(stdout: str = "", returncode: int = 0, stderr: str = "") -> MagicMock:
    """Build a mock asyncssh connection."""
    result = MagicMock(stdout=stdout, returncode=returncode, stderr=stderr)
    conn = MagicMock()
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    conn.run = AsyncMock(return_value=result)
    return conn


# ---------------------------------------------------------------------------
# test_connection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_connection_success():
    conn = _make_conn(stdout="ok\n")
    with patch("app.ssh_client.asyncssh.connect", new=AsyncMock(return_value=conn)):
        result = await verify_connection(HOST_KEY, SSH_CFG)
    assert result["ok"] is True
    assert "Connected" in result["message"]


@pytest.mark.asyncio
async def test_connection_unexpected_output():
    conn = _make_conn(stdout="something else\n")
    with patch("app.ssh_client.asyncssh.connect", new=AsyncMock(return_value=conn)):
        result = await verify_connection(HOST_KEY, SSH_CFG)
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_connection_failure():
    with patch("app.ssh_client.asyncssh.connect", side_effect=Exception("Connection refused")):
        result = await verify_connection(HOST_KEY, SSH_CFG)
    assert result["ok"] is False
    assert "Connection refused" in result["message"]


@pytest.mark.asyncio
async def test_connection_uses_password_when_set():
    conn = _make_conn(stdout="ok\n")
    with patch("app.ssh_client.asyncssh.connect", new=AsyncMock(return_value=conn)) as mock_connect:
        await verify_connection(HOST_PW, SSH_CFG)
    call_kwargs = mock_connect.call_args.kwargs
    assert call_kwargs.get("password") == "secret"
    assert call_kwargs.get("preferred_auth") == "password"
    assert "client_keys" not in call_kwargs


@pytest.mark.asyncio
async def test_connection_uses_key_when_no_password():
    conn = _make_conn(stdout="ok\n")
    with patch("app.ssh_client.asyncssh.connect", new=AsyncMock(return_value=conn)) as mock_connect:
        await verify_connection(HOST_KEY, SSH_CFG)
    call_kwargs = mock_connect.call_args.kwargs
    assert "client_keys" in call_kwargs
    assert "password" not in call_kwargs


# ---------------------------------------------------------------------------
# check_host_updates
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_host_updates_no_packages():
    output = "__REBOOT__\nno\n"
    conn = _make_conn(stdout=output)
    with patch("app.ssh_client.asyncssh.connect", new=AsyncMock(return_value=conn)):
        result = await check_host_updates(HOST_KEY, SSH_CFG)
    assert result["packages"] == []
    assert result["reboot_required"] is False


@pytest.mark.asyncio
async def test_check_host_updates_with_packages():
    output = (
        "nginx/stable 1.26.0-1 amd64 [upgradable from: 1.24.0-1]\n"
        "curl/stable 8.0.0 amd64 [upgradable from: 7.0.0]\n"
        "__REBOOT__\nno\n"
    )
    conn = _make_conn(stdout=output)
    with patch("app.ssh_client.asyncssh.connect", new=AsyncMock(return_value=conn)):
        result = await check_host_updates(HOST_KEY, SSH_CFG)
    assert len(result["packages"]) == 2
    assert result["packages"][0]["name"] == "nginx"
    assert result["packages"][0]["available"] == "1.26.0-1"
    assert result["packages"][0]["current"] == "1.24.0-1"


@pytest.mark.asyncio
async def test_check_host_updates_reboot_required():
    output = "__REBOOT__\nyes\n"
    conn = _make_conn(stdout=output)
    with patch("app.ssh_client.asyncssh.connect", new=AsyncMock(return_value=conn)):
        result = await check_host_updates(HOST_KEY, SSH_CFG)
    assert result["reboot_required"] is True


@pytest.mark.asyncio
async def test_check_host_updates_no_reboot_marker():
    # Output without __REBOOT__ sentinel still works
    conn = _make_conn(stdout="nginx/stable 1.26.0-1 amd64 [upgradable from: 1.24.0-1]\n")
    with patch("app.ssh_client.asyncssh.connect", new=AsyncMock(return_value=conn)):
        result = await check_host_updates(HOST_KEY, SSH_CFG)
    assert result["reboot_required"] is False
    assert len(result["packages"]) == 1


@pytest.mark.asyncio
async def test_check_host_updates_skips_non_package_lines():
    # Lines without [upgradable from: hit the continue branch
    output = "Listing... Done\nnginx/stable 1.26.0-1 amd64 [upgradable from: 1.24.0-1]\n__REBOOT__\nno\n"
    conn = _make_conn(stdout=output)
    with patch("app.ssh_client.asyncssh.connect", new=AsyncMock(return_value=conn)):
        result = await check_host_updates(HOST_KEY, SSH_CFG)
    assert len(result["packages"]) == 1  # Only the upgradable line counted


# ---------------------------------------------------------------------------
# reboot_host
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reboot_host_returns_message():
    conn = _make_conn()
    with patch("app.ssh_client.asyncssh.connect", new=AsyncMock(return_value=conn)):
        lines = await reboot_host(HOST_KEY, SSH_CFG)
    assert any("reboot" in line.lower() for line in lines)
    conn.run.assert_called_once()


# ---------------------------------------------------------------------------
# run_host_update_buffered
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_host_update_returns_lines():
    output = "Reading package lists...\nUpgrading curl...\n"
    conn = _make_conn(stdout=output)
    with patch("app.ssh_client.asyncssh.connect", new=AsyncMock(return_value=conn)):
        lines = await run_host_update_buffered(HOST_KEY, SSH_CFG)
    assert "Reading package lists..." in lines
    assert "Upgrading curl..." in lines


@pytest.mark.asyncio
async def test_run_host_update_includes_stderr_on_error():
    conn = _make_conn(stdout="some output\n", returncode=1, stderr="E: Could not lock\n")
    with patch("app.ssh_client.asyncssh.connect", new=AsyncMock(return_value=conn)):
        lines = await run_host_update_buffered(HOST_KEY, SSH_CFG)
    assert "E: Could not lock" in lines


@pytest.mark.asyncio
async def test_run_host_update_respects_timeout():
    import asyncio
    conn = _make_conn()
    conn.run = AsyncMock(side_effect=asyncio.TimeoutError())
    with patch("app.ssh_client.asyncssh.connect", new=AsyncMock(return_value=conn)):
        with pytest.raises(asyncio.TimeoutError):
            await run_host_update_buffered(HOST_KEY, {"command_timeout": 1})
