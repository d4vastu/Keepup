"""Integration tests for auth routes — setup, login, forgot-password."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def auth_client(config_file, data_dir, monkeypatch):
    """Unauthenticated TestClient for testing auth routes.
    data_dir fixture already patches app.auth._DATA_DIR and app.credentials paths.
    """
    monkeypatch.setenv("PORTAINER_URL", "")
    monkeypatch.setenv("PORTAINER_API_KEY", "")
    from app.main import app

    return TestClient(app, raise_server_exceptions=True)


def _create_admin(username="alice", password="password123"):
    from app.auth import create_admin

    return create_admin(username=username, password=password, totp_secret=None)


# ---------------------------------------------------------------------------
# GET /setup  (screen 1 — welcome + timezone)
# ---------------------------------------------------------------------------


def test_setup_page_no_admin_returns_200(auth_client):
    response = auth_client.get("/setup", follow_redirects=False)
    assert response.status_code == 200


def test_setup_page_with_admin_redirects_to_login(auth_client, data_dir):
    _create_admin()
    response = auth_client.get("/setup", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/login"


def test_setup_welcome_shows_timezone_select(auth_client):
    response = auth_client.get("/setup")
    assert response.status_code == 200
    assert 'name="timezone"' in response.text


# ---------------------------------------------------------------------------
# POST /setup  (saves timezone, redirects to account)
# ---------------------------------------------------------------------------


def test_setup_welcome_submit_redirects_to_account(auth_client):
    response = auth_client.post(
        "/setup", data={"timezone": "America/New_York"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/setup/account"


def test_setup_welcome_submit_with_admin_redirects_to_login(auth_client, data_dir):
    _create_admin()
    response = auth_client.post(
        "/setup", data={"timezone": "UTC"}, follow_redirects=False
    )
    assert response.status_code == 302
    assert response.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# GET /setup/account  (screen 2 — credentials)
# ---------------------------------------------------------------------------


def test_setup_account_page_no_admin_returns_200(auth_client):
    response = auth_client.get("/setup/account", follow_redirects=False)
    assert response.status_code == 200


def test_setup_account_page_with_admin_redirects_to_login(auth_client, data_dir):
    _create_admin()
    response = auth_client.get("/setup/account", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# POST /setup/account  (creates admin, redirects to security)
# ---------------------------------------------------------------------------


def test_setup_account_valid_redirects_to_security(auth_client):
    response = auth_client.post(
        "/setup/account",
        data={
            "username": "alice",
            "password": "password123",
            "password_confirm": "password123",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "/setup/security" in response.headers["location"]


def test_setup_account_creates_admin(auth_client, data_dir):
    from app.auth import admin_exists

    auth_client.post(
        "/setup/account",
        data={
            "username": "alice",
            "password": "password123",
            "password_confirm": "password123",
        },
    )
    assert admin_exists() is True


def test_setup_account_stores_username(auth_client, data_dir):
    from app.auth import get_admin_username

    auth_client.post(
        "/setup/account",
        data={
            "username": "alice",
            "password": "password123",
            "password_confirm": "password123",
        },
    )
    assert get_admin_username() == "alice"


def test_setup_account_short_username_shows_error(auth_client):
    response = auth_client.post(
        "/setup/account",
        data={
            "username": "a",
            "password": "password123",
            "password_confirm": "password123",
        },
    )
    assert response.status_code == 200
    assert "at least 2" in response.text.lower()


def test_setup_account_invalid_username_chars_shows_error(auth_client):
    response = auth_client.post(
        "/setup/account",
        data={
            "username": "alice@bad",
            "password": "password123",
            "password_confirm": "password123",
        },
    )
    assert response.status_code == 200
    assert "letters" in response.text.lower() or "only contain" in response.text.lower()


def test_setup_account_short_password_shows_error(auth_client):
    response = auth_client.post(
        "/setup/account",
        data={
            "username": "alice",
            "password": "short",
            "password_confirm": "short",
        },
    )
    assert response.status_code == 200
    assert "8 characters" in response.text.lower()


def test_setup_account_mismatched_passwords_shows_error(auth_client):
    response = auth_client.post(
        "/setup/account",
        data={
            "username": "alice",
            "password": "password123",
            "password_confirm": "different456",
        },
    )
    assert response.status_code == 200
    assert "do not match" in response.text.lower()


def test_setup_account_preserves_username_on_error(auth_client):
    response = auth_client.post(
        "/setup/account",
        data={
            "username": "alice",
            "password": "short",
            "password_confirm": "short",
        },
    )
    assert response.status_code == 200
    assert "alice" in response.text


def test_setup_account_with_admin_redirects_to_login(auth_client, data_dir):
    _create_admin()
    response = auth_client.post(
        "/setup/account",
        data={
            "username": "bob",
            "password": "password123",
            "password_confirm": "password123",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# GET /setup/security  (screen 3 — 2FA)
# ---------------------------------------------------------------------------


def test_setup_security_without_session_flag_redirects(auth_client, data_dir):
    _create_admin()
    response = auth_client.get("/setup/security", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/login"


def test_setup_security_with_session_flag_returns_200(auth_client):
    auth_client.post(
        "/setup/account",
        data={
            "username": "alice",
            "password": "password123",
            "password_confirm": "password123",
        },
        follow_redirects=False,
    )
    response = auth_client.get("/setup/security", follow_redirects=False)
    assert response.status_code == 200


def test_setup_security_skip_mfa_redirects_to_recovery(auth_client):
    auth_client.post(
        "/setup/account",
        data={
            "username": "alice",
            "password": "password123",
            "password_confirm": "password123",
        },
        follow_redirects=False,
    )
    response = auth_client.post("/setup/security", data={}, follow_redirects=False)
    assert response.status_code == 303
    assert "/setup/recovery-code" in response.headers["location"]


def test_setup_security_wrong_totp_code_shows_error(auth_client):
    auth_client.post(
        "/setup/account",
        data={
            "username": "alice",
            "password": "password123",
            "password_confirm": "password123",
        },
        follow_redirects=False,
    )
    response = auth_client.post(
        "/setup/security",
        data={
            "enable_mfa": "on",
            "totp_code": "000000",
        },
    )
    assert response.status_code == 200
    assert "incorrect" in response.text.lower()


# ---------------------------------------------------------------------------
# GET /setup/recovery-code  (screen 4)
# ---------------------------------------------------------------------------


def test_setup_recovery_code_without_session_key_redirects(auth_client, data_dir):
    _create_admin()
    response = auth_client.get("/setup/recovery-code", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/login"


def test_setup_recovery_code_with_session_key_returns_200(auth_client):
    auth_client.post(
        "/setup/account",
        data={
            "username": "alice",
            "password": "password123",
            "password_confirm": "password123",
        },
        follow_redirects=False,
    )
    response = auth_client.get("/setup/recovery-code", follow_redirects=False)
    assert response.status_code == 200
    assert "recovery" in response.text.lower() or "backup" in response.text.lower()


def test_setup_recovery_code_confirm_redirects_to_connect(auth_client):
    auth_client.post(
        "/setup/account",
        data={
            "username": "alice",
            "password": "password123",
            "password_confirm": "password123",
        },
        follow_redirects=False,
    )
    response = auth_client.post("/setup/recovery-code/confirm", follow_redirects=False)
    assert response.status_code == 303
    assert "/setup/connect" in response.headers["location"]


# ---------------------------------------------------------------------------
# GET /setup/connect  (screen 5 — integrations)
# ---------------------------------------------------------------------------


def test_setup_connect_no_admin_redirects(auth_client):
    response = auth_client.get("/setup/connect", follow_redirects=False)
    assert response.status_code == 302


def test_setup_connect_with_admin_returns_200(auth_client, data_dir):
    _create_admin()
    response = auth_client.get("/setup/connect", follow_redirects=False)
    assert response.status_code == 200


def test_setup_connect_shows_all_integrations(auth_client, data_dir):
    _create_admin()
    response = auth_client.get("/setup/connect")
    assert response.status_code == 200
    assert "Proxmox" in response.text
    assert "OPNsense" in response.text
    assert "Portainer" in response.text
    assert "Home Assistant" in response.text


# ---------------------------------------------------------------------------
# POST /setup/connect/proxmox/test
# ---------------------------------------------------------------------------


def test_setup_proxmox_test_missing_fields(auth_client, data_dir):
    _create_admin()
    response = auth_client.post(
        "/setup/connect/proxmox/test",
        data={
            "proxmox_url": "",
            "proxmox_token_id": "",
            "proxmox_secret": "",
        },
    )
    assert response.status_code == 200
    assert "enter" in response.text.lower()


def test_setup_proxmox_test_connection_failure(auth_client, data_dir):
    _create_admin()
    response = auth_client.post(
        "/setup/connect/proxmox/test",
        data={
            "proxmox_url": "https://192.0.2.1:8006",
            "proxmox_api_user": "user@pam",
            "proxmox_token_id": "user@pam!token",
            "proxmox_secret": "uuid",
        },
    )
    assert response.status_code == 200
    assert (
        "✗" in response.text
        or "&#10007;" in response.text
        or "error" in response.text.lower()
        or "can" in response.text.lower()
    )


# ---------------------------------------------------------------------------
# POST /setup/connect/opnsense/test
# ---------------------------------------------------------------------------


def test_setup_opnsense_test_missing_fields(auth_client, data_dir):
    _create_admin()
    response = auth_client.post(
        "/setup/connect/opnsense/test",
        data={
            "opnsense_url": "",
            "opnsense_api_key": "",
            "opnsense_api_secret": "",
        },
    )
    assert response.status_code == 200
    assert "enter" in response.text.lower()


# ---------------------------------------------------------------------------
# POST /setup/connect/homeassistant/test
# ---------------------------------------------------------------------------


def test_setup_homeassistant_test_missing_fields(auth_client, data_dir):
    _create_admin()
    response = auth_client.post(
        "/setup/connect/homeassistant/test",
        data={
            "ha_url": "",
            "ha_token": "",
        },
    )
    assert response.status_code == 200
    assert "enter" in response.text.lower()


# ---------------------------------------------------------------------------
# POST /setup/connect/{integration}/save
# ---------------------------------------------------------------------------


def test_setup_save_proxmox(auth_client, data_dir):
    _create_admin()
    response = auth_client.post(
        "/setup/connect/proxmox/save",
        data={
            "proxmox_url": "https://192.168.1.10:8006",
            "proxmox_api_user": "user@pam",
            "proxmox_token_id": "user@pam!token",
            "proxmox_secret": "abc123",
            "proxmox_verify_ssl": "",
        },
    )
    assert response.status_code == 200
    assert "saved" in response.text.lower() or "connected" in response.text.lower()


def test_setup_save_opnsense(auth_client, data_dir):
    _create_admin()
    response = auth_client.post(
        "/setup/connect/opnsense/save",
        data={
            "opnsense_url": "https://192.168.1.1",
            "opnsense_api_key": "mykey",
            "opnsense_api_secret": "mysecret",
            "opnsense_verify_ssl": "",
        },
    )
    assert response.status_code == 200
    assert "saved" in response.text.lower()


def test_setup_save_homeassistant(auth_client, data_dir):
    _create_admin()
    response = auth_client.post(
        "/setup/connect/homeassistant/save",
        data={
            "ha_url": "http://homeassistant.local:8123",
            "ha_token": "eyJtoken",
        },
    )
    assert response.status_code == 200
    assert "saved" in response.text.lower()


def test_setup_save_pbs(auth_client, data_dir):
    _create_admin()
    response = auth_client.post(
        "/setup/connect/pbs/save",
        data={
            "pbs_url": "https://192.168.1.11:8007",
            "pbs_api_user": "user@pbs",
            "pbs_token_id": "user@pbs!token",
            "pbs_secret": "abc",
            "pbs_verify_ssl": "",
        },
    )
    assert response.status_code == 200
    assert "saved" in response.text.lower()


def test_setup_save_pfsense(auth_client, data_dir):
    _create_admin()
    response = auth_client.post(
        "/setup/connect/pfsense/save",
        data={
            "pfsense_url": "https://192.168.1.1",
            "pfsense_api_key": "mykey",
            "pfsense_verify_ssl": "",
        },
    )
    assert response.status_code == 200
    assert "saved" in response.text.lower()


# ---------------------------------------------------------------------------
# GET /login
# ---------------------------------------------------------------------------


def test_login_page_no_admin_redirects_to_setup(auth_client):
    response = auth_client.get("/login", follow_redirects=False)
    assert response.status_code == 302
    assert "/setup" in response.headers["location"]


def test_login_page_with_admin_returns_200(auth_client, data_dir):
    _create_admin()
    response = auth_client.get("/login", follow_redirects=False)
    assert response.status_code == 200


def test_login_page_with_username_shows_username_field(auth_client, data_dir):
    _create_admin(username="alice")
    response = auth_client.get("/login", follow_redirects=False)
    assert response.status_code == 200
    assert 'name="username"' in response.text


def test_login_page_without_username_no_username_field(auth_client, data_dir):
    """Legacy accounts without username don't show the username field."""
    from app.credentials import save_integration_credentials
    from app.auth import _hash_password

    save_integration_credentials("admin", password_hash=_hash_password("password123"))

    response = auth_client.get("/login", follow_redirects=False)
    assert response.status_code == 200
    assert 'name="username"' not in response.text


