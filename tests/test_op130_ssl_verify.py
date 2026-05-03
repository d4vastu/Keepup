"""Tests for OP#130 — skip-SSL-verify option for infrastructure integrations."""

import pytest
import yaml
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# config_manager: verify_ssl persisted by each save function
# ---------------------------------------------------------------------------


def test_save_proxmox_config_verify_ssl_false(config_file, tmp_path):
    from app.config_manager import save_proxmox_config, load_config

    save_proxmox_config(url="https://192.168.1.10:8006", verify_ssl=False)
    cfg = load_config()
    assert cfg["proxmox"]["verify_ssl"] is False


def test_save_proxmox_config_verify_ssl_default_true(config_file, tmp_path):
    from app.config_manager import save_proxmox_config, load_config

    save_proxmox_config(url="https://192.168.1.10:8006")
    cfg = load_config()
    assert cfg["proxmox"]["verify_ssl"] is True


def test_save_pbs_config_verify_ssl_false(config_file, tmp_path):
    from app.config_manager import save_pbs_config, load_config

    save_pbs_config(url="https://192.168.1.11:8007", verify_ssl=False)
    cfg = load_config()
    assert cfg["proxmox_backup"]["verify_ssl"] is False


def test_save_opnsense_config_verify_ssl_false(config_file, tmp_path):
    from app.config_manager import save_opnsense_config, load_config

    save_opnsense_config(url="https://192.168.1.1", verify_ssl=False)
    cfg = load_config()
    assert cfg["opnsense"]["verify_ssl"] is False


def test_save_pfsense_config_verify_ssl_false(config_file, tmp_path):
    from app.config_manager import save_pfsense_config, load_config

    save_pfsense_config(url="https://192.168.1.2", verify_ssl=False)
    cfg = load_config()
    assert cfg["pfsense"]["verify_ssl"] is False


def test_save_homeassistant_config_verify_ssl_false(config_file, tmp_path):
    from app.config_manager import save_homeassistant_config, load_config

    save_homeassistant_config(url="http://homeassistant.local:8123", verify_ssl=False)
    cfg = load_config()
    assert cfg["homeassistant"]["verify_ssl"] is False


# ---------------------------------------------------------------------------
# ProxmoxClient: verify_ssl=False passes verify=False to httpx
# ---------------------------------------------------------------------------


def test_proxmox_client_verify_ssl_false_uses_verify_false():
    from app.proxmox_client import ProxmoxClient

    client = ProxmoxClient(
        url="https://192.168.5.226:8006",
        api_token="root@pam!t=abc",
        verify_ssl=False,
    )

    with patch("app.proxmox_client.make_breaker_client") as mock_breaker:
        mock_breaker.return_value = MagicMock()
        client._client()

    call_kwargs = mock_breaker.call_args[1]
    assert call_kwargs.get("verify") is False
    assert "ssl_context" not in call_kwargs


def test_proxmox_client_verify_ssl_true_uses_ssl_context():
    from app.proxmox_client import ProxmoxClient

    client = ProxmoxClient(
        url="https://192.168.5.226:8006",
        api_token="root@pam!t=abc",
        verify_ssl=True,
    )

    with patch("app.proxmox_client.make_breaker_client") as mock_breaker:
        mock_breaker.return_value = MagicMock()
        client._client()

    call_kwargs = mock_breaker.call_args[1]
    assert "verify" not in call_kwargs
    assert "ssl_context" in call_kwargs


# ---------------------------------------------------------------------------
# Fixtures shared by endpoint tests
# ---------------------------------------------------------------------------


@pytest.fixture
def setup_client(config_file, data_dir, monkeypatch):
    monkeypatch.setenv("PORTAINER_URL", "")
    monkeypatch.setenv("PORTAINER_API_KEY", "")
    from app.main import app
    from app.auth import create_admin

    create_admin(username="admin", password="password1234", totp_secret=None)
    return TestClient(app, raise_server_exceptions=True)


