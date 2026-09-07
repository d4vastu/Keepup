"""OP#239 — the persisted "last checked" store behind the dashboard timestamps.

Written by both the background job and the on-demand dashboard checks: the
timestamp means "when we last actually checked", whatever triggered it. It has
to survive a restart, so `update_check_cache`'s process-local dict is not enough
— that keeps its separate job of suppressing redundant `apt-get update` runs.
"""

from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def store(data_dir, monkeypatch):
    import app.last_check as lc

    monkeypatch.setattr(lc, "_PATH", data_dir / "last_check.json")
    return lc


def _ago(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


def test_nothing_recorded_reads_as_never(store):
    assert store.oldest_host_check([]) is None
    assert store.oldest_host_check(["web"]) is None
    assert store.container_check() is None


def test_a_host_check_is_written_to_disk(store):
    """Persisted, not process-local: the dashboard renders it on first paint,
    before any check of its own has finished."""
    import json

    store.record_host_check("web")

    on_disk = json.loads(store._PATH.read_text())
    assert "web" in on_disk["hosts"]
    assert store.oldest_host_check(["web"]) is not None


def test_section_shows_the_oldest_host_not_the_newest(store):
    """"checked 14m ago" must mean nothing in the section is staler than that.

    Showing the newest would let one freshly-refreshed host hide a section that
    has otherwise not been checked in days.
    """
    store._save({"hosts": {"web": _ago(minutes=5), "db": _ago(hours=9)}, "containers": None})

    oldest = store.oldest_host_check(["web", "db"])

    assert (datetime.now(timezone.utc) - oldest) > timedelta(hours=8)


def test_hosts_never_checked_are_ignored_by_the_aggregate(store):
    store._save({"hosts": {"web": _ago(minutes=5)}, "containers": None})

    oldest = store.oldest_host_check(["web", "brand-new-host"])

    assert oldest is not None
    assert (datetime.now(timezone.utc) - oldest) < timedelta(hours=1)


def test_a_removed_host_does_not_hold_the_section_back(store):
    """Only the hosts asked about count — a deleted host's stale entry must not
    make the whole section look unchecked forever."""
    store._save({"hosts": {"web": _ago(minutes=5), "deleted": _ago(days=30)}, "containers": None})

    oldest = store.oldest_host_check(["web"])

    assert (datetime.now(timezone.utc) - oldest) < timedelta(hours=1)


def test_container_check_is_recorded_separately(store):
    store.record_container_check()

    assert store.container_check() is not None
    assert store.oldest_host_check(["web"]) is None


def test_corrupt_store_reads_as_never_rather_than_raising(store):
    store._PATH.parent.mkdir(parents=True, exist_ok=True)
    store._PATH.write_text("NOT JSON{{")

    assert store.oldest_host_check(["web"]) is None
    assert store.container_check() is None


def test_recording_after_corruption_repairs_the_store(store):
    store._PATH.parent.mkdir(parents=True, exist_ok=True)
    store._PATH.write_text("NOT JSON{{")

    store.record_host_check("web")

    assert store.oldest_host_check(["web"]) is not None


# ---------------------------------------------------------------------------
# Relative formatting — what the dashboard actually renders
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({"seconds": 5}, "just now"),
        ({"minutes": 14}, "14m ago"),
        ({"hours": 3}, "3h ago"),
        ({"days": 2}, "2d ago"),
    ],
)
def test_relative_wording(store, kwargs, expected):
    dt = datetime.now(timezone.utc) - timedelta(**kwargs)
    assert store.relative(dt) == expected


def test_relative_of_nothing_says_never(store):
    assert store.relative(None) == "never checked"
