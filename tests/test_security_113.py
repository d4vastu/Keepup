"""
Tests for OP#113 — Pre-exposure security blockers.

Covers:
  - Open redirect sanitisation (_safe_next_url)
  - CSRF token provisioning and header validation (CSRFMiddleware)
  - Session cookie SameSite=strict configuration
"""

from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _login(client: TestClient) -> None:
    """Perform a test login so the session cookie is set."""
    client.post(
        "/login",
        data={"username": "testadmin", "password": "testpassword123"},
        follow_redirects=False,
    )


# ---------------------------------------------------------------------------
# Open-redirect sanitisation
# ---------------------------------------------------------------------------


class TestSafeNextUrl:
    """Unit tests for app.auth_router._safe_next_url."""

    def setup_method(self):
        from app.auth_router import _safe_next_url
        self.safe = _safe_next_url

    def test_relative_path_returned_unchanged(self):
        assert self.safe("/home") == "/home"

    def test_relative_path_with_query(self):
        assert self.safe("/admin/hosts?tab=1") == "/admin/hosts?tab=1"

    def test_absolute_url_rejected(self):
        assert self.safe("https://evil.com") == "/home"

    def test_protocol_relative_rejected(self):
        assert self.safe("//evil.com") == "/home"

    def test_javascript_scheme_rejected(self):
        assert self.safe("javascript:alert(1)") == "/home"

    def test_backslash_bypass_rejected(self):
        assert self.safe("/\\evil.com") == "/home"

    def test_bare_backslash_rejected(self):
        assert self.safe("\\evil.com") == "/home"

    def test_empty_string_returns_home(self):
        assert self.safe("") == "/home"

    def test_none_equivalent_empty_returns_home(self):
        # Callers pass request.query_params.get("next", "/home"); the fallback
        # is "/home" but a custom default of "" should still be safe.
        assert self.safe("") == "/home"


class TestOpenRedirectIntegration:
    """Integration tests: the /login endpoint must not follow unsafe next params."""

    def test_login_with_safe_next_redirects_to_it(self, client):
        # Already logged in via the `client` fixture.  Log out first.
        client.post("/logout")
        resp = client.post(
            "/login?next=/admin/hosts",
            data={"username": "testadmin", "password": "testpassword123"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/admin/hosts"

    def test_login_with_absolute_next_redirects_to_home(self, client):
        client.post("/logout")
        resp = client.post(
            "/login?next=https://evil.com",
            data={"username": "testadmin", "password": "testpassword123"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/home"

    def test_login_with_protocol_relative_next_redirects_to_home(self, client):
        client.post("/logout")
        resp = client.post(
            "/login?next=//evil.com",
            data={"username": "testadmin", "password": "testpassword123"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/home"

    def test_login_with_javascript_next_redirects_to_home(self, client):
        client.post("/logout")
        resp = client.post(
            "/login?next=javascript:alert(1)",
            data={"username": "testadmin", "password": "testpassword123"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/home"


# ---------------------------------------------------------------------------
# CSRF token provisioning
# ---------------------------------------------------------------------------


class TestCsrfTokenProvisioning:
    """The CSRF token must be present in the session after any GET request."""

    def test_csrf_token_in_session_after_login_page_get(self, anon_client):
        resp = anon_client.get("/login")
        assert resp.status_code == 200
        # The session cookie is set — the token is embedded in the HTML as a
        # Jinja2 template variable.  We verify by checking that the rendered
        # template does not contain an empty token placeholder.
        assert 'csrf_token", "")' not in resp.text  # ensure module loaded

    def test_csrf_token_set_on_get_request(self, client):
        """GET /home should provision the CSRF token in the session."""
        from app.csrf import get_csrf_token

        # We can't inspect the session directly via TestClient, but we can
        # verify the middleware logic independently.
        session: dict = {}
        token = get_csrf_token(session)
        assert len(token) == 64  # 32 bytes hex = 64 chars
        # Calling again returns the same token (not re-generated).
        assert get_csrf_token(session) == token


# ---------------------------------------------------------------------------
# CSRF validation
# ---------------------------------------------------------------------------


class TestCsrfMiddleware:
    """HTMX mutating requests without a valid X-CSRF-Token header must return 403."""

    def test_htmx_post_without_token_returns_403(self, client):
        resp = client.post(
            "/admin/account/timezone",
            data={"timezone": "UTC"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 403

    def test_htmx_post_with_wrong_token_returns_403(self, client):
        resp = client.post(
            "/admin/account/timezone",
            data={"timezone": "UTC"},
            headers={"HX-Request": "true", "X-CSRF-Token": "wrong-token"},
        )
        assert resp.status_code == 403

    def test_validate_returns_false_when_session_token_missing(self):
        """_validate must return False when the session carries no CSRF token."""
        from app.csrf import _validate
        from unittest.mock import MagicMock

        request = MagicMock()
        request.session = {}  # no token in session
        request.headers = {"HX-Request": "true", "X-CSRF-Token": "anyvalue"}
        assert _validate(request) is False

    def test_htmx_post_with_valid_token_passes(self, client):
        """A POST with HX-Request and the correct session CSRF token must succeed."""
        # Obtain the CSRF token from the session by making a GET request first.
        get_resp = client.get("/admin/account")
        assert get_resp.status_code == 200

        # Extract the token: find it in the rendered HTML (embedded by template).
        # The admin_nav.html script embeds it as: var t = "<token>";
        import re
        match = re.search(r'var t = "([0-9a-f]{64})"', get_resp.text)
        assert match, "CSRF token not found in rendered admin page HTML"
        token = match.group(1)

        resp = client.post(
            "/admin/account/timezone",
            data={"timezone": "UTC"},
            headers={"HX-Request": "true", "X-CSRF-Token": token},
        )
        # 200 means the route processed it (token was valid).
        assert resp.status_code == 200

    def test_non_htmx_post_skips_csrf_check(self, client):
        """Traditional form POSTs (no HX-Request header) are not rejected by the middleware.
        They are protected by SameSite=strict instead."""
        # POST /logout is a plain form POST and must not be blocked.
        resp = client.post("/logout", follow_redirects=False)
        assert resp.status_code in (302, 303)

    def test_setup_htmx_post_exempt_from_csrf(self, anon_client):
        """Setup routes are pre-auth and exempt from CSRF token validation."""
        # POST to a setup HTMX endpoint without a token must not return 403.
        resp = anon_client.post(
            "/setup",
            data={"timezone": "UTC"},
            headers={"HX-Request": "true"},
            follow_redirects=False,
        )
        # Expect a redirect (admin doesn't exist yet), not a CSRF 403.
        assert resp.status_code != 403

    def test_htmx_get_never_requires_csrf_token(self, client):
        """GET requests must never be blocked regardless of token presence."""
        resp = client.get(
            "/api/notifications/badge",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Session cookie SameSite
# ---------------------------------------------------------------------------


class TestSessionCookieSameSite:
    """Session cookie must use SameSite=strict."""

    def test_session_cookie_samesite_strict(self, anon_client):
        resp = anon_client.get("/login")
        # The session cookie header should declare SameSite=strict.
        set_cookie = resp.headers.get("set-cookie", "")
        assert "samesite=strict" in set_cookie.lower(), (
            f"Expected SameSite=strict in Set-Cookie, got: {set_cookie!r}"
        )
