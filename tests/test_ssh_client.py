"""Tests for ssh_client.py."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.package_managers import AptPackageManager
from app.ssh_client import (
    check_host_updates,
    detect_docker_stacks,
    discover_containers,
    reboot_host,
    run_host_update_buffered,
    verify_connection,
)

_APT_PM = AptPackageManager()
_DETECT_PM_PATCH = patch(
    "app.ssh_client._detect_pm", new=AsyncMock(return_value=_APT_PM)
)

HOST = {"name": "Test", "host": "10.0.0.1", "user": "root"}
HOST_KEY = {"name": "Test", "host": "10.0.0.1", "user": "root", "key": "/app/keys/id_ed25519"}
CREDS_PW = {"ssh_password": "secret"}
CREDS_EMPTY = {}


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
        result = await verify_connection(HOST_KEY)
    assert result["ok"] is True
    assert "Connected" in result["message"]


@pytest.mark.asyncio
async def test_connection_unexpected_output():
    conn = _make_conn(stdout="something else\n")
    with patch("app.ssh_client.asyncssh.connect", new=AsyncMock(return_value=conn)):
        result = await verify_connection(HOST_KEY)
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_connection_failure():
    with patch(
        "app.ssh_client.asyncssh.connect", side_effect=Exception("Connection refused")
    ):
        result = await verify_connection(HOST_KEY)
    assert result["ok"] is False
    assert "Connection refused" in result["message"]


@pytest.mark.asyncio
async def test_connection_uses_password_when_set():
    conn = _make_conn(stdout="ok\n")
    with patch(
        "app.ssh_client.asyncssh.connect", new=AsyncMock(return_value=conn)
    ) as mock_connect:
        await verify_connection(HOST, CREDS_PW)
    call_kwargs = mock_connect.call_args.kwargs
    assert call_kwargs.get("password") == "secret"
    assert call_kwargs.get("preferred_auth") == "password"
    assert "client_keys" not in call_kwargs


@pytest.mark.asyncio
async def test_connection_uses_key_when_no_password():
    conn = _make_conn(stdout="ok\n")
    with patch(
        "app.ssh_client.asyncssh.connect", new=AsyncMock(return_value=conn)
    ) as mock_connect:
        await verify_connection(HOST_KEY)
    call_kwargs = mock_connect.call_args.kwargs
    assert "client_keys" in call_kwargs
    assert "password" not in call_kwargs


@pytest.mark.asyncio
async def test_connect_raises_on_empty_user():
    from app.ssh_client import _connect
    host_no_user = {"name": "NoUserHost", "host": "10.0.0.2"}
    with pytest.raises(ValueError, match="no SSH user configured"):
        await _connect(host_no_user)


# ---------------------------------------------------------------------------
# check_host_updates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_host_updates_no_packages():
    output = "__REBOOT__\nno\n"
    conn = _make_conn(stdout=output)
    with (
        patch("app.ssh_client.asyncssh.connect", new=AsyncMock(return_value=conn)),
        _DETECT_PM_PATCH,
    ):
        result = await check_host_updates(HOST_KEY)
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
    with (
        patch("app.ssh_client.asyncssh.connect", new=AsyncMock(return_value=conn)),
        _DETECT_PM_PATCH,
    ):
        result = await check_host_updates(HOST_KEY)
    assert len(result["packages"]) == 2
    assert result["packages"][0]["name"] == "nginx"
    assert result["packages"][0]["available"] == "1.26.0-1"
    assert result["packages"][0]["current"] == "1.24.0-1"


@pytest.mark.asyncio
async def test_check_host_updates_reboot_required():
    output = "__REBOOT__\nyes\n"
    conn = _make_conn(stdout=output)
    with (
        patch("app.ssh_client.asyncssh.connect", new=AsyncMock(return_value=conn)),
        _DETECT_PM_PATCH,
    ):
        result = await check_host_updates(HOST_KEY)
    assert result["reboot_required"] is True


@pytest.mark.asyncio
async def test_check_host_updates_no_reboot_marker():
    # Output without __REBOOT__ sentinel still works
    conn = _make_conn(
        stdout="nginx/stable 1.26.0-1 amd64 [upgradable from: 1.24.0-1]\n"
    )
    with (
        patch("app.ssh_client.asyncssh.connect", new=AsyncMock(return_value=conn)),
        _DETECT_PM_PATCH,
    ):
        result = await check_host_updates(HOST_KEY)
    assert result["reboot_required"] is False
    assert len(result["packages"]) == 1


@pytest.mark.asyncio
async def test_check_host_updates_refreshes_on_cold_cache():
    """First check on a cold cache must include apt-get update in the sent command."""
    from app.update_check_cache import clear as clear_cache

    clear_cache()
    conn = _make_conn(stdout="__REBOOT__\nno\n")
    with (
        patch("app.ssh_client.asyncssh.connect", new=AsyncMock(return_value=conn)),
        _DETECT_PM_PATCH,
    ):
        await check_host_updates(HOST_KEY)
    # conn.run is called twice: once for detect (patched out) + once for list_cmd
    sent_cmd = conn.run.call_args[0][0]
    assert "apt-get update" in sent_cmd


@pytest.mark.asyncio
async def test_check_host_updates_skips_refresh_when_cache_fresh():
    """A check within the TTL window must NOT include apt-get update."""
    from app.update_check_cache import clear as clear_cache, mark_refreshed

    clear_cache()
    mark_refreshed("ssh:10.0.0.1")  # cache key is slug or host
    conn = _make_conn(stdout="__REBOOT__\nno\n")
    with (
        patch("app.ssh_client.asyncssh.connect", new=AsyncMock(return_value=conn)),
        _DETECT_PM_PATCH,
    ):
        await check_host_updates(HOST_KEY)
    sent_cmd = conn.run.call_args[0][0]
    assert "apt-get update" not in sent_cmd
    assert "apt list --upgradable" in sent_cmd


@pytest.mark.asyncio
async def test_check_host_updates_skips_non_package_lines():
    # Lines without [upgradable from: hit the continue branch
    output = "Listing... Done\nnginx/stable 1.26.0-1 amd64 [upgradable from: 1.24.0-1]\n__REBOOT__\nno\n"
    conn = _make_conn(stdout=output)
    with (
        patch("app.ssh_client.asyncssh.connect", new=AsyncMock(return_value=conn)),
        _DETECT_PM_PATCH,
    ):
        result = await check_host_updates(HOST_KEY)
    assert len(result["packages"]) == 1  # Only the upgradable line counted


# ---------------------------------------------------------------------------
# reboot_host
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reboot_host_returns_message():
    conn = _make_conn()
    with patch("app.ssh_client.asyncssh.connect", new=AsyncMock(return_value=conn)):
        lines = await reboot_host(HOST_KEY)
    assert any("reboot" in line.lower() for line in lines)
    conn.run.assert_called_once()


# ---------------------------------------------------------------------------
# run_host_update_buffered
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_host_update_returns_lines():
    output = "Reading package lists...\nUpgrading curl...\n"
    conn = _make_conn(stdout=output)
    with (
        patch("app.ssh_client.asyncssh.connect", new=AsyncMock(return_value=conn)),
        _DETECT_PM_PATCH,
    ):
        lines = await run_host_update_buffered(HOST_KEY)
    assert "Reading package lists..." in lines
    assert "Upgrading curl..." in lines


@pytest.mark.asyncio
async def test_run_host_update_includes_stderr_on_error():
    conn = _make_conn(
        stdout="some output\n", returncode=1, stderr="E: Could not lock\n"
    )
    with (
        patch("app.ssh_client.asyncssh.connect", new=AsyncMock(return_value=conn)),
        _DETECT_PM_PATCH,
    ):
        lines = await run_host_update_buffered(HOST_KEY)
    assert "E: Could not lock" in lines


@pytest.mark.asyncio
async def test_run_host_update_respects_timeout():
    import asyncio

    conn = _make_conn()
    conn.run = AsyncMock(side_effect=asyncio.TimeoutError())
    with (
        patch("app.ssh_client.asyncssh.connect", new=AsyncMock(return_value=conn)),
        _DETECT_PM_PATCH,
    ):
        with pytest.raises(asyncio.TimeoutError):
            await run_host_update_buffered(HOST_KEY)


# ---------------------------------------------------------------------------
# discover_containers
# ---------------------------------------------------------------------------

_CONTAINER_JSON = (
    '{"name":"nginx","image":"nginx:latest"}\n'
    '{"name":"redis","image":"redis:7"}\n'
)

HOST_NON_ROOT = {"name": "Test", "host": "10.0.0.1", "user": "admin"}
HOST_ROOT = {"name": "Test", "host": "10.0.0.1", "user": "root"}
CREDS_SUDO = {"sudo_password": "sudopass"}


@pytest.mark.asyncio
async def test_discover_containers_parses_containers():
    conn = _make_conn(stdout=_CONTAINER_JSON)
    with patch("app.ssh_client.asyncssh.connect", new=AsyncMock(return_value=conn)):
        result = await discover_containers(HOST_ROOT)
    assert len(result) == 2
    assert result[0] == {"id": "nginx", "name": "nginx", "image": "nginx:latest"}
    assert result[1] == {"id": "redis", "name": "redis", "image": "redis:7"}


@pytest.mark.asyncio
async def test_discover_containers_returns_empty_on_error():
    with patch(
        "app.ssh_client.asyncssh.connect", side_effect=Exception("refused")
    ):
        result = await discover_containers(HOST_ROOT)
    assert result == []


@pytest.mark.asyncio
async def test_discover_containers_uses_sudo_for_non_root():
    conn = _make_conn(stdout=_CONTAINER_JSON)
    with patch("app.ssh_client.asyncssh.connect", new=AsyncMock(return_value=conn)):
        await discover_containers(HOST_NON_ROOT, creds=CREDS_SUDO)
    sent_cmd = conn.run.call_args[0][0]
    assert sent_cmd.startswith("sudo -S")
    assert "docker ps" in sent_cmd


@pytest.mark.asyncio
async def test_discover_containers_no_sudo_for_root():
    conn = _make_conn(stdout=_CONTAINER_JSON)
    with patch("app.ssh_client.asyncssh.connect", new=AsyncMock(return_value=conn)):
        await discover_containers(HOST_ROOT)
    sent_cmd = conn.run.call_args[0][0]
    assert not sent_cmd.startswith("sudo")
    assert "docker ps" in sent_cmd


@pytest.mark.asyncio
async def test_discover_containers_standalone_containers():
    """docker ps lists standalone (non-compose, non-swarm) containers."""
    stdout = '{"name":"standalone","image":"alpine:3"}\n'
    conn = _make_conn(stdout=stdout)
    with patch("app.ssh_client.asyncssh.connect", new=AsyncMock(return_value=conn)):
        result = await discover_containers(HOST_ROOT)
    assert len(result) == 1
    assert result[0]["name"] == "standalone"


# ---------------------------------------------------------------------------
# detect_docker_stacks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detect_docker_stacks_compose_only():
    stacks_json = '[{"Name":"web","Status":"running","ConfigFiles":"..."}]'
    conn = _make_conn(stdout=stacks_json)
    with patch("app.ssh_client.asyncssh.connect", new=AsyncMock(return_value=conn)):
        count = await detect_docker_stacks(HOST_ROOT)
    assert count == 1


@pytest.mark.asyncio
async def test_detect_docker_stacks_empty_returns_zero():
    conn = _make_conn(stdout="[]")
    with patch("app.ssh_client.asyncssh.connect", new=AsyncMock(return_value=conn)):
        count = await detect_docker_stacks(HOST_ROOT)
    assert count == 0


@pytest.mark.asyncio
async def test_detect_docker_stacks_error_returns_minus_one():
    with patch(
        "app.ssh_client.asyncssh.connect", side_effect=Exception("refused")
    ):
        count = await detect_docker_stacks(HOST_ROOT)
    assert count == -1


@pytest.mark.asyncio
async def test_detect_docker_stacks_uses_sudo_for_non_root():
    conn = _make_conn(stdout="[]")
    with patch("app.ssh_client.asyncssh.connect", new=AsyncMock(return_value=conn)):
        await detect_docker_stacks(HOST_NON_ROOT, creds=CREDS_SUDO)
    sent_cmd = conn.run.call_args[0][0]
    assert sent_cmd.startswith("sudo -S")
    assert "docker compose ls" in sent_cmd


@pytest.mark.asyncio
async def test_detect_docker_stacks_no_sudo_for_root():
    conn = _make_conn(stdout="[]")
    with patch("app.ssh_client.asyncssh.connect", new=AsyncMock(return_value=conn)):
        await detect_docker_stacks(HOST_ROOT)
    sent_cmd = conn.run.call_args[0][0]
    assert not sent_cmd.startswith("sudo")
