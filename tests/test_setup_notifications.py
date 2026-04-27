"""Tests for setup wizard Screen 7 — notifications and update schedule."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient


@pytest.fixture
def setup_client(config_file, data_dir, monkeypatch):
    monkeypatch.setenv("PORTAINER_URL", "")
    monkeypatch.setenv("PORTAINER_API_KEY", "")
    from app.main import app

    return TestClient(app, raise_server_exceptions=True)


def _create_admin():
    from app.auth import create_admin

    return create_admin(username="admin", password="password123", totp_secret=None)


def _mock_pushover(status=200, json_data=None, exc=None):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = json_data or {"status": 1}

    inner = AsyncMock()
    if exc:
        inner.post = AsyncMock(side_effect=exc)
    else:
        inner.post = AsyncMock(return_value=resp)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=inner)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return patch("app.auth_router.make_client", return_value=ctx)


# ---------------------------------------------------------------------------
# GET /setup/notifications
# ---------------------------------------------------------------------------


def test_setup_notifications_no_admin_redirects(setup_client):
    response = setup_client.get("/setup/notifications", follow_redirects=False)
    assert response.status_code == 302
    assert "/setup" in response.headers["location"]


def test_setup_notifications_page_returns_200(setup_client, data_dir):
    _create_admin()
    response = setup_client.get("/setup/notifications")
    assert response.status_code == 200
    assert "Step 8" in response.text
    assert "Pushover" in response.text
    assert "schedule" in response.text.lower()


# ---------------------------------------------------------------------------
# POST /setup/notifications/pushover/test
# ---------------------------------------------------------------------------


def test_pushover_test_missing_fields(setup_client, data_dir):
    _create_admin()
    response = setup_client.post(
        "/setup/notifications/pushover/test",
        data={
            "pushover_token": "",
            "pushover_user_key": "",
        },
    )
    assert response.status_code == 200
    assert "amber" in response.text or "Enter" in response.text


def test_pushover_test_success(setup_client, data_dir):
    _create_admin()
    with _mock_pushover(status=200):
        response = setup_client.post(
            "/setup/notifications/pushover/test",
            data={
                "pushover_token": "abc123",
                "pushover_user_key": "xyz456",
            },
        )
    assert response.status_code == 200
    assert "sent" in response.text.lower() or "&#10003;" in response.text


def test_pushover_test_api_error(setup_client, data_dir):
    _create_admin()
    with _mock_pushover(
        status=400, json_data={"status": 0, "errors": ["invalid token"]}
    ):
        response = setup_client.post(
            "/setup/notifications/pushover/test",
            data={
                "pushover_token": "bad",
                "pushover_user_key": "bad",
            },
        )
    assert response.status_code == 200
    assert "invalid token" in response.text.lower() or "&#10007;" in response.text


def test_pushover_test_connection_error(setup_client, data_dir):
    _create_admin()
    with _mock_pushover(exc=Exception("Connection refused")):
        response = setup_client.post(
            "/setup/notifications/pushover/test",
            data={
                "pushover_token": "abc123",
                "pushover_user_key": "xyz456",
            },
        )
    assert response.status_code == 200
    assert "&#10007;" in response.text


# ---------------------------------------------------------------------------
# POST /setup/notifications/pushover/save
# ---------------------------------------------------------------------------


def test_pushover_save_stores_credentials(setup_client, data_dir):
    from app.credentials import get_integration_credentials

    _create_admin()
    response = setup_client.post(
        "/setup/notifications/pushover/save",
        data={
            "pushover_token": "mytoken",
            "pushover_user_key": "myuserkey",
            "pushover_enabled": "on",
        },
    )
    assert response.status_code == 200
    assert "saved" in response.text.lower() or "&#10003;" in response.text
    creds = get_integration_credentials("pushover")
    assert creds.get("api_token") == "mytoken"
    assert creds.get("user_key") == "myuserkey"


def test_pushover_save_no_token_skips_credentials(setup_client, data_dir):
    from app.credentials import get_integration_credentials

    _create_admin()
    response = setup_client.post(
        "/setup/notifications/pushover/save",
        data={
            "pushover_token": "",
            "pushover_user_key": "",
            "pushover_enabled": "",
        },
    )
    assert response.status_code == 200
    # No token supplied — credentials should not be overwritten
    creds = get_integration_credentials("pushover")
    assert not creds.get("api_token")


# ---------------------------------------------------------------------------
# POST /setup/notifications/schedule/save
# ---------------------------------------------------------------------------


def test_schedule_save_6h(setup_client, data_dir, config_file):
    import yaml

    _create_admin()
    response = setup_client.post(
        "/setup/notifications/schedule/save", data={"update_schedule": "6h"}
    )
    assert response.status_code == 200
    assert "6 hour" in response.text.lower() or "&#10003;" in response.text
    raw = yaml.safe_load(config_file.read_text())
    assert raw.get("update_check_schedule") == "0 */6 * * *"


def test_schedule_save_24h(setup_client, data_dir, config_file):
    import yaml

    _create_admin()
    response = setup_client.post(
        "/setup/notifications/schedule/save", data={"update_schedule": "24h"}
    )
    assert response.status_code == 200
    raw = yaml.safe_load(config_file.read_text())
    assert raw.get("update_check_schedule") == "0 2 * * *"


def test_schedule_save_manual_removes_key(setup_client, data_dir, config_file):
    import yaml

    _create_admin()
    # First set a schedule
    setup_client.post(
        "/setup/notifications/schedule/save", data={"update_schedule": "12h"}
    )
    # Then set to manual — key should be removed
    response = setup_client.post(
        "/setup/notifications/schedule/save", data={"update_schedule": "manual"}
    )
    assert response.status_code == 200
    raw = yaml.safe_load(config_file.read_text())
    assert "update_check_schedule" not in raw


# ---------------------------------------------------------------------------
# POST /setup/notifications/email/save
# ---------------------------------------------------------------------------


def test_email_save_stores_config(setup_client, data_dir, config_file):
    import yaml

    _create_admin()
    response = setup_client.post(
        "/setup/notifications/email/save",
        data={
            "sender_name": "Keepup",
            "sender_address": "keepup@example.com",
            "recipient_address": "me@example.com",
            "smtp_host": "smtp.example.com",
            "smtp_port": "587",
            "smtp_password": "secret",
            "tls": "on",
        },
    )
    assert response.status_code == 200
    assert "saved" in response.text.lower() or "&#10003;" in response.text
    raw = yaml.safe_load(config_file.read_text())
    email = raw.get("email", {})
    assert email.get("smtp_host") == "smtp.example.com"
    assert email.get("sender_address") == "keepup@example.com"
    assert email.get("tls") is True


def test_email_save_no_host_returns_warning(setup_client, data_dir):
    _create_admin()
    response = setup_client.post(
        "/setup/notifications/email/save",
        data={
            "sender_name": "",
            "sender_address": "a@b.com",
            "recipient_address": "c@d.com",
            "smtp_host": "",
            "smtp_port": "587",
        },
    )
    assert response.status_code == 200
    assert "amber" in response.text or "required" in response.text.lower()


# ---------------------------------------------------------------------------
# POST /setup/notifications/email/test
# ---------------------------------------------------------------------------


def test_email_test_missing_fields(setup_client, data_dir):
    _create_admin()
    response = setup_client.post(
        "/setup/notifications/email/test",
        data={
            "sender_address": "",
            "recipient_address": "",
            "smtp_host": "",
            "smtp_port": "587",
        },
    )
    assert response.status_code == 200
    assert "amber" in response.text or "Fill in" in response.text


def test_email_test_smtp_failure(setup_client, data_dir):
    _create_admin()
    with patch("smtplib.SMTP", side_effect=Exception("Connection refused")):
        with patch("smtplib.SMTP_SSL", side_effect=Exception("Connection refused")):
            response = setup_client.post(
                "/setup/notifications/email/test",
                data={
                    "sender_address": "a@b.com",
                    "recipient_address": "c@d.com",
                    "smtp_host": "smtp.example.com",
                    "smtp_port": "587",
                    "tls": "",
                },
            )
    assert response.status_code == 200
    assert "&#10007;" in response.text


# ---------------------------------------------------------------------------
# POST /setup/notifications/email/delete
# ---------------------------------------------------------------------------


def test_email_delete_removes_config(setup_client, data_dir, config_file):
    import yaml

    _create_admin()
    # First save some config
    setup_client.post(
        "/setup/notifications/email/save",
        data={
            "sender_address": "a@b.com",
            "recipient_address": "c@d.com",
            "smtp_host": "smtp.example.com",
            "smtp_port": "587",
        },
    )
    # Now delete it
    response = setup_client.post("/setup/notifications/email/delete")
    assert response.status_code == 200
    raw = yaml.safe_load(config_file.read_text()) or {}
    assert "email" not in raw


# ---------------------------------------------------------------------------
# GET /setup/notifications — email context variables
# ---------------------------------------------------------------------------


def test_setup_notifications_shows_email_tile(setup_client, data_dir):
    _create_admin()
    response = setup_client.get("/setup/notifications")
    assert response.status_code == 200
    assert "Email" in response.text
    assert "Pushover" in response.text


def test_setup_notifications_shows_connected_when_email_configured(
    setup_client, data_dir, config_file
):
    _create_admin()
    # Pre-configure email
    setup_client.post(
        "/setup/notifications/email/save",
        data={
            "sender_address": "a@b.com",
            "recipient_address": "c@d.com",
            "smtp_host": "smtp.example.com",
            "smtp_port": "587",
        },
    )
    response = setup_client.get("/setup/notifications")
    assert response.status_code == 200
    assert "connected" in response.text.lower()
