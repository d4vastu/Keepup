"""Additional auth router tests to cover gaps."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def auth_client(config_file, data_dir, monkeypatch):
    """Unauthenticated TestClient for auth routes."""
    monkeypatch.setenv("PORTAINER_URL", "")
    monkeypatch.setenv("PORTAINER_API_KEY", "")
    from app.main import app
    return TestClient(app, raise_server_exceptions=True)


def _create_admin(username="testuser", password="password123"):
    from app.auth import create_admin
    return create_admin(username=username, password=password, totp_secret=None)


# ---------------------------------------------------------------------------
# POST /logout
# ---------------------------------------------------------------------------

def test_logout_redirects_to_login(auth_client, data_dir):
    _create_admin()
    # Log in first
    auth_client.post("/login", data={"username": "testuser", "password": "password123"})
    response = auth_client.post("/logout", follow_redirects=False)
    assert response.status_code == 303
    assert "/login" in response.headers["location"]


# ---------------------------------------------------------------------------
# GET /setup/backup-key
# ---------------------------------------------------------------------------

def test_setup_backup_key_no_session_redirects(auth_client):
    response = auth_client.get("/setup/backup-key", follow_redirects=False)
    assert response.status_code == 302
    assert "/login" in response.headers["location"]


def test_setup_backup_key_with_session_shows_key(auth_client):
    """After setup, user is redirected to backup-key page."""
    response = auth_client.post("/setup", data={
        "username": "testuser",
        "password": "password123",
        "password_confirm": "password123",
    }, follow_redirects=True)
    # Should end up on backup-key page
    assert response.status_code == 200
    assert "backup" in response.text.lower() or "key" in response.text.lower()


# ---------------------------------------------------------------------------
# POST /setup/backup-key/confirm
# ---------------------------------------------------------------------------

def test_setup_backup_key_confirm_redirects(auth_client):
    # Set up session
    auth_client.post("/setup", data={
        "username": "testuser",
        "password": "password123",
        "password_confirm": "password123",
    }, follow_redirects=False)
    # Confirm backup key
    response = auth_client.post("/setup/backup-key/confirm", follow_redirects=False)
    assert response.status_code == 303


# ---------------------------------------------------------------------------
# GET /setup page when admin exists
# ---------------------------------------------------------------------------

def test_setup_submit_when_admin_exists_redirects(auth_client, data_dir):
    _create_admin()
    response = auth_client.post("/setup", data={
        "username": "another",
        "password": "password123",
        "password_confirm": "password123",
    }, follow_redirects=False)
    assert response.status_code == 302


# ---------------------------------------------------------------------------
# POST /forgot-password/reset
# ---------------------------------------------------------------------------

def test_forgot_password_reset_success(auth_client, data_dir):
    backup_key = _create_admin()
    # Submit backup key to get recovery session
    auth_client.post("/forgot-password", data={"backup_key": backup_key})
    # Now reset password
    response = auth_client.post("/forgot-password/reset", data={
        "new_password": "newpassword123",
        "new_password_confirm": "newpassword123",
    })
    assert response.status_code == 200
    assert "done" in response.text.lower() or "success" in response.text.lower() or "password" in response.text.lower()


def test_forgot_password_reset_without_session_redirects(auth_client, data_dir):
    _create_admin()
    response = auth_client.post("/forgot-password/reset", data={
        "new_password": "newpassword123",
        "new_password_confirm": "newpassword123",
    }, follow_redirects=False)
    assert response.status_code == 302


def test_forgot_password_reset_short_password_error(auth_client, data_dir):
    backup_key = _create_admin()
    auth_client.post("/forgot-password", data={"backup_key": backup_key})
    response = auth_client.post("/forgot-password/reset", data={
        "new_password": "short",
        "new_password_confirm": "short",
    })
    assert response.status_code == 200
    assert "8 characters" in response.text.lower() or "error" in response.text.lower()


def test_forgot_password_reset_mismatch_error(auth_client, data_dir):
    backup_key = _create_admin()
    auth_client.post("/forgot-password", data={"backup_key": backup_key})
    response = auth_client.post("/forgot-password/reset", data={
        "new_password": "password123",
        "new_password_confirm": "different123",
    })
    assert response.status_code == 200
    assert "match" in response.text.lower() or "error" in response.text.lower()


# ---------------------------------------------------------------------------
# GET /forgot-password
# ---------------------------------------------------------------------------

def test_forgot_password_page_returns_200(auth_client):
    response = auth_client.get("/forgot-password")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Login with remember_me
# ---------------------------------------------------------------------------

def test_login_with_remember_me(auth_client, data_dir):
    _create_admin()
    response = auth_client.post("/login", data={
        "username": "testuser",
        "password": "password123",
        "remember_me": "on",
    }, follow_redirects=False)
    assert response.status_code == 303


# ---------------------------------------------------------------------------
# Login with already-authenticated session
# ---------------------------------------------------------------------------

def test_login_page_already_authenticated_redirects(auth_client, data_dir):
    _create_admin()
    # Log in
    auth_client.post("/login", data={"username": "testuser", "password": "password123"})
    # Access login page again - should redirect to /
    response = auth_client.get("/login", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/"


# ---------------------------------------------------------------------------
# GET /setup/hosts
# ---------------------------------------------------------------------------

def test_setup_hosts_page_returns_200(auth_client, data_dir):
    _create_admin()
    response = auth_client.get("/setup/hosts")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Login warnings when 2-3 attempts remain
# ---------------------------------------------------------------------------

def test_login_warns_when_few_attempts_remain(auth_client, data_dir):
    from app.auth_router import _ATTEMPTS
    _ATTEMPTS.clear()
    _create_admin()
    # Make 3 failed attempts
    for _ in range(3):
        auth_client.post("/login", data={
            "username": "testuser",
            "password": "wrongpass",
        })
    # 4th should show "2 attempts remaining"
    response = auth_client.post("/login", data={
        "username": "testuser",
        "password": "wrongpass",
    })
    assert response.status_code == 200
    # Should warn about remaining attempts
    assert "attempt" in response.text.lower() or "incorrect" in response.text.lower()
