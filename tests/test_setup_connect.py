"""Tests for setup wizard Screen 5 — connect integrations."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
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


def _mock_httpx(status=200, json_data=None, exc=None):
    """Context manager that mocks httpx.AsyncClient used in auth_router."""
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = json_data or {"message": "ok"}
    if exc:
        resp.raise_for_status.side_effect = exc
    else:
        resp.raise_for_status = MagicMock()

    inner = AsyncMock()
    if exc:
        inner.get = AsyncMock(side_effect=exc)
    else:
        inner.get = AsyncMock(return_value=resp)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=inner)
    ctx.__aexit__ = AsyncMock(return_value=None)

    return patch("app.auth_router.httpx.AsyncClient", return_value=ctx)


# ---------------------------------------------------------------------------
# POST /setup/security — no session flag
# ---------------------------------------------------------------------------


def test_setup_security_post_no_session_redirects(setup_client, data_dir):
    _create_admin()
    response = setup_client.post("/setup/security", data={}, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# POST /setup/security — correct TOTP enrolls MFA
# ---------------------------------------------------------------------------


def test_setup_security_correct_totp_enrolls_mfa(setup_client):
    import pyotp
    from app.auth import mfa_enrolled

    setup_client.post(
        "/setup/account",
        data={
            "username": "alice",
            "password": "password123",
            "password_confirm": "password123",
        },
        follow_redirects=False,
    )

    # Load security page to get secret stored in session
    setup_client.get("/setup/security", follow_redirects=False)

    # Peek at the session secret via the credential store (we can't easily read session)
    # Instead, patch new_totp_secret to return a known secret and re-load the page
    known_secret = pyotp.random_base32()

    with patch("app.auth_router.new_totp_secret", return_value=known_secret):
        setup_client.get("/setup/security", follow_redirects=False)

    code = pyotp.TOTP(known_secret).now()
    response = setup_client.post(
        "/setup/security",
        data={
            "enable_mfa": "on",
            "totp_code": code,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "/setup/recovery-code" in response.headers["location"]
    assert mfa_enrolled() is True


# ---------------------------------------------------------------------------
# POST /setup/connect/proxmox/test
# ---------------------------------------------------------------------------


def test_proxmox_test_success(setup_client, data_dir):
    _create_admin()
    with patch("app.auth_router.ProxmoxClient") as MockClient:
        instance = AsyncMock()
        instance.get_version = AsyncMock(return_value={"version": "8.1.4"})
        MockClient.return_value = instance
        response = setup_client.post(
            "/setup/connect/proxmox/test",
            data={
                "proxmox_url": "https://192.168.1.10:8006",
                "proxmox_api_user": "user@pam",
                "proxmox_api_token": "token=abc",
            },
        )
    assert response.status_code == 200
    assert "8.1.4" in response.text


def test_proxmox_test_auth_error(setup_client, data_dir):
    _create_admin()
    with patch("app.auth_router.ProxmoxClient") as MockClient:
        instance = AsyncMock()
        instance.get_version = AsyncMock(side_effect=Exception("401 Unauthorized"))
        MockClient.return_value = instance
        response = setup_client.post(
            "/setup/connect/proxmox/test",
            data={
                "proxmox_url": "https://192.168.1.10:8006",
                "proxmox_api_user": "user@pam",
                "proxmox_api_token": "badtoken",
            },
        )
    assert response.status_code == 200
    assert "Invalid API token" in response.text


def test_proxmox_test_connect_error(setup_client, data_dir):
    _create_admin()
    with patch("app.auth_router.ProxmoxClient") as MockClient:
        instance = AsyncMock()
        instance.get_version = AsyncMock(
            side_effect=Exception("Failed to connect to host")
        )
        MockClient.return_value = instance
        response = setup_client.post(
            "/setup/connect/proxmox/test",
            data={
                "proxmox_url": "https://192.0.2.1:8006",
                "proxmox_api_user": "user@pam",
                "proxmox_api_token": "token=abc",
            },
        )
    assert response.status_code == 200
    assert "reach" in response.text.lower() or "connect" in response.text.lower()


def test_proxmox_test_ssl_error(setup_client, data_dir):
    _create_admin()
    with patch("app.auth_router.ProxmoxClient") as MockClient:
        instance = AsyncMock()
        instance.get_version = AsyncMock(
            side_effect=Exception("SSL certificate verify failed")
        )
        MockClient.return_value = instance
        response = setup_client.post(
            "/setup/connect/proxmox/test",
            data={
                "proxmox_url": "https://192.168.1.10:8006",
                "proxmox_api_user": "user@pam",
                "proxmox_api_token": "token=abc",
            },
        )
    assert response.status_code == 200
    assert "SSL" in response.text


# ---------------------------------------------------------------------------
# POST /setup/connect/proxmox/discover
# ---------------------------------------------------------------------------


def test_proxmox_discover_not_configured(setup_client, data_dir):
    _create_admin()
    response = setup_client.post("/setup/connect/proxmox/discover")
    assert response.status_code == 200
    assert "not configured" in response.text.lower()


def test_proxmox_discover_success(setup_client, data_dir):
    _create_admin()
    from app.config_manager import save_proxmox_config
    from app.credentials import save_integration_credentials

    save_proxmox_config(url="https://192.168.1.10:8006", verify_ssl=False)
    save_integration_credentials("proxmox", api_user="user@pam", api_token="token=abc")

    with patch("app.auth_router.ProxmoxClient") as MockClient:
        instance = AsyncMock()
        instance.discover_resources = AsyncMock(
            return_value=[
                {
                    "type": "vm",
                    "node": "pve",
                    "vmid": 100,
                    "name": "ubuntu",
                    "status": "running",
                },
            ]
        )
        MockClient.return_value = instance
        response = setup_client.post("/setup/connect/proxmox/discover")

    assert response.status_code == 200
    assert "ubuntu" in response.text


def test_proxmox_discover_failure(setup_client, data_dir):
    _create_admin()
    from app.config_manager import save_proxmox_config
    from app.credentials import save_integration_credentials

    save_proxmox_config(url="https://192.168.1.10:8006", verify_ssl=False)
    save_integration_credentials("proxmox", api_user="user@pam", api_token="token=abc")

    with patch("app.auth_router.ProxmoxClient") as MockClient:
        instance = AsyncMock()
        instance.discover_resources = AsyncMock(side_effect=Exception("timeout"))
        MockClient.return_value = instance
        response = setup_client.post("/setup/connect/proxmox/discover")

    assert response.status_code == 200
    assert "failed" in response.text.lower()


# ---------------------------------------------------------------------------
# POST /setup/connect/proxmox/select-hosts
# ---------------------------------------------------------------------------


def test_proxmox_select_hosts(setup_client, data_dir):
    _create_admin()
    response = setup_client.post(
        "/setup/connect/proxmox/select-hosts",
        data={
            "selected_hosts": ["pve:100:vm:ubuntu-vm", "pve:101:lxc:debian-ct"],
        },
    )
    assert response.status_code == 200
    assert "2 hosts" in response.text or "queued" in response.text.lower()


def test_proxmox_select_hosts_empty(setup_client, data_dir):
    _create_admin()
    response = setup_client.post("/setup/connect/proxmox/select-hosts", data={})
    assert response.status_code == 200
    assert "0 hosts" in response.text or "queued" in response.text.lower()


# ---------------------------------------------------------------------------
# POST /setup/connect/pbs/test
# ---------------------------------------------------------------------------


def test_pbs_test_success(setup_client, data_dir):
    _create_admin()
    with _mock_httpx(json_data={"data": {"version": "3.1.0"}}):
        response = setup_client.post(
            "/setup/connect/pbs/test",
            data={
                "pbs_url": "https://192.168.1.11:8007",
                "pbs_api_user": "user@pbs",
                "pbs_api_token": "token=abc",
            },
        )
    assert response.status_code == 200
    assert "Connected" in response.text


def test_pbs_test_auth_failure(setup_client, data_dir):
    _create_admin()
    with _mock_httpx(exc=Exception("401 Unauthorized")):
        response = setup_client.post(
            "/setup/connect/pbs/test",
            data={
                "pbs_url": "https://192.168.1.11:8007",
                "pbs_api_user": "user@pbs",
                "pbs_api_token": "badtoken",
            },
        )
    assert response.status_code == 200
    assert "Invalid" in response.text


# ---------------------------------------------------------------------------
# POST /setup/connect/opnsense/test
# ---------------------------------------------------------------------------


def test_opnsense_test_success(setup_client, data_dir):
    _create_admin()
    with _mock_httpx():
        response = setup_client.post(
            "/setup/connect/opnsense/test",
            data={
                "opnsense_url": "https://192.168.1.1",
                "opnsense_api_key": "mykey",
                "opnsense_api_secret": "mysecret",
            },
        )
    assert response.status_code == 200
    assert "Connected" in response.text


def test_opnsense_test_auth_failure(setup_client, data_dir):
    _create_admin()
    with _mock_httpx(exc=Exception("403 Forbidden")):
        response = setup_client.post(
            "/setup/connect/opnsense/test",
            data={
                "opnsense_url": "https://192.168.1.1",
                "opnsense_api_key": "badkey",
                "opnsense_api_secret": "badsecret",
            },
        )
    assert response.status_code == 200
    assert "Invalid" in response.text


def test_opnsense_test_generic_failure(setup_client, data_dir):
    _create_admin()
    with _mock_httpx(exc=Exception("Something went wrong with the request")):
        response = setup_client.post(
            "/setup/connect/opnsense/test",
            data={
                "opnsense_url": "https://192.168.1.1",
                "opnsense_api_key": "key",
                "opnsense_api_secret": "secret",
            },
        )
    assert response.status_code == 200
    assert (
        "&#10007;" in response.text
        or "error" in response.text.lower()
        or "wrong" in response.text.lower()
    )


# ---------------------------------------------------------------------------
# POST /setup/connect/pfsense/test
# ---------------------------------------------------------------------------


def test_pfsense_test_success(setup_client, data_dir):
    _create_admin()
    with _mock_httpx():
        response = setup_client.post(
            "/setup/connect/pfsense/test",
            data={
                "pfsense_url": "https://192.168.1.1",
                "pfsense_api_key": "mykey",
            },
        )
    assert response.status_code == 200
    assert "Connected" in response.text


def test_pfsense_test_failure(setup_client, data_dir):
    _create_admin()
    with _mock_httpx(exc=Exception("401 Unauthorized")):
        response = setup_client.post(
            "/setup/connect/pfsense/test",
            data={
                "pfsense_url": "https://192.168.1.1",
                "pfsense_api_key": "badkey",
            },
        )
    assert response.status_code == 200
    assert "Invalid" in response.text


# ---------------------------------------------------------------------------
# POST /setup/connect/homeassistant/test
# ---------------------------------------------------------------------------


def test_homeassistant_test_success(setup_client, data_dir):
    _create_admin()
    with _mock_httpx(json_data={"message": "API running.", "version": "2024.1.0"}):
        response = setup_client.post(
            "/setup/connect/homeassistant/test",
            data={
                "ha_url": "http://homeassistant.local:8123",
                "ha_token": "mytoken",
            },
        )
    assert response.status_code == 200
    assert "Connected" in response.text


def test_homeassistant_test_auth_failure(setup_client, data_dir):
    _create_admin()
    with _mock_httpx(exc=Exception("401 Unauthorized")):
        response = setup_client.post(
            "/setup/connect/homeassistant/test",
            data={
                "ha_url": "http://homeassistant.local:8123",
                "ha_token": "badtoken",
            },
        )
    assert response.status_code == 200
    assert "Invalid" in response.text


def test_homeassistant_test_generic_failure(setup_client, data_dir):
    _create_admin()
    with _mock_httpx(exc=Exception("connection refused")):
        response = setup_client.post(
            "/setup/connect/homeassistant/test",
            data={
                "ha_url": "http://192.0.2.1:8123",
                "ha_token": "token",
            },
        )
    assert response.status_code == 200
    assert "&#10007;" in response.text
