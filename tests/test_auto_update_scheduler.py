"""Tests for auto_update_scheduler module."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture(autouse=True)
def _setup(config_file, data_dir, monkeypatch):
    """Each test gets isolated config and data dirs, and a clean log."""
    import app.auto_update_log as log_mod

    monkeypatch.setattr(log_mod, "_LOG_PATH", data_dir / "auto_update_log.json")


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
    from app.auto_update_log import get_recent

    with patch(
        "app.auto_update_scheduler.run_host_update_buffered", new=AsyncMock()
    ) as mock_update:
        await _run_os_update("test-host")

    mock_update.assert_not_called()
    assert get_recent(10) == []


@pytest.mark.asyncio
async def test_run_os_update_success(config_file):
    """Successful update logs to auto_update_log."""
    import yaml
    from app.auto_update_scheduler import _run_os_update
    from app.auto_update_log import get_recent

    # Enable auto-update for test-host
    raw = yaml.safe_load(config_file.read_text())
    raw["hosts"][0]["auto_update"] = {
        "os_enabled": True,
        "os_schedule": "0 3 * * *",
        "auto_reboot": False,
    }
    config_file.write_text(yaml.dump(raw))

    with patch(
        "app.auto_update_scheduler.run_host_update_buffered",
        new=AsyncMock(return_value=["Package updated."]),
    ):
        await _run_os_update("test-host")

    entries = get_recent(10)
    assert len(entries) == 1
    assert entries[0]["status"] == "success"
    assert entries[0]["target"] == "test-host"


@pytest.mark.asyncio
async def test_run_os_update_failure_logs_error(config_file):
    """Exception during update is logged as error."""
    import yaml
    from app.auto_update_scheduler import _run_os_update
    from app.auto_update_log import get_recent

    raw = yaml.safe_load(config_file.read_text())
    raw["hosts"][0]["auto_update"] = {
        "os_enabled": True,
        "os_schedule": "0 3 * * *",
        "auto_reboot": False,
    }
    config_file.write_text(yaml.dump(raw))

    with patch(
        "app.auto_update_scheduler.run_host_update_buffered",
        new=AsyncMock(side_effect=Exception("SSH timeout")),
    ):
        await _run_os_update("test-host")

    entries = get_recent(10)
    assert len(entries) == 1
    assert entries[0]["status"] == "error"
    assert "SSH timeout" in entries[0]["lines"][0]


@pytest.mark.asyncio
async def test_run_os_update_sudo_required_but_no_password(config_file):
    """If sudo is needed but not stored, log an error and skip."""
    import yaml
    from app.auto_update_scheduler import _run_os_update
    from app.auto_update_log import get_recent

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
            "app.auto_update_scheduler.run_host_update_buffered", new=AsyncMock()
        ) as mock_update,
    ):
        await _run_os_update("test-host")

    mock_update.assert_not_called()
    entries = get_recent(10)
    assert len(entries) == 1
    assert entries[0]["status"] == "error"


@pytest.mark.asyncio
async def test_run_os_update_with_auto_reboot(config_file):
    """When auto_reboot=True and reboot_required, reboot is called."""
    import yaml
    from app.auto_update_scheduler import _run_os_update
    from app.auto_update_log import get_recent

    raw = yaml.safe_load(config_file.read_text())
    raw["hosts"][0]["auto_update"] = {
        "os_enabled": True,
        "os_schedule": "0 3 * * *",
        "auto_reboot": True,
    }
    config_file.write_text(yaml.dump(raw))

    with (
        patch(
            "app.auto_update_scheduler.run_host_update_buffered",
            new=AsyncMock(return_value=["Updated."]),
        ),
        patch(
            "app.auto_update_scheduler.check_host_updates",
            new=AsyncMock(return_value={"packages": [], "reboot_required": True}),
        ),
        patch(
            "app.auto_update_scheduler.reboot_host", new=AsyncMock(return_value=[])
        ) as mock_reboot,
    ):
        await _run_os_update("test-host")

    mock_reboot.assert_called_once()
    entries = get_recent(10)
    assert any(e["status"] == "success" for e in entries)


# ---------------------------------------------------------------------------
# _run_stack_update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_stack_update_invalid_path(config_file):
    """Invalid update_path format logs error."""
    from app.auto_update_scheduler import _run_stack_update
    from app.auto_update_log import get_recent

    await _run_stack_update("badformat", "mystack")

    entries = get_recent(10)
    assert len(entries) == 1
    assert entries[0]["status"] == "error"


@pytest.mark.asyncio
async def test_run_stack_update_no_backend(config_file, monkeypatch):
    """Missing backend logs error."""
    from app.auto_update_scheduler import _run_stack_update, set_backends
    from app.auto_update_log import get_recent

    set_backends([])
    await _run_stack_update("portainer/10:1", "sonarr")

    entries = get_recent(10)
    assert len(entries) == 1
    assert entries[0]["status"] == "error"
    assert "portainer" in entries[0]["lines"][0]


@pytest.mark.asyncio
async def test_run_stack_update_success(config_file):
    """Successful stack update logs success."""
    from app.auto_update_scheduler import _run_stack_update, set_backends
    from app.auto_update_log import get_recent

    mock_backend = MagicMock()
    mock_backend.BACKEND_KEY = "portainer"
    mock_backend.update_stack = AsyncMock(return_value=None)
    set_backends([mock_backend])

    await _run_stack_update("portainer/10:1", "sonarr")

    entries = get_recent(10)
    assert len(entries) == 1
    assert entries[0]["status"] == "success"
    mock_backend.update_stack.assert_called_once_with("10:1")


@pytest.mark.asyncio
async def test_run_stack_update_failure_logs_error(config_file):
    """Exception during stack update is logged as error."""
    from app.auto_update_scheduler import _run_stack_update, set_backends
    from app.auto_update_log import get_recent

    mock_backend = MagicMock()
    mock_backend.BACKEND_KEY = "portainer"
    mock_backend.update_stack = AsyncMock(side_effect=Exception("API error"))
    set_backends([mock_backend])

    await _run_stack_update("portainer/10:1", "sonarr")

    entries = get_recent(10)
    assert len(entries) == 1
    assert entries[0]["status"] == "error"
    assert "API error" in entries[0]["lines"][0]


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
