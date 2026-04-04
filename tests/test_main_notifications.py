"""Tests for main.py notification routes and auto-update log integration."""


def test_notifications_badge_zero(client):
    response = client.get("/api/notifications/badge")
    assert response.status_code == 200
    # With no entries, returns an empty badge span
    assert "notif-badge" in response.text


def test_notifications_badge_with_errors(client, monkeypatch):
    import app.main as m
    monkeypatch.setattr(m, "get_unread_error_count", lambda: 3)
    response = client.get("/api/notifications/badge")
    assert response.status_code == 200
    assert "3" in response.text


def test_notifications_badge_high_count(client, monkeypatch):
    import app.main as m
    monkeypatch.setattr(m, "get_unread_error_count", lambda: 15)
    response = client.get("/api/notifications/badge")
    assert response.status_code == 200
    assert "9+" in response.text


def test_notifications_panel_returns_200(client):
    response = client.get("/api/notifications/panel")
    assert response.status_code == 200


def test_notifications_read_marks_read(client):
    response = client.post("/api/notifications/read")
    assert response.status_code == 200
    assert "notif-badge" in response.text


def test_reload_connections_returns_200(client):
    response = client.post("/api/reload-connections")
    assert response.status_code == 200