def _mock_make_client(status=200, json_data=None):
    """Mock app.auth_router.make_client returning a successful response."""
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = json_data or {}
    resp.raise_for_status = MagicMock()

    inner = AsyncMock()
    inner.get = AsyncMock(return_value=resp)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=inner)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return patch("app.auth_router.make_client", return_value=ctx)


# ---------------------------------------------------------------------------
# Proxmox test endpoint: skip_ssl_verify=1 short-circuits to verify=False
# ---------------------------------------------------------------------------


def test_proxmox_test_skip_ssl_verify_success(setup_client, data_dir):
    with patch("app.auth_router.ProxmoxClient") as MockClient:
        instance = AsyncMock()
        instance.get_version = AsyncMock(return_value={"version": "8.2.0"})
        MockClient.return_value = instance

        resp = setup_client.post(
            "/setup/connect/proxmox/test",
            data={
                "proxmox_url": "https://192.168.5.226:8006",
                "proxmox_token_id": "root@pam!t",
                "proxmox_secret": "secret",
                "skip_ssl_verify": "1",
            },
        )

    assert resp.status_code == 200
    assert "Connected" in resp.text
    call_kwargs = MockClient.call_args[1]
    assert call_kwargs.get("verify_ssl") is False


def test_proxmox_test_skip_ssl_verify_failure(setup_client, data_dir):
    with patch("app.auth_router.ProxmoxClient") as MockClient:
        instance = AsyncMock()
        instance.get_version = AsyncMock(side_effect=Exception("ssl error"))
        MockClient.return_value = instance

        resp = setup_client.post(
            "/setup/connect/proxmox/test",
            data={
                "proxmox_url": "https://192.168.5.226:8006",
                "proxmox_token_id": "root@pam!t",
                "proxmox_secret": "secret",
                "skip_ssl_verify": "1",
            },
        )

    assert resp.status_code == 200
    assert "ssl error" in resp.text


# ---------------------------------------------------------------------------
# Proxmox save endpoint: skip_ssl_verify=1 persists verify_ssl=False
# ---------------------------------------------------------------------------


def test_proxmox_save_persists_verify_ssl_false(setup_client, data_dir, config_file):
    with patch("app.auth_router.ProxmoxClient") as MockClient:
        instance = AsyncMock()
        instance.get_nodes = AsyncMock(return_value=[])
        instance.discover_resources = AsyncMock(return_value=[])
        MockClient.return_value = instance

        setup_client.post(
            "/setup/connect/proxmox/save",
            data={
                "proxmox_url": "https://192.168.5.226:8006",
                "proxmox_token_id": "root@pam!t",
                "proxmox_secret": "secret",
                "skip_ssl_verify": "1",
            },
        )

    cfg = yaml.safe_load(config_file.read_text())
    assert cfg["proxmox"]["verify_ssl"] is False


# ---------------------------------------------------------------------------
# PBS test endpoint: skip_ssl_verify=1 short-circuits to verify=False
# ---------------------------------------------------------------------------


def test_pbs_test_skip_ssl_verify_success(setup_client, data_dir):
    with _mock_make_client(json_data={"data": {"version": "3.1.0"}}):
        resp = setup_client.post(
            "/setup/connect/pbs/test",
            data={
                "pbs_url": "https://192.168.1.11:8007",
                "pbs_api_user": "root@pbs",
                "pbs_token_id": "root@pbs!t",
                "pbs_secret": "secret",
                "skip_ssl_verify": "1",
            },
        )

    assert resp.status_code == 200
    assert "Connected" in resp.text
    assert "Proxmox Backup Server" in resp.text


def test_pbs_test_skip_ssl_verify_failure(setup_client, data_dir):
    ctx = MagicMock()
    inner = AsyncMock()
    inner.get = AsyncMock(side_effect=Exception("ssl verify failed"))
    ctx.__aenter__ = AsyncMock(return_value=inner)
    ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("app.auth_router.make_client", return_value=ctx):
        resp = setup_client.post(
            "/setup/connect/pbs/test",
            data={
                "pbs_url": "https://192.168.1.11:8007",
                "pbs_api_user": "root@pbs",
                "pbs_token_id": "root@pbs!t",
                "pbs_secret": "secret",
                "skip_ssl_verify": "1",
            },
        )

    assert resp.status_code == 200
    assert "ssl verify failed" in resp.text


