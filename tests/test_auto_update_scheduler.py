"""Tests for auto_update_scheduler module."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture(autouse=True)
def _setup(config_file, data_dir):
    """Each test gets isolated config and data dirs, and a clean activity log.

    data_dir (conftest.py) already points app.activity_log at the temp dir;
    requesting it here is what makes that apply to every test in this module.
    """
    return data_dir


# ---------------------------------------------------------------------------
# _run_os_update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_os_update_host_not_found(config_file):
    """If the host slug doesn't exist, function returns silently."""
    from app.auto_update_scheduler import _run_os_update

    # No exception should be raised
    await _run_os_update("nonexistent-host")


@pytest.mark.asyncio
async def test_run_os_update_disabled(config_file):
    """If os_enabled is False, function returns without running update."""
    from app.auto_update_scheduler import _run_os_update
    from app.activity_log import get_recent

    with patch(
        "app.auto_update_scheduler.run_os_update", new=AsyncMock()
    ) as mock_update:
        await _run_os_update("test-host")

    mock_update.assert_not_called()
    assert get_recent(10) == []


@pytest.mark.asyncio
async def test_run_os_update_success(config_file):
    """A successful scheduled update is recorded to the activity log."""
    import yaml
    from app.auto_update_scheduler import _run_os_update
    from app.activity_log import get_recent

    # Enable auto-update for test-host
    raw = yaml.safe_load(config_file.read_text())
    raw["hosts"][0]["auto_update"] = {
        "os_enabled": True,
        "os_schedule": "0 3 * * *",
        "auto_reboot": False,
    }
    config_file.write_text(yaml.dump(raw))

    with patch(
        "app.auto_update_scheduler.run_os_update",
        new=AsyncMock(return_value=["Package updated."]),
    ):
        await _run_os_update("test-host")

    entries = get_recent(10)
    assert len(entries) == 1
    assert entries[0]["status"] == "success"
    assert entries[0]["target"] == "test-host"
    assert entries[0]["kind"] == "os_upgrade"
    assert entries[0]["trigger"] == "scheduled"


@pytest.mark.asyncio
async def test_run_os_update_failure_logs_error(config_file):
    """Exception during update is logged as error."""
    import yaml
    from app.auto_update_scheduler import _run_os_update
    from app.activity_log import get_recent, get_run_output

    raw = yaml.safe_load(config_file.read_text())
    raw["hosts"][0]["auto_update"] = {
        "os_enabled": True,
        "os_schedule": "0 3 * * *",
        "auto_reboot": False,
    }
    config_file.write_text(yaml.dump(raw))

    with patch(
        "app.auto_update_scheduler.run_os_update",
        new=AsyncMock(side_effect=Exception("SSH timeout")),
    ):
        await _run_os_update("test-host")

    entries = get_recent(10)
    assert len(entries) == 1
    assert entries[0]["status"] == "error"
    assert entries[0]["trigger"] == "scheduled"
    assert "SSH timeout" in entries[0]["error"]
    assert "SSH timeout" in get_run_output(entries[0]["id"])[0]


@pytest.mark.asyncio
async def test_run_os_update_sudo_required_but_no_password(config_file):
    """A run that never started is recorded as 'skipped', not as a failure."""
    import yaml
    from app.auto_update_scheduler import _run_os_update
    from app.activity_log import get_recent

    raw = yaml.safe_load(config_file.read_text())
    raw["hosts"][0]["auto_update"] = {
        "os_enabled": True,
        "os_schedule": "0 3 * * *",
        "auto_reboot": False,
    }
    config_file.write_text(yaml.dump(raw))

    with (
        patch("app.auto_update_scheduler._needs_sudo", return_value=True),
        patch(
            "app.auto_update_scheduler.run_os_update", new=AsyncMock()
        ) as mock_update,
    ):
        await _run_os_update("test-host")

    mock_update.assert_not_called()
    entries = get_recent(10)
    assert len(entries) == 1
    assert entries[0]["status"] == "skipped"
    assert entries[0]["kind"] == "os_upgrade"
    assert entries[0]["trigger"] == "scheduled"


