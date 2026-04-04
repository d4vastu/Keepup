"""Tests for admin HTTPS routes."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def admin_client(config_file, data_dir, monkeypatch):
    """Authenticated TestClient for testing admin HTTPS routes."""
    monkeypatch.setenv("PORTAINER_URL", "")
    monkeypatch.setenv("PORTAINER_API_KEY", "")
    monkeypatch.setenv("DATA_PATH", str(data_dir))

    # Point ssl_manager at the temp data dir
    import app.ssl_manager as sm
    monkeypatch.setattr(sm, "_DATA_DIR", data_dir)

    from app.auth import create_admin
    create_admin(username="testadmin", password="testpass123", totp_secret=None)

    import app.main as main_mod

    async def _always_allow(self, request, call_next):
        if "session" not in request.scope:
            request.scope["session"] = {"authenticated": True}
        return await call_next(request)

    monkeypatch.setattr(main_mod.AuthMiddleware, "dispatch", _always_allow)

    from app.main import app
    original_stack = app.middleware_stack
    app.middleware_stack = None  # Force rebuild with patched dispatch

    yield TestClient(app, raise_server_exceptions=True)

    app.middleware_stack = original_stack


@pytest.fixture(autouse=True)
def _patch_restart(monkeypatch):
    """Prevent actual SIGTERM from being sent during tests."""
    async def _noop():
        pass
    monkeypatch.setattr("app.admin._restart_after_delay", _noop)


# ---------------------------------------------------------------------------
# GET /admin/https
# ---------------------------------------------------------------------------

def test_get_admin_https_returns_200(admin_client):
    response = admin_client.get("/admin/https")
    assert response.status_code == 200


def test_get_admin_https_shows_http_only_when_no_cert(admin_client):
    response = admin_client.get("/admin/https")
    assert response.status_code == 200
    assert "HTTP only" in response.text


def test_get_admin_https_shows_self_signed_form(admin_client):
    response = admin_client.get("/admin/https")
    assert "Self-signed certificate" in response.text


# ---------------------------------------------------------------------------
# GET /admin/https/custom-form
# ---------------------------------------------------------------------------

def test_get_custom_form_returns_200(admin_client):
    response = admin_client.get("/admin/https/custom-form")
    assert response.status_code == 200


def test_get_custom_form_shows_custom_cert_form(admin_client):
    response = admin_client.get("/admin/https/custom-form")
    assert "Custom certificate" in response.text
    assert "cert_pem" in response.text
    assert "key_pem" in response.text


# ---------------------------------------------------------------------------
# POST /admin/https/self-signed
# ---------------------------------------------------------------------------

def test_self_signed_empty_hostname_shows_error(admin_client):
    response = admin_client.post("/admin/https/self-signed", data={"hostname": ""})
    assert response.status_code == 200
    assert "IP address or hostname" in response.text


def test_self_signed_valid_hostname_returns_restarting(admin_client):
    response = admin_client.post("/admin/https/self-signed", data={"hostname": "192.168.1.10"})
    assert response.status_code == 200
    assert "restarting" in response.text.lower() or "Enabling HTTPS" in response.text


def test_self_signed_creates_cert_files(admin_client, data_dir, monkeypatch):
    monkeypatch.setenv("DATA_PATH", str(data_dir))
    response = admin_client.post("/admin/https/self-signed", data={"hostname": "192.168.1.10"})
    assert response.status_code == 200
    assert (data_dir / "ssl" / "cert.pem").exists()
    assert (data_dir / "ssl" / "key.pem").exists()


def test_self_signed_saves_ssl_config(admin_client, config_file):
    admin_client.post("/admin/https/self-signed", data={"hostname": "192.168.1.50"})
    from app.config_manager import get_ssl_config
    cfg = get_ssl_config()
    assert cfg["mode"] == "self-signed"
    assert cfg["hostname"] == "192.168.1.50"


def test_self_signed_new_url_in_response(admin_client):
    response = admin_client.post("/admin/https/self-signed", data={"hostname": "10.0.0.5"})
    assert "https://10.0.0.5:8765" in response.text


# ---------------------------------------------------------------------------
# POST /admin/https/custom
# ---------------------------------------------------------------------------

def test_custom_no_cert_shows_error(admin_client):
    response = admin_client.post("/admin/https/custom", data={"cert_pem": "", "key_pem": ""})
    assert response.status_code == 200
    assert "Certificate is required" in response.text


def test_custom_invalid_cert_pem_shows_error(admin_client):
    response = admin_client.post("/admin/https/custom", data={
        "cert_pem": "NOT A REAL CERT",
        "key_pem": "",
    })
    assert response.status_code == 200
    assert "Invalid certificate" in response.text


def test_custom_invalid_key_pem_shows_error(admin_client):
    from app.ssl_manager import generate_self_signed_cert
    cert_pem, _ = generate_self_signed_cert("test.local")
    response = admin_client.post("/admin/https/custom", data={
        "cert_pem": cert_pem,
        "key_pem": "NOT A REAL KEY",
    })
    assert response.status_code == 200
    assert "Invalid private key" in response.text


def test_custom_valid_cert_and_key_returns_restarting(admin_client):
    from app.ssl_manager import generate_self_signed_cert
    cert_pem, key_pem = generate_self_signed_cert("test.local")
    response = admin_client.post("/admin/https/custom", data={
        "cert_pem": cert_pem,
        "key_pem": key_pem,
    })
    assert response.status_code == 200
    assert "restarting" in response.text.lower() or "Enabling HTTPS" in response.text


def test_custom_valid_saves_files(admin_client, data_dir, monkeypatch):
    monkeypatch.setenv("DATA_PATH", str(data_dir))
    from app.ssl_manager import generate_self_signed_cert
    cert_pem, key_pem = generate_self_signed_cert("test.local")
    admin_client.post("/admin/https/custom", data={"cert_pem": cert_pem, "key_pem": key_pem})
    assert (data_dir / "ssl" / "cert.pem").exists()
    assert (data_dir / "ssl" / "key.pem").exists()


def test_custom_missing_key_shows_error(admin_client):
    from app.ssl_manager import generate_self_signed_cert
    cert_pem, _ = generate_self_signed_cert("test.local")
    response = admin_client.post("/admin/https/custom", data={
        "cert_pem": cert_pem,
        "key_pem": "",
    })
    assert response.status_code == 200
    assert "Private key is required" in response.text


# ---------------------------------------------------------------------------
# POST /admin/https/disable
# ---------------------------------------------------------------------------

def test_disable_removes_cert_files(admin_client, data_dir, monkeypatch):
    monkeypatch.setenv("DATA_PATH", str(data_dir))
    # First enable SSL
    from app.ssl_manager import generate_self_signed_cert, save_ssl_files
    cert_pem, key_pem = generate_self_signed_cert("192.168.1.1")
    save_ssl_files(cert_pem, key_pem)
    assert (data_dir / "ssl" / "cert.pem").exists()

    response = admin_client.post("/admin/https/disable")
    assert response.status_code == 200
    assert not (data_dir / "ssl" / "cert.pem").exists()


def test_disable_returns_restarting_template(admin_client):
    response = admin_client.post("/admin/https/disable")
    assert response.status_code == 200
    assert "restarting" in response.text.lower() or "Disabling HTTPS" in response.text


def test_disable_when_no_cert_still_returns_restarting(admin_client):
    """Disabling when no cert is installed should not error."""
    response = admin_client.post("/admin/https/disable")
    assert response.status_code == 200
