"""OP#254 — a failed OS upgrade must be reported as a failure, with its cause.

Both upgrade paths used to treat any return as success: `upgrade_lxc()` never
looked at the exit status, and `run_host_update_buffered()` logged a non-zero
exit and returned the lines anyway. A scheduled run with auto-reboot then
rebooted the host after a failed upgrade. LXC upgrades also ran apt without
`DEBIAN_FRONTEND=noninteractive` or a conffile answer, under a hard-coded
300 s timeout.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.package_managers import AptPackageManager
from app.ssh_client import (
    UpgradeFailed,
    UpgradeTimeout,
    _upgrade_timeout,
    run_host_update_buffered,
)

HOST = {"name": "Test", "host": "10.0.0.1", "user": "root"}
LXC_CREDS = {"key_path": "/app/keys/id_ed25519"}
LOCK_ERROR = (
    "E: Could not get lock /var/lib/dpkg/lock-frontend. It is held by "
    "process 4242 (apt-get)"
)

_DETECT_APT = patch(
    "app.ssh_client._detect_pm", new=AsyncMock(return_value=AptPackageManager())
)


def _result(stdout="", returncode=0, stderr="", exit_signal=None):
    return MagicMock(
        stdout=stdout, returncode=returncode, stderr=stderr, exit_signal=exit_signal
    )


def _conn(result=None, run=None):
    conn = MagicMock()
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    conn.run = run or AsyncMock(return_value=result)
    return conn


@pytest.fixture
def px():
    from app.proxmox_client import ProxmoxClient

    return ProxmoxClient(url="https://192.168.1.10:8006", api_token="u@pam!t=x")


async def _upgrade_lxc(px, conn):
    with patch("app.ssh_client.asyncssh.connect", new=AsyncMock(return_value=conn)):
        return await px.upgrade_lxc("pve", 101, "192.168.1.10", LXC_CREDS)


async def _upgrade_ssh(conn):
    with (
        patch("app.ssh_client.asyncssh.connect", new=AsyncMock(return_value=conn)),
        _DETECT_APT,
    ):
        return await run_host_update_buffered(HOST)


# ---------------------------------------------------------------------------
# SSH hosts and Proxmox nodes (run_host_update_buffered)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ssh_nonzero_exit_raises_with_cause_and_output():
    conn = _conn(_result(
        stdout="Reading package lists...\n" + LOCK_ERROR + "\n",
        returncode=100,
        stderr="E: Unable to acquire the dpkg frontend lock\n",
    ))

    with pytest.raises(UpgradeFailed) as exc_info:
        await _upgrade_ssh(conn)

    msg = str(exc_info.value)
    assert "10.0.0.1" in msg
    assert "status 100" in msg
    assert "E: Unable to acquire the dpkg frontend lock" in msg
    assert "dpkg --configure -a" in msg  # apt's recovery hint
    # The whole output survives for the activity log, stderr included.
    assert "Reading package lists..." in exc_info.value.lines
    assert LOCK_ERROR in exc_info.value.lines
    assert "E: Unable to acquire the dpkg frontend lock" in exc_info.value.lines


@pytest.mark.asyncio
async def test_ssh_nonzero_exit_with_no_output_still_says_something():
    conn = _conn(_result(stdout="", returncode=1, stderr=""))

    with pytest.raises(UpgradeFailed) as exc_info:
        await _upgrade_ssh(conn)

    msg = str(exc_info.value)
    assert "status 1" in msg
    assert "printed nothing" in msg
    assert exc_info.value.lines == []


@pytest.mark.asyncio
async def test_ssh_signal_killed_upgrade_names_the_signal():
    conn = _conn(_result(
        stdout="Unpacking foo ...\n", returncode=None,
        exit_signal=("KILL", False, "", ""),
    ))

    with pytest.raises(UpgradeFailed) as exc_info:
        await _upgrade_ssh(conn)

    msg = str(exc_info.value)
    assert "signal KILL" in msg
    assert "None" not in msg


@pytest.mark.asyncio
async def test_ssh_failure_cause_falls_back_to_last_output_line():
    conn = _conn(_result(stdout="line one\nsomething odd happened\n\n", returncode=2))

    with pytest.raises(UpgradeFailed) as exc_info:
        await _upgrade_ssh(conn)

    assert "something odd happened" in str(exc_info.value)


@pytest.mark.asyncio
async def test_ssh_successful_upgrade_still_returns_lines():
    conn = _conn(_result(stdout="Setting up curl ...\n", returncode=0))

    assert await _upgrade_ssh(conn) == ["Setting up curl ..."]


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [102, 103])
async def test_zypper_informational_exit_codes_are_success(code):
    """zypper exits 102 (reboot needed) or 103 (zypper itself was updated)
    after an upgrade that worked. Calling those failures would also cancel
    the very auto-reboot that 102 asks for."""
    from app.package_managers import ZypperPackageManager

    conn = _conn(_result(stdout="Installing: kernel-default\n", returncode=code))
    with (
        patch("app.ssh_client.asyncssh.connect", new=AsyncMock(return_value=conn)),
        patch("app.ssh_client._detect_pm",
              new=AsyncMock(return_value=ZypperPackageManager())),
    ):
        assert await run_host_update_buffered(HOST) == ["Installing: kernel-default"]


@pytest.mark.asyncio
async def test_zypper_real_failure_code_still_raises():
    from app.package_managers import ZypperPackageManager

    conn = _conn(_result(stdout="Problem: nothing provides libfoo\n", returncode=4))
    with (
        patch("app.ssh_client.asyncssh.connect", new=AsyncMock(return_value=conn)),
        patch("app.ssh_client._detect_pm",
              new=AsyncMock(return_value=ZypperPackageManager())),
    ):
        with pytest.raises(UpgradeFailed) as exc_info:
            await run_host_update_buffered(HOST)

    assert "zypper exited with status 4" in str(exc_info.value)
    assert "nothing provides libfoo" in str(exc_info.value)


@pytest.mark.parametrize("cause, expected", [
    ("E: Broken packages.", "E: Broken packages. Hint."),
    # apt's real lock error, as seen in live QA: it ends in "?", and the
    # message used to read "…using it?. Hint."
    ("E: Unable to acquire the dpkg frontend lock, is another process using it?",
     "is another process using it? Hint."),
    ("error: failed!", "error: failed! Hint."),
    ("E: plain", "E: plain. Hint."),
])
def test_failure_message_punctuates_the_cause_once(cause, expected):
    from app.ssh_client import upgrade_failure

    msg = str(upgrade_failure("h", "apt", _result(returncode=1), [cause], "Hint."))

    assert msg.endswith(expected)
    assert "?." not in msg and "!." not in msg and ".." not in msg


def test_apt_upgrade_cmd_answers_conffile_prompts():
    cmd = AptPackageManager().upgrade_cmd()

    assert "DEBIAN_FRONTEND=noninteractive" in cmd
    assert "-o Dpkg::Options::=--force-confdef" in cmd
    assert "-o Dpkg::Options::=--force-confold" in cmd


# ---------------------------------------------------------------------------
# LXCs (ProxmoxClient.upgrade_lxc)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lxc_upgrade_runs_noninteractively_with_conffile_answer(px):
    conn = _conn(_result(stdout="0 upgraded\n"))

    await _upgrade_lxc(px, conn)

    cmd = conn.run.await_args.args[0]
    assert cmd.startswith("pct exec 101 -- env DEBIAN_FRONTEND=noninteractive apt-get ")
    assert "-o Dpkg::Options::=--force-confdef" in cmd
    assert "-o Dpkg::Options::=--force-confold" in cmd
    assert "upgrade -y" in cmd


@pytest.mark.asyncio
async def test_lxc_upgrade_timeout_is_the_generous_upgrade_budget(px, monkeypatch):
    monkeypatch.delenv("KEEPUP_UPGRADE_TIMEOUT", raising=False)
    conn = _conn(_result(stdout="ok\n"))
    seen = {}

    async def fake_wait_for(coro, timeout):
        seen["timeout"] = timeout
        return await coro

    with patch("app.ssh_client.asyncio.wait_for", new=fake_wait_for):
        await _upgrade_lxc(px, conn)

    assert seen["timeout"] == _upgrade_timeout()
    assert seen["timeout"] >= 3600


@pytest.mark.asyncio
async def test_lxc_upgrade_timeout_honours_env_override(px, monkeypatch):
    monkeypatch.setenv("KEEPUP_UPGRADE_TIMEOUT", "7200")
    conn = _conn(_result(stdout="ok\n"))
    seen = {}

    async def fake_wait_for(coro, timeout):
        seen["timeout"] = timeout
        return await coro

    with patch("app.ssh_client.asyncio.wait_for", new=fake_wait_for):
        await _upgrade_lxc(px, conn)

    assert seen["timeout"] == 7200


@pytest.mark.asyncio
async def test_lxc_upgrade_that_really_hangs_raises_readable_timeout(px, monkeypatch):
    """Nothing here answers instantly: the command genuinely never returns and
    the real `asyncio.wait_for` has to fire (CLAUDE.md QA rules, OP#228)."""
    monkeypatch.setenv("KEEPUP_UPGRADE_TIMEOUT", "1")

    async def hang(*_a, **_kw):
        await asyncio.Event().wait()

    conn = _conn(run=hang)

    with pytest.raises(UpgradeTimeout) as exc_info:
        await _upgrade_lxc(px, conn)

    msg = str(exc_info.value)
    assert "timed out after 1s" in msg
    assert "LXC 101" in msg
    assert "pct exec 101 -- dpkg --configure -a" in msg


@pytest.mark.asyncio
async def test_lxc_nonzero_exit_raises_with_cause_and_output(px):
    conn = _conn(_result(
        stdout="Reading package lists...\n" + LOCK_ERROR + "\n", returncode=100
    ))

    with pytest.raises(UpgradeFailed) as exc_info:
        await _upgrade_lxc(px, conn)

    msg = str(exc_info.value)
    assert "LXC 101" in msg
    assert "status 100" in msg
    assert LOCK_ERROR in msg
    assert "pct exec 101 -- dpkg --configure -a" in msg
    assert exc_info.value.lines == ["Reading package lists...", LOCK_ERROR]


@pytest.mark.asyncio
async def test_lxc_nonzero_exit_with_no_output_still_says_something(px):
    conn = _conn(_result(stdout="", returncode=1))

    with pytest.raises(UpgradeFailed) as exc_info:
        await _upgrade_lxc(px, conn)

    assert "printed nothing" in str(exc_info.value)


@pytest.mark.asyncio
async def test_lxc_successful_upgrade_returns_lines(px):
    conn = _conn(_result(stdout="Setting up curl ...\n\n0 upgraded\n"))

    assert await _upgrade_lxc(px, conn) == ["Setting up curl ...", "0 upgraded"]


def test_dead_api_node_upgrade_is_gone():
    """Nodes upgrade over SSH since OP#232; the API path had the same bug."""
    from app.proxmox_client import ProxmoxClient

    assert not hasattr(ProxmoxClient, "upgrade_node")


# ---------------------------------------------------------------------------
# Callers keep the failed run's output and report it as an error
# ---------------------------------------------------------------------------


def _failure():
    return UpgradeFailed(
        "Upgrade failed on LXC 101 (apt exited with status 100): " + LOCK_ERROR,
        ["Reading package lists...", LOCK_ERROR],
    )


def _seed_job(main, job_id):
    main._jobs[job_id] = {
        "done": False, "status": "running", "error": None, "lines": [],
        "type": "os_upgrade", "label": "My Host", "target": "my-host",
        "sub": "1.2.3.4", "started_at": "2026-09-30T10:00:00+00:00",
        "activity_id": "",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("runner", ["host", "node", "lxc"])
async def test_dashboard_job_records_failed_upgrade_with_output(
    config_file, data_dir, runner
):
    import app.main as main
    from app.activity_log import get_recent, get_run_output

    job_id = f"op254-{runner}"
    _seed_job(main, job_id)
    host = {"slug": "my-host", "name": "My Host", "host": "1.2.3.4",
            "proxmox_node": "pve", "proxmox_vmid": 101}

    with (
        patch("app.main.run_os_update", new=AsyncMock(side_effect=_failure())),
        patch("app.main._get_host", return_value=host),
        patch("app.main.get_credentials", return_value={}),
    ):
        if runner == "host":
            await main._job_run_host_update(job_id, host, {})
        elif runner == "node":
            await main._job_run_proxmox_node_upgrade(job_id, "my-host")
        else:
            await main._job_run_lxc_upgrade(job_id, host)

    job = main._jobs[job_id]
    assert job["status"] == "error"
    assert LOCK_ERROR in job["error"]
    assert job["lines"] == ["Reading package lists...", LOCK_ERROR]
    entry = get_recent()[0]
    assert entry["status"] == "error"
    assert LOCK_ERROR in entry["error"]
    assert get_run_output(entry["id"]) == ["Reading package lists...", LOCK_ERROR]


def _enable_auto_update(config_file, auto_reboot):
    import yaml

    raw = yaml.safe_load(config_file.read_text())
    raw["hosts"][0]["auto_update"] = {
        "os_enabled": True, "os_schedule": "0 3 * * *", "auto_reboot": auto_reboot,
    }
    config_file.write_text(yaml.dump(raw))


@pytest.mark.asyncio
async def test_scheduled_failed_upgrade_is_an_error_and_does_not_reboot(
    config_file, data_dir
):
    from app.activity_log import get_recent, get_run_output
    from app.auto_update_scheduler import _run_os_update

    _enable_auto_update(config_file, auto_reboot=True)
    reboot = AsyncMock()
    reboot_required = AsyncMock(return_value=True)

    with (
        patch("app.auto_update_scheduler.run_os_update",
              new=AsyncMock(side_effect=_failure())),
        patch("app.auto_update_scheduler.reboot_required_typed", new=reboot_required),
        patch("app.auto_update_scheduler.reboot_host_typed", new=reboot),
        patch("app.auto_update_scheduler.notify") as notify,
    ):
        await _run_os_update("test-host")

    reboot.assert_not_called()
    reboot_required.assert_not_called()
    entries = get_recent(10)
    assert [e["kind"] for e in entries] == ["os_upgrade"]
    assert entries[0]["status"] == "error"
    assert LOCK_ERROR in entries[0]["error"]
    assert get_run_output(entries[0]["id"]) == ["Reading package lists...", LOCK_ERROR]
    assert LOCK_ERROR in notify.call_args.args[1]