# ---------------------------------------------------------------------------
# PBS save endpoint: skip_ssl_verify=1 persists verify_ssl=False
# ---------------------------------------------------------------------------


def test_pbs_save_persists_verify_ssl_false(setup_client, data_dir, config_file):
    setup_client.post(
        "/setup/connect/pbs/save",
        data={
            "pbs_url": "https://192.168.1.11:8007",
            "pbs_api_user": "root@pbs",
            "pbs_token_id": "root@pbs!t",
            "pbs_secret": "secret",
            "skip_ssl_verify": "1",
        },
    )

    cfg = yaml.safe_load(config_file.read_text())
    assert cfg["proxmox_backup"]["verify_ssl"] is False


def test_pbs_save_default_verify_ssl_true(setup_client, data_dir, config_file):
    setup_client.post(
        "/setup/connect/pbs/save",
        data={
            "pbs_url": "https://192.168.1.11:8007",
            "pbs_api_user": "root@pbs",
            "pbs_token_id": "root@pbs!t",
            "pbs_secret": "secret",
        },
    )

    cfg = yaml.safe_load(config_file.read_text())
    assert cfg["proxmox_backup"]["verify_ssl"] is True


# ---------------------------------------------------------------------------
# OPNsense save endpoint: skip_ssl_verify=1 persists verify_ssl=False
# ---------------------------------------------------------------------------


def test_opnsense_save_persists_verify_ssl_false(setup_client, data_dir, config_file):
    setup_client.post(
        "/setup/connect/opnsense/save",
        data={
            "opnsense_url": "https://192.168.1.1",
            "opnsense_api_key": "key",
            "opnsense_api_secret": "secret",
            "skip_ssl_verify": "1",
        },
    )

    cfg = yaml.safe_load(config_file.read_text())
    assert cfg["opnsense"]["verify_ssl"] is False


def test_opnsense_save_default_verify_ssl_true(setup_client, data_dir, config_file):
    setup_client.post(
        "/setup/connect/opnsense/save",
        data={
            "opnsense_url": "https://192.168.1.1",
            "opnsense_api_key": "key",
            "opnsense_api_secret": "secret",
        },
    )

    cfg = yaml.safe_load(config_file.read_text())
    assert cfg["opnsense"]["verify_ssl"] is True


# ---------------------------------------------------------------------------
# OPNsense test endpoint: skip_ssl_verify=1 short-circuits to verify=False
# ---------------------------------------------------------------------------


def test_opnsense_test_skip_ssl_verify_success(setup_client, data_dir):
    with _mock_make_client() as _:
        resp = setup_client.post(
            "/setup/connect/opnsense/test",
            data={
                "opnsense_url": "https://192.168.1.1",
                "opnsense_api_key": "key",
                "opnsense_api_secret": "secret",
                "skip_ssl_verify": "1",
            },
        )

    assert resp.status_code == 200
    assert "Connected" in resp.text


def test_opnsense_test_skip_ssl_verify_failure(setup_client, data_dir):
    ctx = MagicMock()
    inner = AsyncMock()
    inner.get = AsyncMock(side_effect=Exception("ssl error"))
    ctx.__aenter__ = AsyncMock(return_value=inner)
    ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("app.auth_router.make_client", return_value=ctx):
        resp = setup_client.post(
            "/setup/connect/opnsense/test",
            data={
                "opnsense_url": "https://192.168.1.1",
                "opnsense_api_key": "key",
                "opnsense_api_secret": "secret",
                "skip_ssl_verify": "1",
            },
        )

    assert resp.status_code == 200
    assert "ssl error" in resp.text


# ---------------------------------------------------------------------------
# pfSense save endpoint: skip_ssl_verify=1 persists verify_ssl=False
# ---------------------------------------------------------------------------


