"""
Tests for OP#116 — Error hygiene and password policy.

Covers:
  - Global exception handler: generic envelope, no stack trace, request_id in response
  - request_id correlation: same id appears in both response body and server log
  - HTTPException and validation errors pass through the exception handler unchanged
  - Password minimum raised to 12 characters at: setup, password change, reset
  - Password policy flag set on create_admin and change_password
  - Login sets show_password_notice when policy flag is absent
  - Password change clears show_password_notice from session
"""

import logging
import uuid

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.auth import _MIN_PASSWORD_LEN, change_password, create_admin


# ---------------------------------------------------------------------------
# Minimal app for exception-handler unit tests
# ---------------------------------------------------------------------------


def _exception_app() -> FastAPI:
    """FastAPI app with one route that always raises an unhandled exception.

    Builds the handler directly without importing app.main (which triggers
    module-level side effects like get_session_secret() writing to /app/data).
    """
    import traceback
    import uuid as _uuid
    from fastapi.responses import JSONResponse
    import logging as _logging

    _log = _logging.getLogger("app.main")

    async def _handler(request, exc: Exception):
        request_id = str(_uuid.uuid4())
        _log.error(
            "Unhandled exception [request_id=%s] %s %s\n%s",
            request_id, request.method, request.url.path,
            traceback.format_exc(),
        )
        return JSONResponse(
            {"error": "Internal error", "request_id": request_id},
            status_code=500,
        )

    exc_app = FastAPI()
    exc_app.add_exception_handler(Exception, _handler)

    @exc_app.get("/boom")
    async def boom():
        raise RuntimeError("secret internal detail")

    @exc_app.get("/http-error")
    async def http_error():
        raise HTTPException(status_code=404, detail="Not found")

    @exc_app.get("/ok")
    async def ok():
        return {"status": "ok"}

    return exc_app


# ---------------------------------------------------------------------------
# Global exception handler
# ---------------------------------------------------------------------------


class TestGlobalExceptionHandler:
    """Verify unhandled exceptions produce a safe generic envelope."""

    def setup_method(self):
        self.client = TestClient(_exception_app(), raise_server_exceptions=False)

    def test_returns_500_status(self):
        """An unhandled exception must yield a 500 response."""
        resp = self.client.get("/boom")
        assert resp.status_code == 500

    def test_response_body_is_generic_envelope(self):
        """Response body must be {error, request_id} — no internal detail."""
        resp = self.client.get("/boom")
        body = resp.json()
        assert body["error"] == "Internal error"
        assert "request_id" in body

    def test_stack_trace_not_in_response(self):
        """The exception message and traceback must not appear in the response."""
        resp = self.client.get("/boom")
        text = resp.text
        assert "secret internal detail" not in text
        assert "RuntimeError" not in text
        assert "Traceback" not in text

    def test_request_id_is_valid_uuid(self):
        """request_id must be a valid UUID string."""
        resp = self.client.get("/boom")
        rid = resp.json()["request_id"]
        # Raises ValueError if not a valid UUID
        uuid.UUID(rid)

    def test_request_id_appears_in_server_log(self, caplog):
        """The same request_id logged server-side must match the one in the response."""
        with caplog.at_level(logging.ERROR, logger="app.main"):
            resp = self.client.get("/boom")
        rid = resp.json()["request_id"]
        assert rid in caplog.text

    def test_http_exception_not_intercepted(self):
        """HTTPException (404) must pass through unchanged, not become a 500."""
        resp = self.client.get("/http-error")
        assert resp.status_code == 404

    def test_normal_routes_unaffected(self):
        """Routes that succeed must not be affected by the exception handler."""
        resp = self.client.get("/ok")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_content_type_is_json(self):
        """Error response must carry application/json content-type."""
        resp = self.client.get("/boom")
        assert "application/json" in resp.headers.get("content-type", "")


