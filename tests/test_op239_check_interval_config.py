"""OP#239 — the background check interval setting, and its migration.

A dead setting for this already existed: the setup wizard asked 6h / 12h / 24h /
manual and stored a *cron string* at `update_check_schedule`, which nothing ever
read. Rather than stand a second key up beside it, the cron is migrated to
`update_checks.interval_hours` on first read and the old key deleted, so the
wizard and the admin card cannot end up describing different schedules.
"""

import yaml


def _write(cfg_path, data):
    cfg_path.write_text(yaml.dump(data, default_flow_style=False))


def test_interval_defaults_to_six_hours_when_nothing_is_stored(config_file):
    """A fresh install checks every 6 hours without being told to."""
    import app.config_manager as cm

    assert cm.get_update_check_interval_hours() == 6


def test_stored_interval_is_returned(config_file):
    import app.config_manager as cm

    _write(config_file, {"hosts": [], "update_checks": {"interval_hours": 12}})
    assert cm.get_update_check_interval_hours() == 12


def test_zero_means_off_and_survives_a_reread(config_file):
    """0 is a real stored value, not an absent one falling back to the default."""
    import app.config_manager as cm

    cm.save_update_check_interval_hours(0)
    assert cm.get_update_check_interval_hours() == 0


def test_legacy_cron_schedule_migrates_to_hours(config_file):
    """The wizard's dead cron string becomes the new interval, once."""
    import app.config_manager as cm

    _write(config_file, {"hosts": [], "update_check_schedule": "0 */12 * * *"})

    assert cm.get_update_check_interval_hours() == 12

    stored = yaml.safe_load(config_file.read_text())
    assert stored["update_checks"]["interval_hours"] == 12
    assert "update_check_schedule" not in stored, "the legacy key must be removed"


def test_legacy_daily_cron_migrates_to_24(config_file):
    import app.config_manager as cm

    _write(config_file, {"hosts": [], "update_check_schedule": "0 2 * * *"})
    assert cm.get_update_check_interval_hours() == 24


def test_unrecognised_legacy_cron_falls_back_to_the_default(config_file):
    """A hand-edited cron we cannot express as an interval must not become 0.

    Silently turning checking off is the failure this whole story exists to fix.
    """
    import app.config_manager as cm

    _write(config_file, {"hosts": [], "update_check_schedule": "*/7 3 * * 1-5"})

    assert cm.get_update_check_interval_hours() == 6
    stored = yaml.safe_load(config_file.read_text())
    assert "update_check_schedule" not in stored


def test_migration_leaves_an_explicit_new_setting_alone(config_file):
    """If both keys exist the new one wins and the old one is dropped."""
    import app.config_manager as cm

    _write(
        config_file,
        {
            "hosts": [],
            "update_check_schedule": "0 */6 * * *",
            "update_checks": {"interval_hours": 24},
        },
    )

    assert cm.get_update_check_interval_hours() == 24
    stored = yaml.safe_load(config_file.read_text())
    assert "update_check_schedule" not in stored


def test_saving_the_interval_does_not_disturb_other_settings(config_file):
    import app.config_manager as cm

    _write(config_file, {"hosts": [], "pushover": {"enabled": True}})

    cm.save_update_check_interval_hours(12)

    stored = yaml.safe_load(config_file.read_text())
    assert stored["pushover"] == {"enabled": True}
    assert stored["update_checks"]["interval_hours"] == 12


def test_saving_pushover_does_not_reset_the_interval(config_file):
    """`save_pushover_config` replaces its whole block — the interval lives
    outside it precisely so the enable checkbox cannot wipe the schedule."""
    import app.config_manager as cm

    cm.save_update_check_interval_hours(12)
    cm.save_pushover_config(enabled=True)

    assert cm.get_update_check_interval_hours() == 12


def test_an_out_of_range_interval_is_rejected(config_file):
    """Only the offered choices are storable; a stray value falls back."""
    import app.config_manager as cm

    _write(config_file, {"hosts": [], "update_checks": {"interval_hours": -3}})
    assert cm.get_update_check_interval_hours() == 6

    _write(config_file, {"hosts": [], "update_checks": {"interval_hours": "soon"}})
    assert cm.get_update_check_interval_hours() == 6
