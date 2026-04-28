import pytest
import yaml
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def reset_circuit_breakers():
    """Clear per-host circuit breaker state between tests to prevent bleed-over."""
    import app.httpx_client as hc
    hc._breakers.clear()
    yield
    hc._breakers.clear()

SAMPLE_CONFIG = {
    "hosts": [
        {"name": "Test Host", "host": "192.168.1.10", "user": "root"},
        {
            "name": "Custom User Host",
            "host": "192.168.1.20",
            "user": "admin",
            "port": 2222,
        },
    ],
}


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    """Create a temp config.yml and point config_manager at it."""
    cfg = tmp_path / "config.yml"
    cfg.write_text(yaml.dump(SAMPLE_CONFIG, default_flow_style=False))

    import app.config_manager as cm

    monkeypatch.setattr(cm, "_CONFIG_PATH", cfg)

    return cfg


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Create a temp data dir and point credentials module at it."""
    d = tmp_path / "data"
    d.mkdir()

    import app.credentials as creds

    monkeypatch.setattr(creds, "_DATA_DIR", d)
    monkeypatch.setattr(creds, "_SECRET_FILE", d / ".secret")
    monkeypatch.setattr(creds, "_CREDS_FILE", d / "credentials.json")

    # Also patch app.auth so get_session_secret() doesn't try to write /app/data
    import app.auth as auth

    monkeypatch.setattr(auth, "_DATA_DIR", d)
    monkeypatch.setattr(auth, "_SESSION_SECRET_FILE", d / ".session_secret")

    # Patch notifications and auto_update_log data paths
    import app.notifications as notifs

    monkeypatch.setattr(notifs, "_DATA_DIR", d)
    monkeypatch.setattr(notifs, "_NOTIF_PATH", d / "notifications.json")

    import app.auto_update_log as aul

    monkeypatch.setattr(aul, "_DATA_DIR", d)
    monkeypatch.setattr(aul, "_LOG_PATH", d / "auto_update_log.json")

    # Redirect audit log to temp dir so tests can read and verify entries.
    import app.audit as audit_mod

    monkeypatch.setattr(audit_mod, "_DATA_DIR", d)
    audit_mod.setup_audit_log(d)

    yield d

    for h in list(audit_mod._audit_log.handlers):
        audit_mod._audit_log.removeHandler(h)
        h.close()


@pytest.fixture
def client(config_file, data_dir, monkeypatch):
    """Authenticated TestClient."""
    monkeypatch.setenv("PORTAINER_URL", "https://portainer.test:9443")
    monkeypatch.setenv("PORTAINER_API_KEY", "test-api-key")
    monkeypatch.setenv("PORTAINER_VERIFY_SSL", "false")

    from app.main import app
    from app.auth import create_admin

    # Create admin account in the temp credential store
    create_admin(username="testadmin", password="testpassword123", totp_secret=None)

    # Reset per-test rate limit windows so tests don't bleed into each other.
    app.state.admin_rl_window = {}
    from app.rate_limiter import limiter
    limiter.reset()

    tc = TestClient(app, raise_server_exceptions=True)
    # Log in — cookies persist in the TestClient
    resp = tc.post(
        "/login",
        data={"username": "testadmin", "password": "testpassword123"},
        follow_redirects=False,
    )
    assert resp.status_code in (
        302,
        303,
    ), f"Login failed with status {resp.status_code}: {resp.text[:200]}"
    return tc


@pytest.fixture
def anon_client(config_file, data_dir, monkeypatch):
    """Unauthenticated TestClient (admin account exists, but no session cookie)."""
    monkeypatch.setenv("PORTAINER_URL", "https://portainer.test:9443")
    monkeypatch.setenv("PORTAINER_API_KEY", "test-api-key")
    monkeypatch.setenv("PORTAINER_VERIFY_SSL", "false")

    from app.main import app
    from app.auth import create_admin

    create_admin(username="testadmin", password="testpassword123", totp_secret=None)

    return TestClient(app, raise_server_exceptions=True)