def test_pfsense_save_persists_verify_ssl_false(setup_client, data_dir, config_file):
    setup_client.post(
        "/setup/connect/pfsense/save",
        data={
            "pfsense_url": "https://192.168.1.2",
            "pfsense_api_key": "key",
            "skip_ssl_verify": "1",
        },
    )

    cfg = yaml.safe_load(config_file.read_text())
    assert cfg["pfsense"]["verify_ssl"] is False


def test_pfsense_save_default_verify_ssl_true(setup_client, data_dir, config_file):
    setup_client.post(
        "/setup/connect/pfsense/save",
        data={
            "pfsense_url": "https://192.168.1.2",
            "pfsense_api_key": "key",
        },
    )

    cfg = yaml.safe_load(config_file.read_text())
    assert cfg["pfsense"]["verify_ssl"] is True


# ---------------------------------------------------------------------------
# pfSense test endpoint: skip_ssl_verify=1 short-circuits to verify=False
# ---------------------------------------------------------------------------


def test_pfsense_test_skip_ssl_verify_success(setup_client, data_dir):
    with _mock_make_client() as _:
        resp = setup_client.post(
            "/setup/connect/pfsense/test",
            data={
                "pfsense_url": "https://192.168.1.2",
                "pfsense_api_key": "key",
                "skip_ssl_verify": "1",
            },
        )

    assert resp.status_code == 200
    assert "Connected" in resp.text


def test_pfsense_test_skip_ssl_verify_failure(setup_client, data_dir):
    ctx = MagicMock()
    inner = AsyncMock()
    inner.get = AsyncMock(side_effect=Exception("connection refused"))
    ctx.__aenter__ = AsyncMock(return_value=inner)
    ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("app.auth_router.make_client", return_value=ctx):
        resp = setup_client.post(
            "/setup/connect/pfsense/test",
            data={
                "pfsense_url": "https://192.168.1.2",
                "pfsense_api_key": "key",
                "skip_ssl_verify": "1",
            },
        )

    assert resp.status_code == 200
    assert "connection refused" in resp.text


# ---------------------------------------------------------------------------
# Home Assistant save endpoint: skip_ssl_verify=1 persists verify_ssl=False
# ---------------------------------------------------------------------------


def test_homeassistant_save_persists_verify_ssl_false(setup_client, data_dir, config_file):
    setup_client.post(
        "/setup/connect/homeassistant/save",
        data={
            "ha_url": "http://homeassistant.local:8123",
            "ha_token": "eyJ...",
            "skip_ssl_verify": "1",
        },
    )

    cfg = yaml.safe_load(config_file.read_text())
    assert cfg["homeassistant"]["verify_ssl"] is False


def test_homeassistant_save_default_verify_ssl_true(setup_client, data_dir, config_file):
    setup_client.post(
        "/setup/connect/homeassistant/save",
        data={
            "ha_url": "http://homeassistant.local:8123",
            "ha_token": "eyJ...",
        },
    )

    cfg = yaml.safe_load(config_file.read_text())
    assert cfg["homeassistant"]["verify_ssl"] is True


# ---------------------------------------------------------------------------
# admin _integration_status: verify_ssl fields populated from config
# ---------------------------------------------------------------------------


def test_integration_status_includes_verify_ssl_fields(config_file, data_dir, monkeypatch):
    from app.config_manager import (
        save_proxmox_config,
        save_pbs_config,
        save_opnsense_config,
        save_pfsense_config,
        save_homeassistant_config,
    )

    save_proxmox_config(url="https://192.168.1.10:8006", verify_ssl=False)
    save_pbs_config(url="https://192.168.1.11:8007", verify_ssl=True)
    save_opnsense_config(url="https://192.168.1.1", verify_ssl=False)
    save_pfsense_config(url="https://192.168.1.2", verify_ssl=True)
    save_homeassistant_config(url="http://ha.local:8123", verify_ssl=False)

    from app.admin import _integration_status

    status = _integration_status()

    assert status["proxmox_verify_ssl"] is False
    assert status["pbs_verify_ssl"] is True
    assert status["opnsense_verify_ssl"] is False
    assert status["pfsense_verify_ssl"] is True
    assert status["ha_verify_ssl"] is False
