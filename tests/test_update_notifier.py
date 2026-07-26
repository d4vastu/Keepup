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


_EMPTY = {"notified": [], "unknown_since": {}, "unknown_notified": []}


def test_load_returns_empty_when_no_file(tmp_path, monkeypatch):
    un = _make_notifier(tmp_path, monkeypatch)
    assert un._load() == _EMPTY


def test_load_migrates_legacy_list(tmp_path, monkeypatch):
    """The pre-OP#217 store was a bare list of notified paths."""
    un = _make_notifier(tmp_path, monkeypatch)
    (tmp_path / "notified_updates.json").write_text(json.dumps(["a/img", "b/img"]))
    result = un._load()
    assert result["notified"] == ["a/img", "b/img"]
    assert result["unknown_since"] == {}


def test_load_returns_empty_on_corrupt_file(tmp_path, monkeypatch):
    un = _make_notifier(tmp_path, monkeypatch)
    (tmp_path / "notified_updates.json").write_text("NOT JSON{{")
    assert un._load() == _EMPTY


def test_load_returns_empty_state_for_unrecognised_dict(tmp_path, monkeypatch):
    """A dict without the known keys is a valid-but-empty store, not a crash."""
    un = _make_notifier(tmp_path, monkeypatch)
    (tmp_path / "notified_updates.json").write_text(json.dumps({"key": "val"}))
    assert un._load() == _EMPTY


def test_load_returns_empty_when_json_is_scalar(tmp_path, monkeypatch):
    un = _make_notifier(tmp_path, monkeypatch)
    (tmp_path / "notified_updates.json").write_text(json.dumps(42))
    assert un._load() == _EMPTY


# ---------------------------------------------------------------------------
# _save()
# ---------------------------------------------------------------------------


def test_save_writes_sorted_json(tmp_path, monkeypatch):
    un = _make_notifier(tmp_path, monkeypatch)
    un._save(
        {
            "notified": {"c/img", "a/img", "b/img"},
            "unknown_since": {},
            "unknown_notified": [],
        }
    )
    data = json.loads((tmp_path / "notified_updates.json").read_text())
    assert data["notified"] == ["a/img", "b/img", "c/img"]


# ---------------------------------------------------------------------------
# check_and_notify()
# ---------------------------------------------------------------------------


def test_check_and_notify_fires_for_new_update(tmp_path, monkeypatch):
    un = _make_notifier(tmp_path, monkeypatch)
    stacks = [
        {
            "update_path": "ghcr.io/myapp",
            "update_status": "update_available",
            "name": "myapp",
        }
    ]

    with patch("app.notifications.notify") as mock_notify:
        un.check_and_notify(stacks)

    mock_notify.assert_called_once()
    title = mock_notify.call_args[0][0]
    assert "myapp" in title
    # Entry persisted
    assert "ghcr.io/myapp" in un._load()["notified"]


def test_check_and_notify_does_not_double_notify(tmp_path, monkeypatch):
    un = _make_notifier(tmp_path, monkeypatch)
    # Pre-populate so the stack is already known
    un._save({"notified": ["ghcr.io/myapp"], "unknown_since": {}, "unknown_notified": []})
    stacks = [
        {
            "update_path": "ghcr.io/myapp",
            "update_status": "update_available",
            "name": "myapp",
        }
    ]

    with patch("app.notifications.notify") as mock_notify:
        un.check_and_notify(stacks)

    mock_notify.assert_not_called()


def test_check_and_notify_clears_on_up_to_date(tmp_path, monkeypatch):
    un = _make_notifier(tmp_path, monkeypatch)
    un._save({"notified": ["ghcr.io/myapp"], "unknown_since": {}, "unknown_notified": []})
    stacks = [
        {"update_path": "ghcr.io/myapp", "update_status": "up_to_date", "name": "myapp"}
    ]

    with patch("app.notifications.notify"):
        un.check_and_notify(stacks)

    assert "ghcr.io/myapp" not in un._load()["notified"]


def test_check_and_notify_clears_on_mixed(tmp_path, monkeypatch):
    un = _make_notifier(tmp_path, monkeypatch)
    un._save({"notified": ["ghcr.io/myapp"], "unknown_since": {}, "unknown_notified": []})
    stacks = [
        {"update_path": "ghcr.io/myapp", "update_status": "mixed", "name": "myapp"}
    ]

    with patch("app.notifications.notify"):
        un.check_and_notify(stacks)

    assert "ghcr.io/myapp" not in un._load()["notified"]