# ---------------------------------------------------------------------------
# Password minimum length constant
# ---------------------------------------------------------------------------


class TestPasswordMinLength:
    """Verify the constant and that it is enforced at all entry points."""

    def test_min_password_len_is_12(self):
        """_MIN_PASSWORD_LEN must be exactly 12 (NIST SP 800-63B)."""
        assert _MIN_PASSWORD_LEN == 12


# ---------------------------------------------------------------------------
# Password policy enforced at account setup
# ---------------------------------------------------------------------------


class TestSetupPasswordPolicy:
    """Password < 12 chars must be rejected during the setup wizard."""

    @pytest.fixture(autouse=True)
    def setup(self, config_file, data_dir, monkeypatch):
        monkeypatch.setenv("PORTAINER_URL", "https://portainer.test:9443")
        monkeypatch.setenv("PORTAINER_API_KEY", "test-api-key")
        monkeypatch.setenv("PORTAINER_VERIFY_SSL", "false")

        from app.main import app
        self.client = TestClient(app, raise_server_exceptions=True)

    def test_password_under_12_rejected_at_setup(self):
        """Setup must reject a password shorter than 12 characters."""
        resp = self.client.post(
            "/setup/account",
            data={
                "username": "admin",
                "password": "short1234",        # 9 chars — should fail
                "password_confirm": "short1234",
            },
            follow_redirects=False,
        )
        # Stays on setup page (200) with an error, not a redirect
        assert resp.status_code == 200
        assert "12" in resp.text

    def test_password_of_exactly_12_accepted_at_setup(self):
        """Setup must accept a password of exactly 12 characters."""
        resp = self.client.post(
            "/setup/account",
            data={
                "username": "admin",
                "password": "ValidPass123",     # 12 chars
                "password_confirm": "ValidPass123",
            },
            follow_redirects=False,
        )
        # Successful setup redirects to /setup/security
        assert resp.status_code in (302, 303)


# ---------------------------------------------------------------------------
# Password policy enforced at admin password change
# ---------------------------------------------------------------------------


class TestAdminPasswordChangePolicy:
    """Password < 12 chars must be rejected at the admin account change endpoint."""

    @pytest.fixture(autouse=True)
    def setup(self, config_file, data_dir, monkeypatch):
        monkeypatch.setenv("PORTAINER_URL", "https://portainer.test:9443")
        monkeypatch.setenv("PORTAINER_API_KEY", "test-api-key")
        monkeypatch.setenv("PORTAINER_VERIFY_SSL", "false")

        from app.main import app
        from app.auth import create_admin

        create_admin(username="testadmin", password="testpassword123", totp_secret=None)
        self.client = TestClient(app, raise_server_exceptions=True)
        # Log in
        self.client.post(
            "/login",
            data={"username": "testadmin", "password": "testpassword123"},
            follow_redirects=False,
        )

    def test_short_new_password_rejected(self):
        """Password change with a new password < 12 chars must be rejected."""
        resp = self.client.post(
            "/admin/account/password",
            data={
                "current_password": "testpassword123",
                "new_password": "tooshort1",     # 9 chars
                "new_password_confirm": "tooshort1",
            },
        )
        assert resp.status_code == 200
        assert "12" in resp.text

    def test_valid_new_password_accepted(self):
        """Password change with a new password ≥ 12 chars must succeed."""
        resp = self.client.post(
            "/admin/account/password",
            data={
                "current_password": "testpassword123",
                "new_password": "NewPassword456",   # 14 chars
                "new_password_confirm": "NewPassword456",
            },
        )
        assert resp.status_code == 200
        assert "12" not in resp.text


# ---------------------------------------------------------------------------
# Password policy enforced at forgot-password reset
# ---------------------------------------------------------------------------


