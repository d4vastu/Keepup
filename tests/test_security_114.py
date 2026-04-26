"""
Tests for OP#114 — Transport hardening: HTTPS redirect and security headers.

Covers:
  - SecurityHeadersMiddleware: header injection, HSTS conditional on TLS
  - ConditionalHTTPSRedirectMiddleware: redirect when TLS active, pass-through when not
  - /api/csp-report endpoint: accepts POST without auth, returns 204
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from app.security_headers import (
    ConditionalHTTPSRedirectMiddleware,
    SecurityHeadersMiddleware,
    _CSP_REPORT_ONLY,
    _HSTS_VALUE,
)


# ---------------------------------------------------------------------------
# Minimal test app helpers
# ---------------------------------------------------------------------------


def _make_app(tls_active: bool = False) -> FastAPI:
    """Return a minimal FastAPI app with security middlewares wired in."""
    test_app = FastAPI()

    @test_app.get("/ping")
    async def ping():
        return PlainTextResponse("pong")

    test_app.add_middleware(SecurityHeadersMiddleware, tls_active=tls_active)
    test_app.add_middleware(ConditionalHTTPSRedirectMiddleware, tls_active=tls_active)
    return test_app


# ---------------------------------------------------------------------------
# SecurityHeadersMiddleware
# ---------------------------------------------------------------------------


class TestSecurityHeadersMiddleware:
    """Verify that security response headers are injected on every reply."""

    def setup_method(self):
        # Use TLS-inactive app by default; override in specific tests.
        self.client = TestClient(_make_app(tls_active=False), raise_server_exceptions=True)
        self.tls_client = TestClient(_make_app(tls_active=True), raise_server_exceptions=True)

    def test_x_content_type_options_present(self):
        """X-Content-Type-Options: nosniff must be set on every response."""
        resp = self.client.get("/ping")
        assert resp.headers.get("x-content-type-options") == "nosniff"

    def test_x_frame_options_present(self):
        """X-Frame-Options: DENY must be set on every response."""
        resp = self.client.get("/ping")
        assert resp.headers.get("x-frame-options") == "DENY"

    def test_referrer_policy_present(self):
        """Referrer-Policy: no-referrer must be set on every response."""
        resp = self.client.get("/ping")
        assert resp.headers.get("referrer-policy") == "no-referrer"

    def test_csp_report_only_present(self):
        """Content-Security-Policy-Report-Only must be set on every response."""
        resp = self.client.get("/ping")
        csp = resp.headers.get("content-security-policy-report-only", "")
        assert csp == _CSP_REPORT_ONLY

    def test_hsts_absent_without_tls(self):
        """HSTS must NOT be set when TLS is not active (HTTP-only deployments)."""
        resp = self.client.get("/ping")
        assert "strict-transport-security" not in resp.headers

    def test_hsts_present_with_tls(self):
        """HSTS must be set when TLS is active."""
        resp = self.tls_client.get("/ping")
        assert resp.headers.get("strict-transport-security") == _HSTS_VALUE

    def test_hsts_value_includes_max_age(self):
        """HSTS value must include a max-age directive."""
        assert "max-age=" in _HSTS_VALUE

    def test_csp_report_only_includes_report_uri(self):
        """CSP policy must direct violation reports to /api/csp-report."""
        assert "report-uri /api/csp-report" in _CSP_REPORT_ONLY

    def test_csp_report_only_blocks_object_src(self):
        """CSP must include object-src 'none' to block Flash/plugin content."""
        assert "object-src 'none'" in _CSP_REPORT_ONLY

    def test_all_required_headers_present_in_single_request(self):
        """All mandatory headers must appear together on a single response."""
        resp = self.client.get("/ping")
        required = [
            "x-content-type-options",
            "x-frame-options",
            "referrer-policy",
            "content-security-policy-report-only",
        ]
        for header in required:
            assert header in resp.headers, f"Missing header: {header}"


# ---------------------------------------------------------------------------
# ConditionalHTTPSRedirectMiddleware
# ---------------------------------------------------------------------------


class TestConditionalHTTPSRedirectMiddleware:
    """Verify HTTPS redirect behaviour."""

    def test_no_redirect_when_tls_inactive(self):
        """HTTP requests must pass through when TLS is not configured."""
        client = TestClient(_make_app(tls_active=False), raise_server_exceptions=True)
        # TestClient sends HTTP by default; with tls_active=False no redirect expected.
        resp = client.get("/ping", follow_redirects=False)
        assert resp.status_code == 200

    def test_redirect_when_tls_active_and_http_scheme(self):
        """An HTTP request must be redirected to HTTPS (301) when TLS is active."""
        # TestClient speaks HTTP internally; the middleware checks request.url.scheme.
        app_with_tls = FastAPI()

        @app_with_tls.get("/ping")
        async def ping():
            return PlainTextResponse("pong")

        app_with_tls.add_middleware(ConditionalHTTPSRedirectMiddleware, tls_active=True)
        client = TestClient(app_with_tls, raise_server_exceptions=True)
        resp = client.get("/ping", follow_redirects=False)
        # Starlette TestClient sends requests as http://testserver/ping
        assert resp.status_code == 301
        assert resp.headers["location"].startswith("https://")

    def test_redirect_preserves_path(self):
        """The 301 redirect must preserve the original request path and query string."""
        app_with_tls = FastAPI()

        @app_with_tls.get("/some/path")
        async def some_path():
            return PlainTextResponse("ok")

        app_with_tls.add_middleware(ConditionalHTTPSRedirectMiddleware, tls_active=True)
        client = TestClient(app_with_tls, raise_server_exceptions=True)
        resp = client.get("/some/path?foo=bar", follow_redirects=False)
        assert resp.status_code == 301
        location = resp.headers["location"]
        assert "/some/path" in location
        assert "foo=bar" in location

    def test_https_request_not_redirected(self):
        """A request that already uses HTTPS must not be redirected."""
        app_with_tls = FastAPI()

        @app_with_tls.get("/ping")
        async def ping():
            return PlainTextResponse("pong")

        app_with_tls.add_middleware(ConditionalHTTPSRedirectMiddleware, tls_active=True)
        client = TestClient(app_with_tls, raise_server_exceptions=True, base_url="https://testserver")
        resp = client.get("/ping", follow_redirects=False)
        # Already HTTPS — should reach the route, not be redirected.
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# /api/csp-report endpoint (integration)
# ---------------------------------------------------------------------------


class TestCspReportEndpoint:
    """Verify the CSP violation report receiver endpoint."""

    @pytest.fixture(autouse=True)
    def setup(self, config_file, data_dir, monkeypatch):
        monkeypatch.setenv("PORTAINER_URL", "https://portainer.test:9443")
        monkeypatch.setenv("PORTAINER_API_KEY", "test-api-key")
        monkeypatch.setenv("PORTAINER_VERIFY_SSL", "false")

        from app.main import app
        self.client = TestClient(app, raise_server_exceptions=True)

    def test_csp_report_returns_204(self):
        """POST to /api/csp-report must return 204 No Content."""
        payload = {
            "csp-report": {
                "document-uri": "https://keepup.local/home",
                "violated-directive": "script-src",
                "blocked-uri": "https://evil.example.com/malicious.js",
            }
        }
        resp = self.client.post(
            "/api/csp-report",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 204

    def test_csp_report_accessible_without_auth(self):
        """CSP report endpoint must be reachable without a session cookie."""
        resp = self.client.post(
            "/api/csp-report",
            content=json.dumps({"csp-report": {"violated-directive": "img-src"}}),
            headers={"Content-Type": "application/json"},
        )
        # Must not redirect to /login (302); must return 204.
        assert resp.status_code == 204

    def test_csp_report_handles_empty_body_gracefully(self):
        """Endpoint must not crash when the browser sends an empty body."""
        resp = self.client.post(
            "/api/csp-report",
            content="",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 204

    def test_csp_report_handles_non_json_body_gracefully(self):
        """Endpoint must not crash when the body is malformed JSON."""
        resp = self.client.post(
            "/api/csp-report",
            content="not-json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 204
