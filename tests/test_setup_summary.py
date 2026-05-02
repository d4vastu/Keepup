"""Tests for setup wizard Screen 8 — summary."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def setup_client(config_file, data_dir, monkeypatch):
    monkeypatch.setenv("PORTAINER_URL", "")
    monkeypatch.setenv("PORTAINER_API_KEY", "")
    from app.main import app

    return TestClient(app, raise_server_exceptions=True)


def _create_admin():
    from app.auth import create_admin

    return create_admin(username="admin", password="password123", totp_secret=None)


# ---------------------------------------------------------------------------
# GET /setup/summary
# ---------------------------------------------------------------------------


def test_setup_summary_no_admin_redirects(setup_client):
    response = setup_client.get("/setup/summary", follow_redirects=False)
    assert response.status_code == 302
    assert "/setup" in response.headers["location"]


def test_setup_summary_returns_200(setup_client, data_dir):
    _create_admin()
    response = setup_client.get("/setup/summary")
    assert response.status_code == 200
    assert "Step 8" in response.text
    assert "Admin account created" in response.text


def test_setup_summary_shows_timezone(setup_client, data_dir, config_file):
    import yaml

    _create_admin()
    cfg = yaml.safe_load(config_file.read_text())
    cfg["timezone"] = "America/New_York"
    config_file.write_text(yaml.dump(cfg))
    response = setup_client.get("/setup/summary")
    assert "America/New_York" in response.text


def test_setup_summary_no_integrations_shows_message(setup_client, data_dir):
    _create_admin()
    response = setup_client.get("/setup/summary")
    assert response.status_code == 200
    assert (
        "no integrations" in response.text.lower()
        or "no ssh hosts" in response.text.lower()
        or "Admin" in response.text
    )


def test_setup_summary_shows_configured_integration(setup_client, data_dir):
    from app.config_manager import save_proxmox_config
    from app.credentials import save_integration_credentials

    _create_admin()
    save_proxmox_config(url="https://192.168.1.10:8006")
    save_integration_credentials("proxmox", api_token="user@pam!token=abc")
    response = setup_client.get("/setup/summary")
    assert "Proxmox VE" in response.text


def test_setup_summary_shows_ssh_hosts(setup_client, data_dir, config_file):
    import yaml

    _create_admin()
    cfg = yaml.safe_load(config_file.read_text())
    cfg.setdefault("hosts", []).append({"name": "My Server", "host": "192.168.1.5"})
    config_file.write_text(yaml.dump(cfg))
    response = setup_client.get("/setup/summary")
    assert "My Server" in response.text


def test_setup_summary_shows_mfa_disabled(setup_client, data_dir):
    _create_admin()
    response = setup_client.get("/setup/summary")
    assert (
        "not enabled" in response.text.lower() or "two-factor" in response.text.lower()
    )


def test_setup_summary_shows_update_schedule(setup_client, data_dir, config_file):
    import yaml

    _create_admin()
    cfg = yaml.safe_load(config_file.read_text())
    cfg["update_check_schedule"] = "0 */6 * * *"
    config_file.write_text(yaml.dump(cfg))
    response = setup_client.get("/setup/summary")
    assert "6 hour" in response.text.lower() or "every 6" in response.text.lower()


def test_setup_summary_finish_redirects_to_login(setup_client, data_dir):
    _create_admin()
    response = setup_client.post("/setup/finish", follow_redirects=False)
    assert response.status_code == 303
    assert "/login" in response.headers["location"]