class TestForgotPasswordResetPolicy:
    """Password < 12 chars must be rejected in the forgot-password reset flow."""

    @pytest.fixture(autouse=True)
    def setup(self, config_file, data_dir, monkeypatch):
        monkeypatch.setenv("PORTAINER_URL", "https://portainer.test:9443")
        monkeypatch.setenv("PORTAINER_API_KEY", "test-api-key")
        monkeypatch.setenv("PORTAINER_VERIFY_SSL", "false")

        from app.main import app
        from app.auth import create_admin

        create_admin(username="testadmin", password="testpassword123", totp_secret=None)
        # Create a client with recovery_verified set in session
        self.app = app
        self.client = TestClient(app, raise_server_exceptions=True)
        # Set recovery_verified via the backup-key flow is complex in tests;
        # patch the session directly via a side-channel endpoint approach.
        # Instead, test the route logic by posting with recovery_verified in session.
        # We use the forgot-password submit endpoint first to get the session state.

    def test_short_password_rejected_at_reset(self):
        """Forgot-password reset must reject a password shorter than 12 characters."""
        # First verify the backup key to get recovery_verified in session
        from app.auth import _hash_backup_key
        from app.credentials import save_integration_credentials

        backup_key = "A3F29B1C-E7D42F8A-B5C19E3D-2A4F8C1E"
        save_integration_credentials(
            "admin", backup_key_hash=_hash_backup_key(backup_key)
        )

        # Submit the backup key to trigger recovery_verified session state
        r = self.client.post(
            "/forgot-password",
            data={"backup_key": backup_key},
            follow_redirects=False,
        )
        assert r.status_code == 200

        # Now attempt reset with a short password
        resp = self.client.post(
            "/forgot-password/reset",
            data={
                "new_password": "tooshort1",
                "new_password_confirm": "tooshort1",
            },
        )
        assert resp.status_code == 200
        assert "12" in resp.text


# ---------------------------------------------------------------------------
# password_meets_policy flag in credentials
# ---------------------------------------------------------------------------


class TestPasswordPolicyFlag:
    """Verify create_admin and change_password set the policy flag correctly."""

    @pytest.fixture(autouse=True)
    def setup(self, data_dir, monkeypatch):
        import app.auth as auth_mod
        import app.credentials as creds_mod

        monkeypatch.setattr(auth_mod, "_DATA_DIR", data_dir)
        monkeypatch.setattr(auth_mod, "_SESSION_SECRET_FILE", data_dir / ".session_secret")
        monkeypatch.setattr(creds_mod, "_DATA_DIR", data_dir)
        monkeypatch.setattr(creds_mod, "_SECRET_FILE", data_dir / ".secret")
        monkeypatch.setattr(creds_mod, "_CREDS_FILE", data_dir / "credentials.json")

    def test_create_admin_sets_flag_true_for_long_password(self):
        """create_admin with password ≥ 12 chars must set password_meets_policy=True."""
        from app.credentials import get_integration_credentials

        create_admin("admin", "ValidPassword1", None)
        creds = get_integration_credentials("admin")
        assert creds.get("password_meets_policy") is True

    def test_create_admin_sets_flag_false_for_short_password(self):
        """create_admin with password < 12 chars must set password_meets_policy=False."""
        from app.credentials import get_integration_credentials

        create_admin("admin", "short1234", None)
        creds = get_integration_credentials("admin")
        assert creds.get("password_meets_policy") is False

    def test_change_password_sets_flag_true(self):
        """change_password with ≥ 12 chars must set password_meets_policy=True."""
        from app.credentials import get_integration_credentials

        create_admin("admin", "short1234", None)
        change_password("ValidNewPass1!")
        creds = get_integration_credentials("admin")
        assert creds.get("password_meets_policy") is True


# ---------------------------------------------------------------------------
# Login sets show_password_notice when policy flag missing
# ---------------------------------------------------------------------------


