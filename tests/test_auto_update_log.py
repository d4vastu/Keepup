"""Tests for auto_update_log module."""
import pytest


@pytest.fixture(autouse=True)
def _use_data_dir(data_dir, monkeypatch):
    import app.auto_update_log as log_mod
    monkeypatch.setattr(log_mod, "_LOG_PATH", data_dir / "auto_update_log.json")


def test_get_recent_empty():
    from app.auto_update_log import get_recent
    assert get_recent(10) == []


def test_get_unread_error_count_empty():
    from app.auto_update_log import get_unread_error_count
    assert get_unread_error_count() == 0


def test_append_and_get_recent():
    from app.auto_update_log import append_log, get_recent
    append_log("os", "my-host", "My Host", "success", ["line1", "line2"])
    entries = get_recent(10)
    assert len(entries) == 1
    assert entries[0]["target"] == "my-host"
    assert entries[0]["target_name"] == "My Host"
    assert entries[0]["status"] == "success"
    assert entries[0]["type"] == "os"
    assert entries[0]["lines"] == ["line1", "line2"]
    assert entries[0]["read"] is False


def test_append_multiple_entries_newest_first():
    from app.auto_update_log import append_log, get_recent
    append_log("os", "host1", "Host 1", "success", [])
    append_log("os", "host2", "Host 2", "error", [])
    entries = get_recent(10)
    assert len(entries) == 2
    # Most recent first
    assert entries[0]["target"] == "host2"
    assert entries[1]["target"] == "host1"


def test_get_recent_limit():
    from app.auto_update_log import append_log, get_recent
    for i in range(5):
        append_log("os", f"host{i}", f"Host {i}", "success", [])
    entries = get_recent(3)
    assert len(entries) == 3


def test_get_unread_error_count():
    from app.auto_update_log import append_log, get_unread_error_count
    append_log("os", "h1", "H1", "error", [])
    append_log("os", "h2", "H2", "error", [])
    append_log("os", "h3", "H3", "success", [])
    assert get_unread_error_count() == 2


def test_mark_all_read():
    from app.auto_update_log import append_log, get_recent, mark_all_read, get_unread_error_count
    append_log("os", "h1", "H1", "error", [])
    append_log("os", "h2", "H2", "error", [])
    mark_all_read()
    assert get_unread_error_count() == 0
    entries = get_recent(10)
    assert all(e["read"] is True for e in entries)


def test_append_log_lines_capped_at_50():
    from app.auto_update_log import append_log, get_recent
    lines = [f"line{i}" for i in range(100)]
    append_log("os", "h1", "H1", "success", lines)
    entry = get_recent(1)[0]
    assert len(entry["lines"]) == 50
    # Should be last 50 lines
    assert entry["lines"][0] == "line50"
    assert entry["lines"][-1] == "line99"


def test_append_log_docker_type():
    from app.auto_update_log import append_log, get_recent
    append_log("docker", "portainer/10:1", "sonarr", "success", ["Updated."])
    entry = get_recent(1)[0]
    assert entry["type"] == "docker"
    assert entry["target"] == "portainer/10:1"


def test_load_handles_corrupt_file(data_dir):
    """Corrupt JSON file should not crash — returns empty list."""
    import app.auto_update_log as log_mod
    log_mod._LOG_PATH.write_text("not json!")
    from app.auto_update_log import get_recent
    assert get_recent(10) == []
