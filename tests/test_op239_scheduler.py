"""OP#239 — registering the background check job.

The resume rule is the subtle part. A plain `IntervalTrigger` counts from
process start, so a Keepup that restarts more often than its interval would
never run a check at all — a failure indistinguishable from the bug this story
exists to fix.
"""

from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def sched(config_file, data_dir, monkeypatch):
    import app.auto_update_scheduler as aus
    import app.last_check as lc

    monkeypatch.setattr(lc, "_PATH", data_dir / "last_check.json")
    for job in list(aus.scheduler.get_jobs()):
        job.remove()
    yield aus
    for job in list(aus.scheduler.get_jobs()):
        job.remove()


def _record_scan_at(lc_module, when: datetime):
    lc_module._save({"hosts": {"web": when.isoformat()}, "containers": None})


def test_the_job_is_registered_with_the_configured_interval(sched, config_file):
    import app.config_manager as cm

    cm.save_update_check_interval_hours(12)
    sched.apply_update_check_schedule()

    job = sched.scheduler.get_job("update_check")
    assert job is not None
    assert job.trigger.interval == timedelta(hours=12)


def test_off_removes_the_job(sched, config_file):
    import app.config_manager as cm

    cm.save_update_check_interval_hours(6)
    sched.apply_update_check_schedule()
    assert sched.scheduler.get_job("update_check") is not None

    cm.save_update_check_interval_hours(0)
    sched.apply_update_check_schedule()

    assert sched.scheduler.get_job("update_check") is None


def test_reapplying_replaces_rather_than_duplicates(sched, config_file):
    import app.config_manager as cm

    cm.save_update_check_interval_hours(6)
    sched.apply_update_check_schedule()
    cm.save_update_check_interval_hours(24)
    sched.apply_update_check_schedule()

    jobs = [j for j in sched.scheduler.get_jobs() if j.id == "update_check"]
    assert len(jobs) == 1
    assert jobs[0].trigger.interval == timedelta(hours=24)


def test_first_run_resumes_from_the_last_check(sched, config_file, data_dir):
    """Checked 5h ago on a 6h interval → roughly an hour from now, not six."""
    import app.config_manager as cm
    import app.last_check as lc

    _record_scan_at(lc, datetime.now(timezone.utc) - timedelta(hours=5))
    cm.save_update_check_interval_hours(6)

    sched.apply_update_check_schedule()

    start = sched.scheduler.get_job("update_check").trigger.start_date
    delay = start - datetime.now(timezone.utc)
    assert timedelta(minutes=30) < delay < timedelta(hours=1, minutes=30)


def test_an_overdue_check_runs_soon_but_never_in_the_past(sched, config_file, data_dir):
    """Down for three days: due immediately, but a start_date in the past would
    have APScheduler fire it before the app has finished coming up."""
    import app.config_manager as cm
    import app.last_check as lc

    _record_scan_at(lc, datetime.now(timezone.utc) - timedelta(days=3))
    cm.save_update_check_interval_hours(6)

    sched.apply_update_check_schedule()

    start = sched.scheduler.get_job("update_check").trigger.start_date
    delay = start - datetime.now(timezone.utc)
    assert timedelta(seconds=0) < delay < timedelta(minutes=5)


def test_a_never_checked_install_waits_one_interval(sched, config_file):
    """Matches the card's copy: "first check in about 6 hours"."""
    import app.config_manager as cm

    cm.save_update_check_interval_hours(6)
    sched.apply_update_check_schedule()

    start = sched.scheduler.get_job("update_check").trigger.start_date
    delay = start - datetime.now(timezone.utc)
    assert timedelta(hours=5, minutes=30) < delay < timedelta(hours=6, minutes=30)


def test_apply_all_schedules_registers_the_check_job(sched, config_file):
    """Startup must not need a separate call the caller can forget."""
    import app.config_manager as cm

    cm.save_update_check_interval_hours(6)
    sched.apply_all_schedules()

    assert sched.scheduler.get_job("update_check") is not None


@pytest.mark.asyncio
async def test_the_job_body_runs_a_scan(sched, config_file, monkeypatch):
    from unittest.mock import AsyncMock

    ran = AsyncMock()
    monkeypatch.setattr("app.update_scan.run_scan", ran)

    await sched._run_update_check()

    ran.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_failing_scan_is_logged_not_swallowed(sched, config_file, caplog):
    """A scan that dies must say so — a silent job is the original defect."""
    import logging
    from unittest.mock import AsyncMock

    caplog.set_level(logging.WARNING)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.update_scan.run_scan", AsyncMock(side_effect=TimeoutError()))
        await sched._run_update_check()

    assert "TimeoutError" in caplog.text
