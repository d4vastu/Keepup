"""OP#239 — the "Update checks" card, per the OP#237 design.

One dropdown whose options include Off, at the top of the Auto-Updates page.
The status line has to tell the truth about what will happen next, because a
user who turns checking off is choosing exactly the behaviour this story exists
to remove.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolated_last_check(data_dir, monkeypatch):
    import app.last_check as lc

    monkeypatch.setattr(lc, "_PATH", data_dir / "last_check.json")


def test_the_card_is_on_the_auto_updates_page(client):
    response = client.get("/admin/auto-updates")

    assert response.status_code == 200
    assert "Update checks" in response.text
    assert "Check every" in response.text


def test_every_offered_interval_is_present(client):
    response = client.get("/admin/auto-updates")

    for label in ("Off", "Every hour", "Every 6 hours", "Every 12 hours", "Every 24 hours"):
        assert label in response.text, f"missing option: {label}"


def test_the_stored_interval_is_preselected(client, config_file):
    import app.config_manager as cm

    cm.save_update_check_interval_hours(12)

    response = client.get("/admin/auto-updates")

    assert 'value="12" selected' in response.text.replace("'", '"')


def test_saving_the_interval_persists_it(client, config_file):
    import app.config_manager as cm

    response = client.post("/admin/auto-updates/check-interval", data={"interval_hours": "24"})

    assert response.status_code == 200
    assert cm.get_update_check_interval_hours() == 24


def test_saving_reschedules_the_job(client, config_file):
    import app.auto_update_scheduler as aus

    client.post("/admin/auto-updates/check-interval", data={"interval_hours": "1"})

    job = aus.scheduler.get_job("update_check")
    assert job is not None, "saving the setting must take effect without a restart"
    job.remove()


def test_saving_off_removes_the_job(client, config_file):
    import app.auto_update_scheduler as aus

    client.post("/admin/auto-updates/check-interval", data={"interval_hours": "6"})
    client.post("/admin/auto-updates/check-interval", data={"interval_hours": "0"})

    assert aus.scheduler.get_job("update_check") is None


def test_turning_it_off_says_what_that_costs(client, config_file):
    """"Disabled" is not an explanation. The copy names the consequence."""
    response = client.post("/admin/auto-updates/check-interval", data={"interval_hours": "0"})

    assert "only look for updates while the dashboard is open" in response.text


def test_a_never_checked_install_says_so(client, config_file):
    response = client.get("/admin/auto-updates")

    assert "No check has run yet" in response.text


def test_a_completed_check_is_reported_with_its_age(client, config_file, data_dir):
    from datetime import datetime, timedelta, timezone

    import app.last_check as lc

    lc._save(
        {
            "hosts": {},
            "containers": (datetime.now(timezone.utc) - timedelta(minutes=14)).isoformat(),
        }
    )

    response = client.get("/admin/auto-updates")

    assert "14m ago" in response.text


def test_a_junk_interval_is_rejected_without_a_crash(client, config_file):
    import app.config_manager as cm

    cm.save_update_check_interval_hours(12)

    response = client.post(
        "/admin/auto-updates/check-interval", data={"interval_hours": "banana"}
    )

    assert response.status_code == 200
    assert cm.get_update_check_interval_hours() in (6, 12)


# ---------------------------------------------------------------------------
# The setup wizard writes the same setting
# ---------------------------------------------------------------------------


def test_the_wizard_writes_the_real_interval(client, config_file):
    """The wizard asked this question for years and stored a cron nothing read."""
    import app.config_manager as cm

    client.post("/setup/notifications/schedule/save", data={"update_schedule": "12h"})

    assert cm.get_update_check_interval_hours() == 12


def test_the_wizard_no_longer_promises_a_time_of_day(client):
    """It is an interval now — "Daily (2 am)" would be a lie."""
    response = client.get("/setup/notifications")

    assert "2 am" not in response.text
