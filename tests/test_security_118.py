"""Tests for OP#118 security hardening pass.

Covers:
  M2 – session secret from KEEPUP_SESSION_SECRET env var
  M3 – slowapi rate limits on trigger endpoints
  M4 – SSH key path traversal rejection
  M5 – config.example.yml has no real Portainer API key
  M6 – session_version invalidates sessions on MFA/password changes
"""

import time
from pathlib import Path
from unittest.mock import patch


# ---------------------------------------------------------------------------
# M2 – Session secret from env var
# ---------------------------------------------------------------------------


def test_session_secret_env_var_takes_precedence(monkeypatch, tmp_path):
    monkeypatch.setenv("KEEPUP_SESSION_SECRET", "env-secret-value")
    import app.auth as auth

    monkeypatch.setattr(auth, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(auth, "_SESSION_SECRET_FILE", tmp_path / ".session_secret")
    (tmp_path / ".session_secret").write_text("file-secret-value")

    assert auth.get_session_secret() == "env-secret-value"


def test_session_secret_falls_back_to_file(monkeypatch, tmp_path):
    monkeypatch.delenv("KEEPUP_SESSION_SECRET", raising=False)
    import app.auth as auth

    monkeypatch.setattr(auth, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(auth, "_SESSION_SECRET_FILE", tmp_path / ".session_secret")
    (tmp_path / ".session_secret").write_text("file-secret-value")

    assert auth.get_session_secret() == "file-secret-value"


def test_session_secret_env_var_empty_uses_file(monkeypatch, tmp_path):
    monkeypatch.setenv("KEEPUP_SESSION_SECRET", "  ")
    import app.auth as auth

    monkeypatch.setattr(auth, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(auth, "_SESSION_SECRET_FILE", tmp_path / ".session_secret")
    (tmp_path / ".session_secret").write_text("file-secret-value")

    assert auth.get_session_secret() == "file-secret-value"


# ---------------------------------------------------------------------------
# M3 – Rate limiting on trigger endpoints (7th request returns 429)
# ---------------------------------------------------------------------------


def test_host_update_rate_limit(client):
    """Requests 1-6 are not rate-limited; the 7th returns 429."""
    for i in range(6):
        r = client.post("/api/host/no-such-host/update", data={"sudo_password": "", "save_sudo": ""})
        assert r.status_code != 429, f"Request {i + 1} unexpectedly rate-limited"
    r = client.post("/api/host/no-such-host/update", data={"sudo_password": "", "save_sudo": ""})
    assert r.status_code == 429


def test_host_restart_rate_limit(client):
    """Requests 1-6 are not rate-limited; the 7th returns 429."""
    for i in range(6):
        r = client.post("/api/host/no-such-host/restart", data={"sudo_password": "", "save_sudo": ""})
        assert r.status_code != 429, f"Request {i + 1} unexpectedly rate-limited"
    r = client.post("/api/host/no-such-host/restart", data={"sudo_password": "", "save_sudo": ""})
    assert r.status_code == 429


def test_stack_update_rate_limit(client):
    """Requests 1-6 are not rate-limited; the 7th returns 429."""
    for i in range(6):
        r = client.post("/api/docker/stack/no-backend/no-proj/update")
        assert r.status_code != 429, f"Request {i + 1} unexpectedly rate-limited"
    r = client.post("/api/docker/stack/no-backend/no-proj/update")
    assert r.status_code == 429


def test_admin_post_rate_limit(client):
    """30th admin POST succeeds, 31st returns 429."""
    for i in range(30):
        r = client.post("/admin/account/timezone", data={"timezone": "UTC"})
        assert r.status_code != 429, f"Request {i + 1} unexpectedly rate-limited"
    r = client.post("/admin/account/timezone", data={"timezone": "UTC"})
    assert r.status_code == 429


# ---------------------------------------------------------------------------
# M4 – SSH key path traversal rejection
# ---------------------------------------------------------------------------


def test_resolve_ssh_key_path_valid(tmp_path, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "_KEYS_DIR", tmp_path)
    (tmp_path / "id_ed25519").touch()
    result = main._resolve_ssh_key_path("id_ed25519")
    assert result == str(tmp_path / "id_ed25519")


def test_resolve_ssh_key_path_dotdot(tmp_path, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "_KEYS_DIR", tmp_path)
    import pytest
    with pytest.raises(ValueError, match="escapes keys directory"):
        main._resolve_ssh_key_path("../../etc/passwd")


def test_resolve_ssh_key_path_absolute(tmp_path, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "_KEYS_DIR", tmp_path)
    import pytest
    with pytest.raises(ValueError, match="escapes keys directory"):
        main._resolve_ssh_key_path("/etc/passwd")


def test_resolve_ssh_key_path_traversal_via_subdirectory(tmp_path, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "_KEYS_DIR", tmp_path)
    import pytest
    with pytest.raises(ValueError, match="escapes keys directory"):
        main._resolve_ssh_key_path("keys/../etc/passwd")


def test_resolve_ssh_key_path_special_chars(tmp_path, monkeypatch):
    """CSS-safe-IDs requirement: key names with parens are still validated safely."""
    import app.main as main

    monkeypatch.setattr(main, "_KEYS_DIR", tmp_path)
    (tmp_path / "(special).key").touch()
    result = main._resolve_ssh_key_path("(special).key")
    assert result == str(tmp_path / "(special).key")


# ---------------------------------------------------------------------------
# M5 – config.example.yml has no real-looking Portainer API key
# ---------------------------------------------------------------------------


def test_config_example_no_plaintext_api_key():
    example = (Path(__file__).parent.parent / "config.example.yml").read_text()
    assert "YOUR_PORTAINER_API_KEY" not in example
    assert "api_key:" not in example


# ---------------------------------------------------------------------------
# M6 – session_version invalidates sessions on credential changes
# ---------------------------------------------------------------------------


def test_session_version_bumped_on_mfa_enroll(data_dir):
    import app.auth as auth

    auth.create_admin("u", "ValidPassword123", None)
    v0 = auth.get_session_version()
    auth.enroll_mfa(auth.new_totp_secret())
    assert auth.get_session_version() == v0 + 1


def test_session_version_bumped_on_mfa_remove(data_dir):
    import app.auth as auth

    auth.create_admin("u", "ValidPassword123", auth.new_totp_secret())
    v0 = auth.get_session_version()
    auth.remove_mfa()
    assert auth.get_session_version() == v0 + 1


def test_session_version_bumped_on_password_change(data_dir):
    import app.auth as auth

    auth.create_admin("u", "ValidPassword123", None)
    v0 = auth.get_session_version()
    auth.change_password("NewValidPassword456")
    assert auth.get_session_version() == v0 + 1


def test_mfa_enroll_invalidates_existing_session(client, data_dir):
    """Session obtained before MFA enrolment is rejected after enrolment."""
    r = client.get("/home")
    assert r.status_code == 200

    from app.auth import enroll_mfa, new_totp_secret
    enroll_mfa(new_totp_secret())

    r = client.get("/home", follow_redirects=False)
    assert r.status_code in (302, 303)
    assert "/login" in r.headers["location"]


def test_password_change_invalidates_existing_session(client, data_dir):
    """Session obtained before a password change is rejected after the change."""
    r = client.get("/home")
    assert r.status_code == 200

    from app.auth import change_password
    change_password("BrandNewPassword999")

    r = client.get("/home", follow_redirects=False)
    assert r.status_code in (302, 303)
    assert "/login" in r.headers["location"]
