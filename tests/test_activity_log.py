"""Tests for the persistent activity log."""

import json
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(autouse=True)
def _paths(data_dir, monkeypatch):
    import app.activity_log as al

    monkeypatch.setattr(al, "_ACTIVITY_DIR", data_dir / "activity")
    monkeypatch.setattr(al, "_INDEX_PATH", data_dir / "activity" / "index.json")
    monkeypatch.setattr(al, "_RUNS_DIR", data_dir / "activity" / "runs")


def _record(**kw):
    from app.activity_log import record_run

    defaults = {
        "kind": "os_upgrade",
        "target": "my-host",
        "target_name": "My Host",
        "trigger": "manual",
        "status": "success",
        "output": ["line1", "line2"],
    }
    return record_run(**{**defaults, **kw})


def test_get_recent_empty():
    from app.activity_log import get_recent

    assert get_recent() == []


def test_record_and_read_back():
    from app.activity_log import get_recent, get_run_output

    run_id = _record()
    assert run_id

    entries = get_recent()
    assert len(entries) == 1
    e = entries[0]
    assert e["id"] == run_id
    assert e["kind"] == "os_upgrade"
    assert e["target"] == "my-host"
    assert e["target_name"] == "My Host"
    assert e["trigger"] == "manual"
    assert e["status"] == "success"
    assert e["line_count"] == 2
    assert e["duration_s"] >= 0
    assert get_run_output(run_id) == ["line1", "line2"]


def test_full_output_is_kept_not_truncated():
    """The old auto_update_log capped at 50 lines; this must not."""
    from app.activity_log import get_run_output

    lines = [f"line{i}" for i in range(400)]
    run_id = _record(output=lines)
    assert get_run_output(run_id) == lines


def test_newest_first():
    from app.activity_log import get_recent

    _record(target="host1")
    _record(target="host2")
    assert [e["target"] for e in get_recent()] == ["host2", "host1"]


def test_get_recent_filters():
    from app.activity_log import get_recent

    _record(target="a", status="error", trigger="scheduled", kind="os_upgrade")
    _record(target="b", status="success", trigger="manual", kind="container_redeploy")

    assert [e["target"] for e in get_recent(status="error")] == ["a"]
    assert [e["target"] for e in get_recent(trigger="manual")] == ["b"]
    assert [e["target"] for e in get_recent(kind="container_redeploy")] == ["b"]
    assert len(get_recent()) == 2


def test_duration_from_started_at():
    from app.activity_log import get_recent

    started = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    _record(started_at=started)
    assert get_recent()[0]["duration_s"] >= 29


def test_prune_by_count(monkeypatch):
    import app.activity_log as al
    from app.activity_log import get_recent

    monkeypatch.setattr(al, "MAX_ENTRIES", 3)
    for i in range(5):
        _record(target=f"host{i}")

    entries = get_recent()
    assert len(entries) == 3
    assert [e["target"] for e in entries] == ["host4", "host3", "host2"]


def test_prune_by_count_deletes_output_files(monkeypatch):
    import app.activity_log as al

    monkeypatch.setattr(al, "MAX_ENTRIES", 2)
    first = _record(target="old")
    for i in range(3):
        _record(target=f"host{i}")

    assert not (al._RUNS_DIR / f"{first}.log").exists()


def test_prune_by_age(monkeypatch):
    from app.activity_log import get_recent

    old = (datetime.now(timezone.utc) - timedelta(days=91)).isoformat()
    _record(target="ancient", started_at=old)
    assert get_recent() == [] or get_recent()[0]["target"] != "ancient"

    _record(target="fresh")
    assert [e["target"] for e in get_recent()] == ["fresh"]


def test_prune_removes_orphan_log_files():
    import app.activity_log as al

    al._RUNS_DIR.mkdir(parents=True, exist_ok=True)
    orphan = al._RUNS_DIR / "deadbeef.log"
    orphan.write_text("no index record points here")

    _record()
    assert not orphan.exists()


def test_index_written_atomically():
    """No .tmp file is left behind after a write."""
    import app.activity_log as al

    _record()
    assert list(al._ACTIVITY_DIR.glob("*.tmp")) == []


def test_corrupt_index_preserved_and_treated_as_empty():
    import app.activity_log as al
    from app.activity_log import get_recent

    al._ACTIVITY_DIR.mkdir(parents=True, exist_ok=True)
    al._INDEX_PATH.write_text("not json!")

    assert get_recent() == []
    assert (al._ACTIVITY_DIR / "index.json.corrupt").read_text() == "not json!"


def test_index_that_is_not_a_list_is_corrupt():
    import app.activity_log as al
    from app.activity_log import get_recent

    al._ACTIVITY_DIR.mkdir(parents=True, exist_ok=True)
    al._INDEX_PATH.write_text(json.dumps({"not": "a list"}))

    assert get_recent() == []
    assert (al._ACTIVITY_DIR / "index.json.corrupt").exists()


def test_record_run_never_raises(monkeypatch):
    """A storage failure must not propagate into the update that called it."""
    import app.activity_log as al

    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(al, "_save_index", boom)
    assert _record() == ""


def test_output_write_failure_still_records_the_run(monkeypatch):
    """If the output file cannot be written, the run is still in the index."""
    import app.activity_log as al
    from app.activity_log import get_recent, get_run_output

    # Make the runs dir un-creatable by putting a file where it belongs.
    al._ACTIVITY_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(al, "_RUNS_DIR", al._ACTIVITY_DIR / "blocked")
    al._RUNS_DIR.write_text("not a directory")

    run_id = _record(output=["boom"])

    assert run_id
    assert get_recent()[0]["line_count"] == 0
    assert "unavailable" in get_run_output(run_id)[0].lower()


def test_missing_output_file_reports_unavailable():
    from app.activity_log import get_run_output

    run_id = _record()
    import app.activity_log as al

    (al._RUNS_DIR / f"{run_id}.log").unlink()
    assert "unavailable" in get_run_output(run_id)[0].lower()


@pytest.mark.parametrize(
    "bad_id",
    ["../../etc/passwd", "..", "abc", "DEADBEEF", "dead beef", "", "a" * 64],
)
def test_get_run_output_rejects_non_hex_ids(bad_id):
    """run_id arrives from a URL path — it must never reach the filesystem raw."""
    from app.activity_log import get_run_output

    assert get_run_output(bad_id) == []


def test_get_run_returns_record():
    from app.activity_log import get_run

    run_id = _record(target="lookup-me")
    assert get_run(run_id)["target"] == "lookup-me"
    assert get_run("00000000") is None