@pytest.mark.asyncio
async def test_run_os_update_with_auto_reboot(config_file):
    """When auto_reboot=True and reboot_required, reboot is called."""
    import yaml
    from app.auto_update_scheduler import _run_os_update
    from app.activity_log import get_recent

    raw = yaml.safe_load(config_file.read_text())
    raw["hosts"][0]["auto_update"] = {
        "os_enabled": True,
        "os_schedule": "0 3 * * *",
        "auto_reboot": True,
    }
    config_file.write_text(yaml.dump(raw))

    with (
        patch(
            "app.auto_update_scheduler.run_os_update",
            new=AsyncMock(return_value=["Updated."]),
        ),
        patch(
            "app.auto_update_scheduler.reboot_required_typed",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "app.auto_update_scheduler.reboot_host_typed", new=AsyncMock(return_value=[])
        ) as mock_reboot,
    ):
        await _run_os_update("test-host")

    mock_reboot.assert_called_once()
    entries = get_recent(10)
    # The reboot is its own run, not a second os_upgrade entry.
    assert [e["kind"] for e in entries] == ["reboot", "os_upgrade"]
    assert all(e["status"] == "success" for e in entries)
    assert all(e["trigger"] == "scheduled" for e in entries)


@pytest.mark.asyncio
async def test_run_os_update_node_reboots_gracefully_not_ssh(config_file):
    """OP#176: a Proxmox node auto-reboot must use the graceful typed path, never SSH."""
    import yaml
    from app.auto_update_scheduler import _run_os_update

    raw = yaml.safe_load(config_file.read_text())
    raw["hosts"][0]["proxmox_node"] = "pve"
    raw["hosts"][0]["proxmox_vmid"] = None
    raw["hosts"][0]["user"] = "root"
    raw["hosts"][0]["auto_update"] = {
        "os_enabled": True,
        "os_schedule": "0 3 * * *",
        "auto_reboot": True,
    }
    config_file.write_text(yaml.dump(raw))

    with (
        patch(
            "app.auto_update_scheduler.run_os_update",
            new=AsyncMock(return_value=["Updated."]),
        ),
        patch(
            "app.auto_update_scheduler.reboot_required_typed",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "app.auto_update_scheduler.reboot_host_typed",
            new=AsyncMock(return_value=["api reboot"]),
        ) as mock_typed,
        # The SSH hard-reboot path must never be reached for a Proxmox node.
        patch("app.host_ops.reboot_host", new=AsyncMock()) as mock_ssh,
    ):
        await _run_os_update("test-host")

    mock_typed.assert_awaited_once()
    mock_ssh.assert_not_called()


# ---------------------------------------------------------------------------
# _run_stack_update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_stack_update_invalid_path(config_file):
    """Invalid update_path format logs error."""
    from app.auto_update_scheduler import _run_stack_update
    from app.activity_log import get_recent

    await _run_stack_update("badformat", "mystack")

    entries = get_recent(10)
    assert len(entries) == 1
    assert entries[0]["status"] == "error"
    assert entries[0]["kind"] == "container_redeploy"
    assert entries[0]["trigger"] == "scheduled"


@pytest.mark.asyncio
async def test_run_stack_update_no_backend(config_file, monkeypatch):
    """Missing backend logs error."""
    from app.auto_update_scheduler import _run_stack_update, set_backends
    from app.activity_log import get_recent, get_run_output

    set_backends([])
    await _run_stack_update("portainer/10:1", "sonarr")

    entries = get_recent(10)
    assert len(entries) == 1
    assert entries[0]["status"] == "error"
    assert "portainer" in get_run_output(entries[0]["id"])[0]


