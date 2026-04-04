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
# GET /setup
# ---------------------------------------------------------------------------

def test_setup_page_no_admin_returns_200(auth_client):
    response = auth_client.get("/setup", follow_redirects=False)
    assert response.status_code == 200


def test_setup_page_with_admin_redirects(auth_client, data_dir):
    _create_admin()
    response = auth_client.get("/setup", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/"


# ---------------------------------------------------------------------------
# POST /setup
# ---------------------------------------------------------------------------

def test_setup_submit_valid_redirects_to_backup_key(auth_client):
    response = auth_client.post("/setup", data={
        "username": "alice",
        "password": "password123",
        "password_confirm": "password123",
    }, follow_redirects=False)
    assert response.status_code == 303
    assert "/setup/backup-key" in response.headers["location"]


def test_setup_submit_creates_admin(auth_client, data_dir):
    from app.auth import admin_exists
    auth_client.post("/setup", data={
        "username": "alice",
        "password": "password123",
        "password_confirm": "password123",
    })
    assert admin_exists() is True


def test_setup_submit_stores_username(auth_client, data_dir):
    from app.auth import get_admin_username
    auth_client.post("/setup", data={
        "username": "alice",
        "password": "password123",
        "password_confirm": "password123",
    })
    assert get_admin_username() == "alice"


def test_setup_submit_short_username_shows_error(auth_client):
    response = auth_client.post("/setup", data={
        "username": "a",
        "password": "password123",
        "password_confirm": "password123",
    })
    assert response.status_code == 200
    assert "at least 2" in response.text.lower()


def test_setup_submit_invalid_username_chars_shows_error(auth_client):
    response = auth_client.post("/setup", data={
        "username": "alice@bad",
        "password": "password123",
        "password_confirm": "password123",
    })
    assert response.status_code == 200
    assert "letters" in response.text.lower() or "only contain" in response.text.lower()


def test_setup_submit_short_password_shows_error(auth_client):
    response = auth_client.post("/setup", data={
        "username": "alice",
        "password": "short",
        "password_confirm": "short",
    })
    assert response.status_code == 200
    assert "8 characters" in response.text.lower()


def test_setup_submit_mismatched_passwords_shows_error(auth_client):
    response = auth_client.post("/setup", data={
        "username": "alice",
        "password": "password123",
        "password_confirm": "different456",
    })
    assert response.status_code == 200
    assert "do not match" in response.text.lower()


def test_setup_submit_preserves_username_on_error(auth_client):
    response = auth_client.post("/setup", data={
        "username": "alice",
        "password": "short",
        "password_confirm": "short",
    })
    assert response.status_code == 200
    assert "alice" in response.text


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
    response = auth_client.post("/login", data={
        "username": "alice",
        "password": "password123",
    }, follow_redirects=False)
    assert response.status_code == 303


def test_login_submit_wrong_password_shows_error(auth_client, data_dir):
    _create_admin(username="alice", password="password123")
    response = auth_client.post("/login", data={
        "username": "alice",
        "password": "wrongpass",
    })
    assert response.status_code == 200
    assert "incorrect" in response.text.lower() or "wrong" in response.text.lower()


def test_login_submit_wrong_username_shows_error(auth_client, data_dir):
    _create_admin(username="alice", password="password123")
    response = auth_client.post("/login", data={
        "username": "notAlice",
        "password": "password123",
    })
    assert response.status_code == 200
    assert "incorrect" in response.text.lower()


def test_login_submit_rate_limiting_after_5_failures(auth_client, data_dir):
    _create_admin(username="alice", password="password123")
    # Make 5 failed attempts
    for _ in range(5):
        auth_client.post("/login", data={
            "username": "alice",
            "password": "wrongpass",
        })
    # 6th attempt should show lockout
    response = auth_client.post("/login", data={
        "username": "alice",
        "password": "wrongpass",
    })
    assert response.status_code == 200
    assert "locked" in response.text.lower() or "too many" in response.text.lower()


def test_login_submit_clears_rate_limit_on_success(auth_client, data_dir):
    """A successful login after partial failures should still work."""
    from app.auth_router import _ATTEMPTS
    _ATTEMPTS.clear()
    _create_admin(username="alice", password="password123")
    # Make 3 failed attempts
    for _ in range(3):
        auth_client.post("/login", data={
            "username": "alice",
            "password": "wrongpass",
        })
    # Correct credentials should still work
    response = auth_client.post("/login", data={
        "username": "alice",
        "password": "password123",
    }, follow_redirects=False)
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
    response = auth_client.post("/forgot-password", data={"backup_key": "WRONG-WRONG-WRONG-WRONG"})
    assert response.status_code == 200
    assert "not correct" in response.text.lower() or "incorrect" in response.text.lower()