def test_check_and_notify_no_change_does_not_save(tmp_path, monkeypatch):
    un = _make_notifier(tmp_path, monkeypatch)
    stacks = [
        {"update_path": "ghcr.io/myapp", "update_status": "up_to_date", "name": "myapp"}
    ]

    with patch("app.notifications.notify"):
        un.check_and_notify(stacks)

    # File should not have been created
    assert not (tmp_path / "notified_updates.json").exists()


# ---------------------------------------------------------------------------
# long-unknown container notifications (OP#217)
# ---------------------------------------------------------------------------


def _unknown(path, name="paperless", reason="rate_limited"):
    return {
        "update_path": path,
        "name": name,
        "update_status": "unknown",
        "unknown_reason": reason,
    }


def _age_unknown(tmp_path, un, path, seconds):
    """Backdate a recorded first-seen so the staleness threshold is crossed."""
    store = tmp_path / "notified_updates.json"
    state = json.loads(store.read_text())
    state["unknown_since"][path] -= seconds
    store.write_text(json.dumps(state))


def test_unknown_below_threshold_is_silent(tmp_path, monkeypatch):
    un = _make_notifier(tmp_path, monkeypatch)

    with patch("app.notifications.notify") as mock_notify:
        un.check_and_notify([_unknown("docker/paperless")])
        un.check_and_notify([_unknown("docker/paperless")])

    assert mock_notify.call_count == 0


def test_unknown_crossing_threshold_notifies_once(tmp_path, monkeypatch):
    un = _make_notifier(tmp_path, monkeypatch)

    with patch("app.notifications.notify") as mock_notify:
        un.check_and_notify([_unknown("docker/paperless")])
        _age_unknown(tmp_path, un, "docker/paperless", un.UNKNOWN_STALE_SECONDS + 1)
        un.check_and_notify([_unknown("docker/paperless")])
        un.check_and_notify([_unknown("docker/paperless")])

    assert mock_notify.call_count == 1
    args, kwargs = mock_notify.call_args
    assert kwargs.get("level") == "warning"
    assert "paperless" in args[1]
    assert "rate limited" in args[1].lower()


def test_second_container_going_stale_notifies_again(tmp_path, monkeypatch):
    un = _make_notifier(tmp_path, monkeypatch)
    first = _unknown("docker/paperless")
    second = _unknown("docker/immich", name="immich")

    with patch("app.notifications.notify") as mock_notify:
        un.check_and_notify([first, second])
        _age_unknown(tmp_path, un, "docker/paperless", un.UNKNOWN_STALE_SECONDS + 1)
        un.check_and_notify([first, second])
        _age_unknown(tmp_path, un, "docker/immich", un.UNKNOWN_STALE_SECONDS + 1)
        un.check_and_notify([first, second])

    assert mock_notify.call_count == 2


def test_unknown_resolution_clears_state(tmp_path, monkeypatch):
    un = _make_notifier(tmp_path, monkeypatch)

    with patch("app.notifications.notify"):
        un.check_and_notify([_unknown("docker/paperless")])
        un.check_and_notify(
            [
                {
                    "update_path": "docker/paperless",
                    "name": "paperless",
                    "update_status": "up_to_date",
                }
            ]
        )

    state = json.loads((tmp_path / "notified_updates.json").read_text())
    assert state["unknown_since"] == {}


def test_legacy_list_store_migrates(tmp_path, monkeypatch):
    """A pre-OP#217 bare-list store keeps its notified paths."""
    un = _make_notifier(tmp_path, monkeypatch)
    (tmp_path / "notified_updates.json").write_text(json.dumps(["docker/jellyfin"]))

    with patch("app.notifications.notify") as mock_notify:
        un.check_and_notify(
            [
                {
                    "update_path": "docker/jellyfin",
                    "name": "jellyfin",
                    "update_status": "update_available",
                }
            ]
        )

    assert mock_notify.call_count == 0  # already notified before the migration
    assert "docker/jellyfin" in un._load()["notified"]
