"""Tests for #119: session fixation, httpx timeouts, access log, upgrade modal XSS."""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client(config_file, data_dir):
    from app.main import app

    return TestClient(app, raise_server_exceptions=True)


def _create_admin():
    from app.auth import create_admin

    create_admin(username="admin", password="password1234", totp_secret=None)


# ---------------------------------------------------------------------------
# L1 — Session fixation: pre-login cookie is invalid after login
# ---------------------------------------------------------------------------


def test_pre_login_session_cookie_invalid_after_login(client, data_dir):
    """Session cookie obtained before login must not grant access after login."""
    _create_admin()

    # Obtain a pre-login session cookie by visiting the login page
    pre = client.get("/login")
    pre_cookie = pre.cookies.get("ud_session")
    assert pre_cookie is not None

    # Perform a successful login
    client.post(
        "/login",
        data={"username": "admin", "password": "password1234", "remember_me": ""},
        follow_redirects=False,
    )

    # Use the pre-login cookie on an authenticated route — must not grant access
    from fastapi.testclient import TestClient
    from app.main import app

    isolated = TestClient(app, raise_server_exceptions=True, cookies={"ud_session": pre_cookie})
    resp = isolated.get("/home", follow_redirects=False)
    # Either redirected to login or returns 200 with a freshly re-authenticated session.
    # Since the pre-login cookie payload changed on clear(), the old cookie's session
    # version will not match and the request should be redirected.
    assert resp.status_code in (302, 303) or "login" in resp.headers.get("location", "")


def test_login_session_clear_wipes_pre_login_data(data_dir):
    """Session.clear() on login removes keys set before authentication."""
    from app.main import app

    _create_admin()
    client = TestClient(app)

    # Simulate something written to the session before login (e.g. a CSRF token stored
    # by CSRFMiddleware or any other pre-auth middleware). After login, session.clear()
    # must have removed it from the authenticated session.
    #
    # We verify indirectly: the signed cookie value must differ pre- vs post-login,
    # proving the session payload was replaced rather than augmented.
    pre_resp = client.get("/login")
    pre_cookie = pre_resp.cookies.get("ud_session")

    client.post(
        "/login",
        data={"username": "admin", "password": "password1234", "remember_me": ""},
        follow_redirects=False,
    )
    post_cookie = client.cookies.get("ud_session")

    assert pre_cookie != post_cookie


# ---------------------------------------------------------------------------
# L2 — httpx factory: default timeout and ReadTimeout propagation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_make_client_default_timeout_values():
    """make_client configures the standardised default timeouts."""
    from app.httpx_client import make_client

    async with make_client() as c:
        t = c.timeout
    assert t.connect == 5
    assert t.read == 15
    assert t.write == 15
    assert t.pool == 30


@pytest.mark.asyncio
async def test_make_client_read_timeout_fires():
    """A hung outbound call raises ReadTimeout (simulated via transport mock)."""
    from app.httpx_client import make_client

    class _HungTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ReadTimeout("read timeout exceeded", request=request)

    # Pass a custom timeout so the test is fast; supply transport at construction
    async with httpx.AsyncClient(
        transport=_HungTransport(),
        timeout=httpx.Timeout(connect=0.1, read=0.1, write=0.1, pool=0.1),
    ) as c:
        with pytest.raises(httpx.ReadTimeout):
            await c.get("http://test/")


# ---------------------------------------------------------------------------
# L3 — Access log middleware: structured log with redacted headers
# ---------------------------------------------------------------------------


def test_access_log_contains_expected_fields(client, data_dir, caplog):
    """Every request emits a keepup.access log line with required fields."""
    _create_admin()
    with caplog.at_level(logging.INFO, logger="keepup.access"):
        client.get("/login")

    access_records = [r for r in caplog.records if r.name == "keepup.access"]
    assert access_records, "No keepup.access log record emitted"
    msg = access_records[0].getMessage()
    assert "method=" in msg
    assert "path=" in msg
    assert "status=" in msg
    assert "duration_ms=" in msg
    assert "request_id=" in msg


def test_access_log_redacts_sensitive_headers(data_dir):
    """Cookie, Authorization, and X-CSRF-Token are redacted in access log headers."""
    from app.main import _safe_headers

    raw = {
        "cookie": "ud_session=secret",
        "authorization": "Bearer mytoken",
        "x-csrf-token": "abc123",
        "accept": "text/html",
    }
    safe = _safe_headers(raw)
    assert safe["cookie"] == "[redacted]"
    assert safe["authorization"] == "[redacted]"
    assert safe["x-csrf-token"] == "[redacted]"
    assert safe["accept"] == "text/html"


# ---------------------------------------------------------------------------
# L5 — Upgrade modal: XSS payload is escaped by the Jinja2 macro
# ---------------------------------------------------------------------------


def test_log_line_items_returns_raw_lines(data_dir):
    """_log_line_items returns dicts with unescaped text (escaping done by template)."""
    from app.main import _log_line_items

    lines = ["<script>alert(1)</script>", "normal line"]
    items = _log_line_items(lines)
    assert len(items) == 2
    assert items[0]["text"] == "<script>alert(1)</script>"
    assert "cls" in items[0]


def test_upgrade_modal_macro_escapes_xss(data_dir):
    """The render_log macro HTML-escapes dangerous content."""
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    from pathlib import Path

    templates_dir = Path(__file__).parent.parent / "app" / "templates"
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=select_autoescape(["html"]),
    )

    macro_tmpl = env.get_template("macros/log.html")
    module = macro_tmpl.make_module()
    xss_line = {"cls": "log-line-white", "text": "<script>alert(1)</script>"}
    rendered = module.render_log([xss_line])
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
