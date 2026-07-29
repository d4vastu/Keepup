"""Tests for the persistent activity log."""

import json
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(autouse=True)
def _use_data_dir(data_dir):
    """Activate the data_dir fixture for every test in this module.

    data_dir (in conftest.py) already monkeypatches app.activity_log's
    module-level paths onto the temp dir; this just makes every test request
    it without re-patching the same attributes a second time here.
    """
    return data_dir


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


def test_get_recent_limit_truncates():
    from app.activity_log import get_recent

    for i in range(5):
        _record(target=f"host{i}")

    limited = get_recent(limit=2)
    assert len(limited) == 2
    assert [e["target"] for e in limited] == ["host4", "host3"]


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


def test_prune_by_age():
    """An existing record older than MAX_AGE_DAYS is dropped once a new run
    triggers pruning.

    Note: the *just-inserted* record is exempt from this cutoff (see
    test_record_run_id_is_never_pruned_on_its_own_insertion) — so this seeds
    the ancient record directly into the index rather than via record_run,
    to test age-pruning of a pre-existing record deterministically.
    """
    import app.activity_log as al
    from app.activity_log import get_recent

    al._ACTIVITY_DIR.mkdir(parents=True, exist_ok=True)
    old = (datetime.now(timezone.utc) - timedelta(days=91)).isoformat()
    al._INDEX_PATH.write_text(
        json.dumps(
            [
                {
                    "id": "aaaaaaaa",
                    "kind": "os_upgrade",
                    "target": "ancient",
                    "target_name": "Ancient",
                    "trigger": "manual",
                    "status": "success",
                    "started_at": old,
                    "finished_at": old,
                    "duration_s": 0,
                    "error": "",
                    "line_count": 0,
                }
            ]
        )
    )

    _record(target="fresh")
    assert [e["target"] for e in get_recent()] == ["fresh"]


def test_record_run_id_is_never_pruned_on_its_own_insertion():
    """A skewed host clock (started_at far in the past) must not make
    record_run hand back an id that's already gone by the time it returns."""
    from app.activity_log import get_recent, get_run

    old = (datetime.now(timezone.utc) - timedelta(days=91)).isoformat()
    run_id = _record(target="clock-skew", started_at=old)

    assert run_id
    assert get_run(run_id) is not None
    assert [e["target"] for e in get_recent()] == ["clock-skew"]


def test_prune_removes_orphan_log_files():
    """An orphan .log file with no index record at all — provenance unknown —
    is swept only once it's old enough (past MAX_AGE_DAYS) to be unlikely to
    be the only surviving copy of something worth recovering."""
    import os

    import app.activity_log as al

    al._RUNS_DIR.mkdir(parents=True, exist_ok=True)
    orphan = al._RUNS_DIR / "deadbeef.log"
    orphan.write_text("no index record points here")
    old_time = (datetime.now(timezone.utc) - timedelta(days=al.MAX_AGE_DAYS + 1)).timestamp()
    os.utime(orphan, (old_time, old_time))

    _record()
    assert not orphan.exists()


def test_orphan_log_file_newer_than_cutoff_survives():
    """A fresh orphan (mtime within MAX_AGE_DAYS) must not be swept just
    because a record_run happens to run pruning — its provenance is unknown
    and it might be the only surviving copy of a failed run's output."""
    import app.activity_log as al

    al._RUNS_DIR.mkdir(parents=True, exist_ok=True)
    orphan = al._RUNS_DIR / "deadbeef.log"
    orphan.write_text("recent orphan, provenance unknown")

    _record()
    assert orphan.exists()


def test_save_index_failure_leaves_previous_index_intact(monkeypatch):
    """A failed atomic replace (e.g. disk full) must not corrupt or drop the
    previously committed index — only the new record is lost, not history.

    A naive write_text() with no tmp file would pass a weaker assertion here
    (e.g. "no .tmp file is left behind") even without real atomicity, so this
    forces os.replace to fail mid-write and checks the prior state survives.
    """
    import app.activity_log as al
    from app.activity_log import get_recent

    _record(target="safe")
    assert [e["target"] for e in get_recent()] == ["safe"]

    real_replace = al.os.replace

    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(al.os, "replace", boom)
    assert _record(target="doomed") == ""

    monkeypatch.setattr(al.os, "replace", real_replace)
    assert [e["target"] for e in get_recent()] == ["safe"]


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


def test_index_with_non_dict_entries_does_not_wedge_the_module():
    """A list that parses fine but holds junk elements (e.g. a hand-edited or
    half-written index) must not crash every later record_run/get_recent/
    get_run call with an AttributeError on `.get`."""
    import app.activity_log as al
    from app.activity_log import get_recent, get_run

    al._ACTIVITY_DIR.mkdir(parents=True, exist_ok=True)
    al._INDEX_PATH.write_text(json.dumps(["junk", 5]))

    assert get_recent() == []
    assert get_recent(status="error") == []
    assert get_run("deadbeef") is None

    run_id = _record(target="recovered")
    assert run_id
    assert [e["target"] for e in get_recent()] == ["recovered"]


def test_concurrent_record_run_keeps_every_output_file():
    """The lock must cover the output write and the index update together.

    Splitting them lets one thread's prune sweep (computed from an index that
    doesn't yet contain a second thread's just-written record) delete that
    second thread's output file before its index record exists — leaving an
    index entry with line_count > 0 pointing at a file that isn't there.
    """
    import threading

    from app.activity_log import get_run_output, record_run

    n = 40
    results: list[str] = [""] * n
    lock = threading.Lock()

    def worker(i):
        run_id = record_run(
            kind="os_upgrade",
            target=f"host{i}",
            target_name=f"Host {i}",
            trigger="manual",
            status="success",
            output=[f"line-{i}"],
        )
        with lock:
            results[i] = run_id

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(results)
    assert len(set(results)) == n
    for i, run_id in enumerate(results):
        assert get_run_output(run_id) == [f"line-{i}"]


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


def test_output_with_non_string_elements_does_not_lose_the_record():
    """A non-str element (e.g. a stray None or exit code) in the output list
    must not escape as a TypeError and take the whole metadata record down
    with it — only the offending line should be coerced to text."""
    from app.activity_log import get_recent, get_run_output

    run_id = _record(output=["ok", None, 3])

    assert run_id
    assert get_recent()[0]["target"] == "my-host"
    assert get_run_output(run_id) == ["ok", "None", "3"]


def test_output_line_count_matches_embedded_newlines_on_read_back():
    """A caller appending a multi-line chunk as one list element (job runners
    do) must get back the same number of lines it wrote, not fewer."""
    from app.activity_log import get_recent, get_run_output

    run_id = _record(output=["step 1\nstep 2", "step 3"])

    assert get_recent()[0]["line_count"] == 3
    assert get_run_output(run_id) == ["step 1", "step 2", "step 3"]


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
