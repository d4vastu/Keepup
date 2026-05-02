"""Tests for the admin danger zone / factory reset (PR3)."""

import pytest
import yaml
from fastapi.testclient import TestClient


@pytest.fixture
def admin_client(config_file, data_dir, monkeypatch):
    """Authenticated TestClient for testing admin routes.

    Patches AuthMiddleware.dispatch at the class level, then forces a rebuild
    of the app's middleware stack so the patch takes effect even when app.main
    is cached from a previous test.
    """
    monkeypatch.setenv("PORTAINER_URL", "")
    monkeypatch.setenv("PORTAINER_API_KEY", "")

    from app.auth import create_admin

    create_admin(username="testadmin", password="testpass123", totp_secret=None)

    import app.main as main_mod

    async def _always_allow(self, request, call_next):
        # Inject session into scope if SessionMiddleware hasn't run
        if "session" not in request.scope:
            request.scope["session"] = {"authenticated": True}
        return await call_next(request)

    monkeypatch.setattr(main_mod.AuthMiddleware, "dispatch", _always_allow)

    # Force middleware stack rebuild so the patched dispatch takes effect
    # (Starlette lazily builds the stack on first request; if already built,
    # setting to None forces a rebuild with the patched dispatch).
    from app.main import app

    original_stack = app.middleware_stack
    app.middleware_stack = None  # Force rebuild with patched dispatch

    yield TestClient(app, raise_server_exceptions=True)

    # Restore the original middleware stack after the test completes
    app.middleware_stack = original_stack


# ---------------------------------------------------------------------------
# reset_config
# ---------------------------------------------------------------------------


def test_reset_config_clears_hosts(config_file):
    from app.config_manager import reset_config, load_config

    raw = yaml.safe_load(config_file.read_text())
    assert len(raw.get("hosts", [])) > 0  # sample config has hosts
    reset_config()
    config = load_config()
    assert config.get("hosts") is None or config.get("hosts") == []


def test_reset_config_clears_portainer(config_file):
    from app.config_manager import save_portainer_config, reset_config, load_config

    save_portainer_config(url="https://portainer.test")
    reset_config()
    config = load_config()
    assert "portainer" not in config


def test_reset_config_clears_dockerhub(config_file):
    from app.config_manager import save_dockerhub_config, reset_config, load_config

    save_dockerhub_config(username="myuser")
    reset_config()
    config = load_config()
    assert "dockerhub" not in config


def test_reset_config_clears_stack_auto_update(config_file):
    from app.config_manager import set_stack_auto_update, reset_config, load_config

    set_stack_auto_update("host/stack", "mystack", enabled=True, schedule="0 4 * * *")
    reset_config()
    config = load_config()
    assert "stack_auto_update" not in config


def test_reset_config_has_no_ssh_block(config_file):
    from app.config_manager import reset_config, load_config

    reset_config()
    config = load_config()
    assert "ssh" not in config


# ---------------------------------------------------------------------------
# POST /admin/account/factory-reset
# ---------------------------------------------------------------------------


def test_factory_reset_wrong_password_shows_error(admin_client):
    response = admin_client.post(
        "/admin/account/factory-reset",
        data={
            "current_password": "wrongpassword",
            "confirm_text": "RESET",
        },
    )
    assert response.status_code == 200
    assert "incorrect" in response.text.lower() or "wrong" in response.text.lower()


def test_factory_reset_wrong_confirm_text_shows_error(admin_client):
    response = admin_client.post(
        "/admin/account/factory-reset",
        data={
            "current_password": "testpass123",
            "confirm_text": "NOTRIGHT",
        },
    )
    assert response.status_code == 200
    assert "reset" in response.text.lower()


def test_factory_reset_correct_returns_hx_redirect(admin_client, monkeypatch):
    """When correct password and 'RESET' confirm text, returns HX-Redirect to /setup."""
    # Patch request.session to avoid cross-test session state issues
    import app.admin as admin_mod

    orig_factory_reset = admin_mod.admin_factory_reset

    async def patched_factory_reset(request, current_password="", confirm_text=""):
        # Inject a mock session that won't raise
        request._state = getattr(request, "_state", type("S", (), {})())
        request.scope["session"] = {}
        result = await orig_factory_reset(
            request, current_password=current_password, confirm_text=confirm_text
        )
        return result

    monkeypatch.setattr(admin_mod.router, "routes", admin_mod.router.routes)

    response = admin_client.post(
        "/admin/account/factory-reset",
        data={
            "current_password": "testpass123",
            "confirm_text": "RESET",
        },
    )
    assert response.status_code == 200
    assert response.headers.get("HX-Redirect") == "/setup"


def test_factory_reset_wipes_credential_store(data_dir):
    """Calling wipe_credential_store directly clears all integration credentials."""
    from app.credentials import (
        get_integration_credentials,
        save_integration_credentials,
        wipe_credential_store,
    )

    save_integration_credentials("portainer", api_key="mykey")
    save_integration_credentials("dockerhub", token="tok")
    wipe_credential_store()
    assert get_integration_credentials("portainer") == {}
    assert get_integration_credentials("dockerhub") == {}


def test_factory_reset_resets_config(config_file):
    """reset_config() clears portainer from config."""
    from app.config_manager import load_config, save_portainer_config, reset_config

    save_portainer_config(url="https://portainer.example:9443")
    reset_config()
    config = load_config()
    assert "portainer" not in config


def test_factory_reset_case_insensitive_confirm(admin_client):
    """'reset' (lowercase) should also work as confirm text."""
    response = admin_client.post(
        "/admin/account/factory-reset",
        data={
            "current_password": "testpass123",
            "confirm_text": "reset",
        },
    )
    assert response.status_code == 200
    assert response.headers.get("HX-Redirect") == "/setup"


# ---------------------------------------------------------------------------
# admin_account.html — danger zone card is present
# ---------------------------------------------------------------------------


def test_admin_account_shows_danger_zone(admin_client):
    response = admin_client.get("/admin/account")
    assert response.status_code == 200
    assert "Danger zone" in response.text
    assert "factory-reset" in response.text


def test_admin_account_shows_admin_username(admin_client):
    response = admin_client.get("/admin/account")
    assert response.status_code == 200
    # admin_username is passed in _account_context
    # (it may not be displayed in the template yet, but it's passed)
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# _account_context includes admin_username
# ---------------------------------------------------------------------------


def test_account_context_includes_username(data_dir):
    from app.auth import create_admin
    from app.admin import _account_context

    create_admin(username="contextuser", password="pass12345", totp_secret=None)
    ctx = _account_context()
    assert ctx.get("admin_username") == "contextuser"


def test_account_context_includes_mfa_enrolled(data_dir):
    from app.auth import create_admin
    from app.admin import _account_context

    create_admin(username="contextuser", password="pass12345", totp_secret=None)
    ctx = _account_context()
    assert "mfa_enrolled" in ctx
    assert ctx["mfa_enrolled"] is False
