"""OP#239 — per-host notification dedup.

Container updates have been deduplicated on `update_path` since OP#217. Hosts
had no such store, because `host_check` never notified at all. A six-hourly job
without dedup would re-announce the same pending packages forever.
"""

import pytest


@pytest.fixture
def notifier(data_dir, monkeypatch):
    import app.update_notifier as un

    monkeypatch.setattr(un, "_PATH", data_dir / "notified_updates.json")
    return un


def test_first_sighting_notifies(notifier):
    assert notifier.should_notify_host("web", True) is True


def test_the_same_pending_updates_do_not_notify_twice(notifier):
    notifier.should_notify_host("web", True)
    assert notifier.should_notify_host("web", True) is False


def test_a_host_with_nothing_pending_never_notifies(notifier):
    assert notifier.should_notify_host("web", False) is False


def test_going_up_to_date_rearms_the_host(notifier):
    """Cleared when the host is clean again, so the next real update is heard."""
    notifier.should_notify_host("web", True)
    notifier.should_notify_host("web", False)

    assert notifier.should_notify_host("web", True) is True


def test_hosts_are_tracked_independently(notifier):
    notifier.should_notify_host("web", True)

    assert notifier.should_notify_host("db", True) is True


def test_the_record_survives_a_restart(notifier):
    """Otherwise every container restart re-announces every pending update."""
    import json

    notifier.should_notify_host("web", True)

    on_disk = json.loads(notifier._PATH.read_text())
    assert "web" in on_disk["notified_hosts"]


def test_container_dedup_is_untouched_by_host_dedup(notifier):
    """The two stores share a file; neither may clobber the other."""
    state = notifier._load()
    state["notified"] = ["portainer/3:1"]
    notifier._save(state)

    notifier.should_notify_host("web", True)

    assert "portainer/3:1" in notifier._load()["notified"]
    assert notifier.should_notify_host("web", True) is False


def test_a_pre_op239_store_without_the_host_key_still_loads(notifier):
    """Upgrading with an existing store must not crash or lose container dedup."""
    notifier._PATH.parent.mkdir(parents=True, exist_ok=True)
    notifier._PATH.write_text('{"notified": ["portainer/3:1"], "unknown_since": {}}')

    assert "portainer/3:1" in notifier._load()["notified"]
    assert notifier.should_notify_host("web", True) is True
