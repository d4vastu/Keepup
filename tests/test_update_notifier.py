"""Tests for app/update_notifier.py — deduplication store for container notifications."""
import json
from unittest.mock import patch


def _make_notifier(tmp_path, monkeypatch):
    import app.update_notifier as un
    path = tmp_path / "notified_updates.json"
    monkeypatch.setattr(un, "_PATH", path)
    return un


# ---------------------------------------------------------------------------
# _load()
# ---------------------------------------------------------------------------

def test_load_returns_empty_when_no_file(tmp_path, monkeypatch):
    un = _make_notifier(tmp_path, monkeypatch)
    assert un._load() == set()


def test_load_returns_set_from_valid_file(tmp_path, monkeypatch):
    un = _make_notifier(tmp_path, monkeypatch)
    (tmp_path / "notified_updates.json").write_text(json.dumps(["a/img", "b/img"]))
    result = un._load()
    assert result == {"a/img", "b/img"}


def test_load_returns_empty_on_corrupt_file(tmp_path, monkeypatch):
    un = _make_notifier(tmp_path, monkeypatch)
    (tmp_path / "notified_updates.json").write_text("NOT JSON{{")
    assert un._load() == set()


def test_load_returns_empty_when_json_is_not_list(tmp_path, monkeypatch):
    un = _make_notifier(tmp_path, monkeypatch)
    (tmp_path / "notified_updates.json").write_text(json.dumps({"key": "val"}))
    assert un._load() == set()


# ---------------------------------------------------------------------------
# _save()
# ---------------------------------------------------------------------------

def test_save_writes_sorted_json(tmp_path, monkeypatch):
    un = _make_notifier(tmp_path, monkeypatch)
    un._save({"c/img", "a/img", "b/img"})
    data = json.loads((tmp_path / "notified_updates.json").read_text())
    assert data == ["a/img", "b/img", "c/img"]


# ---------------------------------------------------------------------------
# check_and_notify()
# ---------------------------------------------------------------------------

def test_check_and_notify_fires_for_new_update(tmp_path, monkeypatch):
    un = _make_notifier(tmp_path, monkeypatch)
    stacks = [{"update_path": "ghcr.io/myapp", "update_status": "update_available", "name": "myapp"}]

    with patch("app.notifications.notify") as mock_notify:
        un.check_and_notify(stacks)

    mock_notify.assert_called_once()
    title = mock_notify.call_args[0][0]
    assert "myapp" in title
    # Entry persisted
    assert "ghcr.io/myapp" in un._load()


def test_check_and_notify_does_not_double_notify(tmp_path, monkeypatch):
    un = _make_notifier(tmp_path, monkeypatch)
    # Pre-populate so the stack is already known
    un._save({"ghcr.io/myapp"})
    stacks = [{"update_path": "ghcr.io/myapp", "update_status": "update_available", "name": "myapp"}]

    with patch("app.notifications.notify") as mock_notify:
        un.check_and_notify(stacks)

    mock_notify.assert_not_called()


def test_check_and_notify_clears_on_up_to_date(tmp_path, monkeypatch):
    un = _make_notifier(tmp_path, monkeypatch)
    un._save({"ghcr.io/myapp"})
    stacks = [{"update_path": "ghcr.io/myapp", "update_status": "up_to_date", "name": "myapp"}]

    with patch("app.notifications.notify"):
        un.check_and_notify(stacks)

    assert "ghcr.io/myapp" not in un._load()


def test_check_and_notify_clears_on_mixed(tmp_path, monkeypatch):
    un = _make_notifier(tmp_path, monkeypatch)
    un._save({"ghcr.io/myapp"})
    stacks = [{"update_path": "ghcr.io/myapp", "update_status": "mixed", "name": "myapp"}]

    with patch("app.notifications.notify"):
        un.check_and_notify(stacks)

    assert "ghcr.io/myapp" not in un._load()


def test_check_and_notify_no_change_does_not_save(tmp_path, monkeypatch):
    un = _make_notifier(tmp_path, monkeypatch)
    stacks = [{"update_path": "ghcr.io/myapp", "update_status": "up_to_date", "name": "myapp"}]

    with patch("app.notifications.notify"):
        un.check_and_notify(stacks)

    # File should not have been created
    assert not (tmp_path / "notified_updates.json").exists()
