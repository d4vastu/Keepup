"""Tests for notification store (app/notifications.py) and related routes."""
from unittest.mock import AsyncMock, patch


# ---------------------------------------------------------------------------
# Unit tests for notifications module
# ---------------------------------------------------------------------------

def test_notify_adds_entry(data_dir, monkeypatch):
    import app.notifications as n
    monkeypatch.setattr(n, "_NOTIF_PATH", data_dir / "notifications.json")
    monkeypatch.setattr(n, "_DATA_DIR", data_dir)

    n.notify("Test title", "Test message", level="error")

    entries = n.get_notifications()
    assert len(entries) == 1
    assert entries[0]["title"] == "Test title"
    assert entries[0]["message"] == "Test message"
    assert entries[0]["level"] == "error"
    assert entries[0]["read"] is False


def test_get_unread_count(data_dir, monkeypatch):
    import app.notifications as n
    monkeypatch.setattr(n, "_NOTIF_PATH", data_dir / "notifications.json")
    monkeypatch.setattr(n, "_DATA_DIR", data_dir)

    assert n.get_unread_count() == 0
    n.notify("A", "B")
    n.notify("C", "D")
    assert n.get_unread_count() == 2


def test_mark_all_read(data_dir, monkeypatch):
    import app.notifications as n
    monkeypatch.setattr(n, "_NOTIF_PATH", data_dir / "notifications.json")
    monkeypatch.setattr(n, "_DATA_DIR", data_dir)

    n.notify("A", "B")
    n.notify("C", "D")
    assert n.get_unread_count() == 2

    n.mark_all_read()
    assert n.get_unread_count() == 0


def test_get_notifications_limit(data_dir, monkeypatch):
    import app.notifications as n
    monkeypatch.setattr(n, "_NOTIF_PATH", data_dir / "notifications.json")
    monkeypatch.setattr(n, "_DATA_DIR", data_dir)

    for i in range(10):
        n.notify(f"Title {i}", f"Msg {i}")

    result = n.get_notifications(limit=5)
    assert len(result) == 5


def test_empty_store_returns_empty(data_dir, monkeypatch):
    import app.notifications as n
    monkeypatch.setattr(n, "_NOTIF_PATH", data_dir / "notifications.json")
    monkeypatch.setattr(n, "_DATA_DIR", data_dir)

    assert n.get_notifications() == []
    assert n.get_unread_count() == 0


# ---------------------------------------------------------------------------
# Route tests
# ---------------------------------------------------------------------------

def test_notifications_badge_zero(client):
    response = client.get("/api/notifications/badge")
    assert response.status_code == 200
    assert "notif-badge" in response.text


def test_notifications_badge_with_unread(client, monkeypatch):
    import app.main as m
    monkeypatch.setattr(m, "get_unread_count", lambda: 5)
    response = client.get("/api/notifications/badge")
    assert response.status_code == 200
    assert "5" in response.text


def test_notifications_panel_empty(client):
    response = client.get("/api/notifications/panel")
    assert response.status_code == 200
    assert "all clear" in response.text.lower() or "No notifications" in response.text


def test_notifications_panel_with_entry(client, monkeypatch):
    import app.main as m
    monkeypatch.setattr(m, "get_notifications", lambda limit=20: [{
        "id": "abc123",
        "title": "Update failed",
        "message": "Something went wrong",
        "level": "error",
        "created_at": "2026-04-04T12:00:00+00:00",
        "read": False,
    }])
    response = client.get("/api/notifications/panel")
    assert response.status_code == 200
    assert "Update failed" in response.text


def test_notifications_read_marks_read(client):
    response = client.post("/api/notifications/read")
    assert response.status_code == 200
    assert "notif-badge" in response.text


def test_notification_panel_has_history_link(client):
    response = client.get("/api/notifications/panel")
    assert response.status_code == 200
    assert "/admin/auto-updates/history" in response.text


# ---------------------------------------------------------------------------
# Auto-update history route
# ---------------------------------------------------------------------------

def test_auto_update_history_empty(client):
    response = client.get("/admin/auto-updates/history")
    assert response.status_code == 200
    assert "No auto-update runs" in response.text or "Auto-Update History" in response.text


def test_auto_update_history_with_entries(client, monkeypatch):
    import app.admin as a
    monkeypatch.setattr(a, "get_recent", lambda n: [
        {
            "id": "abc123",
            "type": "os",
            "target": "test-host",
            "target_name": "Test Host",
            "ran_at": "2026-04-04T12:00:00+00:00",
            "status": "success",
            "lines": ["All packages up to date."],
            "read": True,
        },
        {
            "id": "def456",
            "type": "docker",
            "target": "portainer/3:1",
            "target_name": "My Stack",
            "ran_at": "2026-04-04T11:00:00+00:00",
            "status": "error",
            "lines": ["Connection refused"],
            "read": False,
        },
    ])
    response = client.get("/admin/auto-updates/history")
    assert response.status_code == 200
    assert "Test Host" in response.text
    assert "My Stack" in response.text
    assert "Success" in response.text
    assert "Error" in response.text


# ---------------------------------------------------------------------------
# Pushover config routes
# ---------------------------------------------------------------------------

def test_pushover_save_returns_200(client):
    response = client.post("/admin/connections/pushover", data={
        "pushover_api_token": "test-token",
        "pushover_user_key": "test-user-key",
        "pushover_enabled": "on",
    })
    assert response.status_code == 200
    assert "pushover" in response.text.lower() or "Pushover" in response.text


def test_pushover_save_no_credentials(client):
    response = client.post("/admin/connections/pushover", data={
        "pushover_api_token": "",
        "pushover_user_key": "",
        "pushover_enabled": "",
    })
    assert response.status_code == 200


def test_pushover_test_no_credentials(client):
    """With no credentials stored, test returns failure message."""
    with patch("app.pushover.send_pushover", new=AsyncMock(return_value=False)):
        response = client.post("/admin/connections/pushover/test")
    assert response.status_code == 200
    assert "Failed" in response.text or "check" in response.text.lower()


def test_pushover_test_success(client):
    with patch("app.pushover.send_pushover", new=AsyncMock(return_value=True)):
        response = client.post("/admin/connections/pushover/test")
    assert response.status_code == 200
    assert "sent" in response.text.lower() or "success" in response.text.lower()


def test_admin_connections_includes_pushover(client):
    response = client.get("/admin/connections")
    assert response.status_code == 200
    assert "Pushover" in response.text