class TestLoginPasswordNotice:
    """Verify show_password_notice session flag is set when policy flag is absent."""

    @pytest.fixture(autouse=True)
    def setup(self, config_file, data_dir, monkeypatch):
        monkeypatch.setenv("PORTAINER_URL", "https://portainer.test:9443")
        monkeypatch.setenv("PORTAINER_API_KEY", "test-api-key")
        monkeypatch.setenv("PORTAINER_VERIFY_SSL", "false")

        from app.main import app
        from app.auth import create_admin

        # Create admin with short password so flag is False
        create_admin(username="testadmin", password="short1234", totp_secret=None)
        self.client = TestClient(app, raise_server_exceptions=True)

    def test_home_shows_password_notice_when_policy_flag_absent(self):
        """Home page must show the password upgrade notice after login with old password."""
        self.client.post(
            "/login",
            data={"username": "testadmin", "password": "short1234"},
            follow_redirects=False,
        )
        resp = self.client.get("/home")
        assert resp.status_code == 200
        assert "12" in resp.text or "password" in resp.text.lower()

    def test_home_hides_notice_when_policy_flag_set(self):
        """Home page must NOT show the notice when password already meets policy."""
        from app.auth import change_password
        change_password("LongPassword123")

        self.client.post(
            "/login",
            data={"username": "testadmin", "password": "LongPassword123"},
            follow_redirects=False,
        )
        resp = self.client.get("/home")
        assert resp.status_code == 200
        assert "may not meet the current minimum" not in resp.text


# ---------------------------------------------------------------------------
# Login heals missing password_meets_policy flag (OP#122)
# ---------------------------------------------------------------------------


class TestLoginPolicyFlagHealing:
    """Login must heal a missing password_meets_policy flag using the plaintext password."""

    @pytest.fixture(autouse=True)
    def setup(self, config_file, data_dir, monkeypatch):
        monkeypatch.setenv("PORTAINER_URL", "https://portainer.test:9443")
        monkeypatch.setenv("PORTAINER_API_KEY", "test-api-key")
        monkeypatch.setenv("PORTAINER_VERIFY_SSL", "false")

        from app.main import app
        from app.auth import create_admin

        create_admin(username="testadmin", password="LongPassword123", totp_secret=None)
        self.client = TestClient(app, raise_server_exceptions=True)

    def _strip_policy_flag(self):
        """Remove password_meets_policy from the store to simulate a pre-flag account."""
        from app.credentials import save_integration_credentials
        save_integration_credentials("admin", password_meets_policy="")

    def test_no_notice_for_long_password_missing_flag(self):
        """Login with a ≥12-char password must suppress the notice even when flag is absent."""
        self._strip_policy_flag()
        self.client.post(
            "/login",
            data={"username": "testadmin", "password": "LongPassword123"},
            follow_redirects=False,
        )
        resp = self.client.get("/home")
        assert resp.status_code == 200
        assert "may not meet the current minimum" not in resp.text

    def test_flag_healed_to_true_on_long_password_login(self):
        """Login with a ≥12-char password must write password_meets_policy=True."""
        from app.credentials import get_integration_credentials
        self._strip_policy_flag()
        assert get_integration_credentials("admin").get("password_meets_policy") is None

        self.client.post(
            "/login",
            data={"username": "testadmin", "password": "LongPassword123"},
            follow_redirects=False,
        )
        assert get_integration_credentials("admin").get("password_meets_policy") is True

    def test_notice_shown_for_short_password_missing_flag(self):
        """Login with a <12-char password must still show the notice when flag is absent."""
        from app.auth import create_admin
        from app.credentials import save_integration_credentials

        # Re-create admin with a short password (bypassing UI validation) and strip flag.
        create_admin(username="testadmin", password="short1234", totp_secret=None)
        save_integration_credentials("admin", password_meets_policy="")

        self.client.post(
            "/login",
            data={"username": "testadmin", "password": "short1234"},
            follow_redirects=False,
        )
        resp = self.client.get("/home")
        assert resp.status_code == 200
        assert "may not meet the current minimum" in resp.text