# ---------------------------------------------------------------------------
# POST /login
# ---------------------------------------------------------------------------


def test_login_submit_correct_creds_redirects(auth_client, data_dir):
    _create_admin(username="alice", password="password123")
    response = auth_client.post(
        "/login",
        data={
            "username": "alice",
            "password": "password123",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_login_submit_wrong_password_shows_error(auth_client, data_dir):
    _create_admin(username="alice", password="password123")
    response = auth_client.post(
        "/login",
        data={
            "username": "alice",
            "password": "wrongpass",
        },
    )
    assert response.status_code == 200
    assert "incorrect" in response.text.lower() or "wrong" in response.text.lower()


def test_login_submit_wrong_username_shows_error(auth_client, data_dir):
    _create_admin(username="alice", password="password123")
    response = auth_client.post(
        "/login",
        data={
            "username": "notAlice",
            "password": "password123",
        },
    )
    assert response.status_code == 200
    assert "incorrect" in response.text.lower()


def test_login_submit_rate_limiting_after_5_failures(auth_client, data_dir):
    _create_admin(username="alice", password="password123")
    # Make 5 failed attempts
    for _ in range(5):
        auth_client.post(
            "/login",
            data={
                "username": "alice",
                "password": "wrongpass",
            },
        )
    # 6th attempt should show lockout
    response = auth_client.post(
        "/login",
        data={
            "username": "alice",
            "password": "wrongpass",
        },
    )
    assert response.status_code == 200
    assert "locked" in response.text.lower() or "too many" in response.text.lower()


def test_login_submit_clears_rate_limit_on_success(auth_client, data_dir):
    """A successful login after partial failures should still work."""
    from app.auth_router import _ATTEMPTS

    _ATTEMPTS.clear()
    _create_admin(username="alice", password="password123")
    # Make 3 failed attempts
    for _ in range(3):
        auth_client.post(
            "/login",
            data={
                "username": "alice",
                "password": "wrongpass",
            },
        )
    # Correct credentials should still work
    response = auth_client.post(
        "/login",
        data={
            "username": "alice",
            "password": "password123",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


# ---------------------------------------------------------------------------
# POST /forgot-password
# ---------------------------------------------------------------------------


def test_forgot_password_correct_backup_key_shows_reset(auth_client, data_dir):
    backup_key = _create_admin()
    response = auth_client.post("/forgot-password", data={"backup_key": backup_key})
    assert response.status_code == 200
    assert "reset" in response.text.lower() or "new password" in response.text.lower()


def test_forgot_password_wrong_backup_key_shows_error(auth_client, data_dir):
    _create_admin()
    response = auth_client.post(
        "/forgot-password", data={"backup_key": "WRONG-WRONG-WRONG-WRONG"}
    )
    assert response.status_code == 200
    assert (
        "not correct" in response.text.lower() or "incorrect" in response.text.lower()
    )