@pytest.mark.asyncio
async def test_run_stack_update_success(config_file):
    """Successful stack update logs success."""
    from app.auto_update_scheduler import _run_stack_update, set_backends
    from app.activity_log import get_recent

    mock_backend = MagicMock()
    mock_backend.BACKEND_KEY = "portainer"
    mock_backend.update_stack = AsyncMock(return_value=None)
    set_backends([mock_backend])

    await _run_stack_update("portainer/10:1", "sonarr")

    entries = get_recent(10)
    assert len(entries) == 1
    assert entries[0]["status"] == "success"
    assert entries[0]["kind"] == "container_redeploy"
    assert entries[0]["trigger"] == "scheduled"
    assert entries[0]["target"] == "portainer/10:1"
    assert entries[0]["target_name"] == "sonarr"
    mock_backend.update_stack.assert_called_once_with("10:1")


@pytest.mark.asyncio
async def test_run_stack_update_failure_logs_error(config_file):
    """Exception during stack update is logged as error."""
    from app.auto_update_scheduler import _run_stack_update, set_backends
    from app.activity_log import get_recent, get_run_output

    mock_backend = MagicMock()
    mock_backend.BACKEND_KEY = "portainer"
    mock_backend.update_stack = AsyncMock(side_effect=Exception("API error"))
    set_backends([mock_backend])

    await _run_stack_update("portainer/10:1", "sonarr")

    entries = get_recent(10)
    assert len(entries) == 1
    assert entries[0]["status"] == "error"
    assert "API error" in entries[0]["error"]
    assert "API error" in get_run_output(entries[0]["id"])[0]


# ---------------------------------------------------------------------------
# apply_host_schedule / apply_stack_schedule
# ---------------------------------------------------------------------------


def test_apply_host_schedule_no_auto_update(config_file):
    """Host without auto_update config is silently skipped."""
    from app.auto_update_scheduler import apply_host_schedule

    apply_host_schedule("test-host")  # Should not raise


def test_apply_host_schedule_with_valid_cron(config_file):
    """Host with valid cron schedule gets a job added."""
    import yaml
    from app.auto_update_scheduler import apply_host_schedule, scheduler

    raw = yaml.safe_load(config_file.read_text())
    raw["hosts"][0]["auto_update"] = {
        "os_enabled": True,
        "os_schedule": "0 3 * * *",
        "auto_reboot": False,
    }
    config_file.write_text(yaml.dump(raw))

    apply_host_schedule("test-host")
    job = scheduler.get_job("auto_os_test-host")
    assert job is not None
    # Clean up
    scheduler.remove_job("auto_os_test-host")


def test_apply_stack_schedule_with_valid_cron(config_file):
    """Stack with valid cron schedule gets a job added."""
    from app.auto_update_scheduler import apply_stack_schedule, scheduler
    from app.config_manager import set_stack_auto_update

    set_stack_auto_update("portainer/10:1", "sonarr", True, "0 4 * * *")
    apply_stack_schedule("portainer/10:1")

    job_id = "auto_stack_portainer_10_1"
    job = scheduler.get_job(job_id)
    assert job is not None
    scheduler.remove_job(job_id)


def test_apply_all_schedules_runs_without_error(config_file):
    """apply_all_schedules processes all hosts and stacks."""
    from app.auto_update_scheduler import apply_all_schedules

    apply_all_schedules()  # Should not raise


@pytest.mark.asyncio
async def test_stack_failure_records_captured_output(config_file):
    """A failed redeploy stores what ran before it broke, not just the message."""
    from app.activity_log import get_recent, get_run_output
    from app.auto_update_scheduler import _run_stack_update, set_backends
    from app.backends.protocol import StackUpdateError

    mock_backend = MagicMock()
    mock_backend.BACKEND_KEY = "ssh"
    mock_backend.update_stack = AsyncMock(
        side_effect=StackUpdateError(
            "docker compose pull failed:\nno such image",
            ["$ docker compose pull", "Error response from daemon: no such image"],
        )
    )
    set_backends([mock_backend])

    await _run_stack_update("ssh/h1/mystack", "mystack")

    entry = get_recent()[0]
    assert entry["status"] == "error"
    assert "Error response from daemon: no such image" in get_run_output(entry["id"])
