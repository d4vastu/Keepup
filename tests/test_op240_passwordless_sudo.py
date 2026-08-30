"""Tests for OP#240 — passwordless-sudo hosts must still get a sudo prefix.

``_run`` used to gate the ``sudo`` prefix on a stored sudo password, so a host
whose SSH user has ``NOPASSWD: ALL`` ran every privileged command unprivileged
and failed. These tests pin all three sudo paths: root (none), non-root with a
password (``sudo -S``), and non-root without one (``sudo -n``).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.package_managers import AptPackageManager
from app.ssh_client import _run, check_host_updates, run_host_update_buffered

_APT_PM = AptPackageManager()
_DETECT_PM_PATCH = patch(
    "app.ssh_client._detect_pm", new=AsyncMock(return_value=_APT_PM)
)

HOST_ROOT = {"name": "Test", "host": "10.0.0.1", "user": "root"}
HOST_NON_ROOT = {"name": "Test", "host": "10.0.0.1", "user": "daniel"}


def _make_conn(stdout: str = "", returncode: int = 0, stderr: str = "") -> MagicMock:
    result = MagicMock(stdout=stdout, returncode=returncode, stderr=stderr)
    conn = MagicMock()
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    conn.run = AsyncMock(return_value=result)
    return conn


# ---------------------------------------------------------------------------
# _run — the three sudo paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_uses_sudo_n_when_no_password_stored():
    """A NOPASSWD host gets `sudo -n`, not a bare command."""
    conn = _make_conn()
    await _run(conn, "apt-get upgrade -y", sudo_password=None, needs_sudo=True)
    sent_cmd = conn.run.call_args[0][0]
    assert sent_cmd == "sudo -n apt-get upgrade -y"


@pytest.mark.asyncio
async def test_run_sends_no_stdin_when_no_password_stored():
    """`sudo -n` never prompts, so nothing should be piped to stdin."""
    conn = _make_conn()
    await _run(conn, "apt-get upgrade -y", sudo_password=None, needs_sudo=True)
    assert conn.run.call_args.kwargs["input"] is None


@pytest.mark.asyncio
async def test_run_still_uses_sudo_s_when_password_stored():
    """Hosts that need a password keep the existing `sudo -S` path."""
    conn = _make_conn()
    await _run(conn, "apt-get upgrade -y", sudo_password="pw", needs_sudo=True)
    sent_cmd = conn.run.call_args[0][0]
    assert sent_cmd == "sudo -S apt-get upgrade -y"
    assert conn.run.call_args.kwargs["input"] == "pw\n"


@pytest.mark.asyncio
async def test_run_no_sudo_for_root_without_password():
    conn = _make_conn()
    await _run(conn, "apt-get upgrade -y", sudo_password=None, needs_sudo=False)
    assert conn.run.call_args[0][0] == "apt-get upgrade -y"


@pytest.mark.asyncio
async def test_run_no_sudo_for_root_even_with_password():
    """A stored password must not smuggle sudo onto a root host."""
    conn = _make_conn()
    await _run(conn, "apt-get upgrade -y", sudo_password="pw", needs_sudo=False)
    assert conn.run.call_args[0][0] == "apt-get upgrade -y"
    assert conn.run.call_args.kwargs["input"] is None


# ---------------------------------------------------------------------------
# The paths a passwordless host actually travels
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upgrade_runs_with_sudo_on_passwordless_host():
    """The bug as reported: the upgrade command reached the host bare."""
    conn = _make_conn(stdout="done\n")
    with (
        patch("app.ssh_client.asyncssh.connect", new=AsyncMock(return_value=conn)),
        _DETECT_PM_PATCH,
    ):
        await run_host_update_buffered(HOST_NON_ROOT, creds={})
    sent_cmd = conn.run.call_args[0][0]
    assert sent_cmd.startswith("sudo -n ")
    assert "apt-get upgrade -y" in sent_cmd


@pytest.mark.asyncio
async def test_update_check_uses_sudo_on_passwordless_host():
    conn = _make_conn(stdout="")
    with (
        patch("app.ssh_client.asyncssh.connect", new=AsyncMock(return_value=conn)),
        _DETECT_PM_PATCH,
    ):
        await check_host_updates(HOST_NON_ROOT, creds={})
    sent_cmd = conn.run.call_args[0][0]
    assert sent_cmd.startswith("sudo -n ")


@pytest.mark.asyncio
async def test_sudo_n_failure_is_diagnosable():
    """A host that genuinely needs a password must say so, not fail silently.

    `sudo -n` writes a specific refusal to stderr; the operator has to be able
    to read back *why* the upgrade failed (CLAUDE.md QA rules).
    """
    conn = _make_conn(
        stdout="",
        returncode=1,
        stderr="sudo: a password is required\n",
    )
    result = await _run(
        conn, "apt-get upgrade -y", sudo_password=None, needs_sudo=True
    )
    assert "a password is required" in result.stderr
