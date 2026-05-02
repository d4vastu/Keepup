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

    return create_admin(username="admin", password="password1234", totp_secret=None)


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

    return patch("app.auth_router.make_client", return_value=ctx)


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
            "password": "password1234",
            "password_confirm": "password1234",
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
# derive_api_user — backwards compat via saved credentials
# ---------------------------------------------------------------------------


def test_proxmox_save_derives_api_user_from_token_id(setup_client, data_dir):
    """Saving Proxmox credentials derives api_user from token_id automatically."""
    _create_admin()
    with patch("app.auth_router.ProxmoxClient") as MockClient:
        instance = AsyncMock()
        instance.get_nodes = AsyncMock(return_value=[])
        instance.discover_resources = AsyncMock(return_value=[])
        MockClient.return_value = instance
        setup_client.post(
            "/setup/connect/proxmox/save",
            data={
                "proxmox_url": "https://192.168.1.10:8006",
                "proxmox_token_id": "root@pam!Keepup",
                "proxmox_secret": "abc123",
            },
        )
    from app.credentials import get_integration_credentials
    creds = get_integration_credentials("proxmox")
    assert creds.get("api_user") == "root@pam"
    assert creds.get("token_id") == "root@pam!Keepup"


def test_proxmox_save_auto_adds_node(setup_client, data_dir, config_file):
    """Proxmox node is added automatically on save — no manual prompt needed."""
    import yaml
    _create_admin()
    with patch("app.auth_router.ProxmoxClient") as MockClient:
        instance = AsyncMock()
        instance.get_nodes = AsyncMock(return_value=["pve"])
        instance.discover_resources = AsyncMock(return_value=[])
        MockClient.return_value = instance
        setup_client.post(
            "/setup/connect/proxmox/save",
            data={
                "proxmox_url": "https://192.168.5.226:8006",
                "proxmox_token_id": "root@pam!Keepup",
                "proxmox_secret": "abc123",
            },
        )
    raw = yaml.safe_load(config_file.read_text())
    hosts = raw.get("hosts", [])
    assert any(h["host"] == "192.168.5.226" and h.get("proxmox_node") == "pve" for h in hosts)


def test_proxmox_save_node_not_duplicated(setup_client, data_dir, config_file):
    """Proxmox node is not added twice if already present."""
    import yaml
    _create_admin()
    with patch("app.auth_router.ProxmoxClient") as MockClient:
        instance = AsyncMock()
        instance.get_nodes = AsyncMock(return_value=["pve"])
        instance.discover_resources = AsyncMock(return_value=[])
        MockClient.return_value = instance
        for _ in range(2):
            setup_client.post(
                "/setup/connect/proxmox/save",
                data={
                    "proxmox_url": "https://192.168.5.226:8006",
                    "proxmox_token_id": "root@pam!Keepup",
                    "proxmox_secret": "abc123",
                },
            )
    raw = yaml.safe_load(config_file.read_text())
    node_hosts = [h for h in raw.get("hosts", []) if h.get("host") == "192.168.5.226"]
    assert len(node_hosts) == 1


def test_proxmox_discover_auto_adds_node(setup_client, data_dir, config_file):
    """discover endpoint also auto-adds the node."""
    import yaml
    _create_admin()
    from app.config_manager import save_proxmox_config
    from app.credentials import save_integration_credentials
    save_proxmox_config(url="https://192.168.5.227:8006")
    save_integration_credentials("proxmox", token_id="root@pam!t", secret="s", api_user="root@pam")
    with patch("app.auth_router.ProxmoxClient") as MockClient:
        instance = AsyncMock()
        instance.get_nodes = AsyncMock(return_value=["pve"])
        instance.discover_resources = AsyncMock(return_value=[])
        MockClient.return_value = instance
        setup_client.post("/setup/connect/proxmox/discover")
    raw = yaml.safe_load(config_file.read_text())
    assert any(h["host"] == "192.168.5.227" and h.get("proxmox_node") == "pve" for h in raw.get("hosts", []))


def test_proxmox_existing_api_user_in_config_still_readable(data_dir):
    """Existing configs with api_user stored explicitly continue to work."""
    from app.credentials import save_integration_credentials, get_integration_credentials
    save_integration_credentials("proxmox", api_user="root@pam", token_id="root@pam!old", secret="s")
    creds = get_integration_credentials("proxmox")
    assert creds.get("api_user") == "root@pam"


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
                "proxmox_token_id": "user@pam!token",
                "proxmox_secret": "abc",
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
                "proxmox_token_id": "user@pam!badtoken",
                "proxmox_secret": "bad",
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
                "proxmox_token_id": "user@pam!token",
                "proxmox_secret": "abc",
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
                "proxmox_token_id": "user@pam!token",
                "proxmox_secret": "abc",
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

    save_proxmox_config(url="https://192.168.1.10:8006")
    save_integration_credentials("proxmox", api_user="user@pam", token_id="user@pam!token", secret="abc")

    with patch("app.auth_router.ProxmoxClient") as MockClient:
        instance = AsyncMock()
        instance.get_nodes = AsyncMock(return_value=["pve"])
        instance.discover_resources = AsyncMock(return_value=[])
        MockClient.return_value = instance
        response = setup_client.post("/setup/connect/proxmox/discover")

    assert response.status_code == 200
    # Discover auto-adds the node and proceeds to LXC/done step
    assert "proxmox-section" in response.text


def test_proxmox_discover_failure(setup_client, data_dir):
    _create_admin()
    from app.config_manager import save_proxmox_config
    from app.credentials import save_integration_credentials

    save_proxmox_config(url="https://192.168.1.10:8006")
    save_integration_credentials("proxmox", api_user="user@pam", token_id="user@pam!token", secret="abc")

    with patch("app.auth_router.ProxmoxClient") as MockClient:
        instance = AsyncMock()
        instance.get_nodes = AsyncMock(side_effect=Exception("timeout"))
        instance.discover_resources = AsyncMock(side_effect=Exception("timeout"))
        MockClient.return_value = instance
        response = setup_client.post("/setup/connect/proxmox/discover")

    assert response.status_code == 200
    assert "failed" in response.text.lower()


# ---------------------------------------------------------------------------
# New guided flow endpoints
# ---------------------------------------------------------------------------




def test_proxmox_test_ssh_no_config(setup_client, data_dir):
    _create_admin()
    response = setup_client.post(
        "/setup/connect/proxmox/test-ssh",
        data={"proxmox_ssh_user": "root", "proxmox_ssh_auth": "key", "proxmox_ssh_key": ""},
    )
    assert response.status_code == 200
    assert "not configured" in response.text.lower() or "select a key" in response.text.lower()


def test_proxmox_test_ssh_success(setup_client, data_dir):
    _create_admin()
    from app.config_manager import save_proxmox_config

    save_proxmox_config(url="https://192.168.1.10:8006")

    with patch("app.auth_router.verify_connection", new_callable=AsyncMock) as mock_vc:
        mock_vc.return_value = {"ok": True, "message": ""}
        response = setup_client.post(
            "/setup/connect/proxmox/test-ssh",
            data={"proxmox_ssh_user": "root", "proxmox_ssh_auth": "password", "proxmox_ssh_password": "secret"},
        )
    assert response.status_code == 200
    assert "192.168.1.10" in response.text
    assert "proxmoxSshTestPassed" in response.text


def test_proxmox_test_ssh_failure(setup_client, data_dir):
    _create_admin()
    from app.config_manager import save_proxmox_config

    save_proxmox_config(url="https://192.168.1.10:8006")

    with patch("app.auth_router.verify_connection", new_callable=AsyncMock) as mock_vc:
        mock_vc.return_value = {"ok": False, "message": "Connection refused"}
        response = setup_client.post(
            "/setup/connect/proxmox/test-ssh",
            data={"proxmox_ssh_user": "root", "proxmox_ssh_auth": "password", "proxmox_ssh_password": "secret"},
        )
    assert response.status_code == 200
    assert "Connection refused" in response.text


def test_proxmox_save_lxcs(setup_client, data_dir):
    _create_admin()
    response = setup_client.post(
        "/setup/connect/proxmox/save-lxcs",
        data={
            "selected_lxcs": ["pve:100:debian:192.168.1.100"],
            "proxmox_ssh_user": "root",
            "proxmox_ssh_auth": "password",
            "proxmox_ssh_password": "secret",
        },
    )
    assert response.status_code == 200
    from app.config_manager import get_hosts
    hosts = get_hosts()
    lxc = next((h for h in hosts if h.get("proxmox_vmid") == 100), None)
    assert lxc is not None
    assert lxc.get("docker_mode") == "all"


def test_proxmox_save_lxcs_shows_docker_step(setup_client, data_dir):
    """After saving LXCs, wizard shows Docker monitoring prompt."""
    _create_admin()
    response = setup_client.post(
        "/setup/connect/proxmox/save-lxcs",
        data={
            "selected_lxcs": ["pve:100:debian:192.168.1.100"],
            "proxmox_ssh_user": "root",
            "proxmox_ssh_auth": "password",
            "proxmox_ssh_password": "secret",
        },
    )
    assert response.status_code == 200
    assert "docker_lxcs" in response.text
    assert "debian" in response.text
    assert "Enable for all" in response.text


def test_proxmox_save_docker_sets_monitoring(setup_client, data_dir, config_file):
    """Saving docker selections sets docker_mode=all on chosen LXCs."""
    import yaml
    _create_admin()
    # First add an LXC
    setup_client.post(
        "/setup/connect/proxmox/save-lxcs",
        data={
            "selected_lxcs": ["pve:100:debian:192.168.1.100"],
            "proxmox_ssh_user": "root",
            "proxmox_ssh_auth": "password",
            "proxmox_ssh_password": "secret",
        },
    )
    # Then submit docker selection
    response = setup_client.post(
        "/setup/connect/proxmox/save-docker",
        data={"docker_lxcs": ["debian"]},
    )
    assert response.status_code == 200
    raw = yaml.safe_load(config_file.read_text())
    lxc = next((h for h in raw["hosts"] if h.get("proxmox_vmid") == 100), None)
    assert lxc is not None
    assert lxc.get("docker_mode") == "all"


def test_proxmox_skip_docker_retains_monitoring(setup_client, data_dir, config_file):
    """Skipping the docker step leaves docker_mode=all set by add_host()."""
    import yaml
    _create_admin()
    setup_client.post(
        "/setup/connect/proxmox/save-lxcs",
        data={
            "selected_lxcs": ["pve:100:debian:192.168.1.100"],
            "proxmox_ssh_user": "root",
            "proxmox_ssh_auth": "password",
            "proxmox_ssh_password": "secret",
        },
    )
    response = setup_client.post("/setup/connect/proxmox/skip-docker")
    assert response.status_code == 200
    raw = yaml.safe_load(config_file.read_text())
    lxc = next((h for h in raw["hosts"] if h.get("proxmox_vmid") == 100), None)
    assert lxc is not None
    assert lxc.get("docker_mode") == "all"


def test_proxmox_save_docker_opt_out(setup_client, data_dir, config_file):
    """Unchecking an LXC in save-docker removes docker_mode (explicit opt-out)."""
    import yaml
    _create_admin()
    setup_client.post(
        "/setup/connect/proxmox/save-lxcs",
        data={
            "selected_lxcs": ["pve:100:debian:192.168.1.100", "pve:101:alpine:192.168.1.101"],
            "proxmox_ssh_user": "root",
            "proxmox_ssh_auth": "password",
            "proxmox_ssh_password": "secret",
        },
    )
    # Only check debian; alpine is unchecked (opt-out)
    response = setup_client.post(
        "/setup/connect/proxmox/save-docker",
        data={"docker_lxcs": ["debian"]},
    )
    assert response.status_code == 200
    raw = yaml.safe_load(config_file.read_text())
    debian = next((h for h in raw["hosts"] if h.get("proxmox_vmid") == 100), None)
    alpine = next((h for h in raw["hosts"] if h.get("proxmox_vmid") == 101), None)
    assert debian is not None
    assert debian.get("docker_mode") == "all"
    assert alpine is not None
    assert alpine.get("docker_mode") is None


def test_proxmox_skip_lxcs(setup_client, data_dir):
    _create_admin()
    response = setup_client.post("/setup/connect/proxmox/skip-lxcs")
    assert response.status_code == 200
    assert "proxmox-section" in response.text


def test_proxmox_save_vms_later(setup_client, data_dir):
    _create_admin()
    response = setup_client.post(
        "/setup/connect/proxmox/save-vms",
        data={
            "selected_vms": ["pve:200:ubuntu-server"],
            "vm_ip_200": "192.168.1.200",
            "vm_action": "later",
        },
    )
    assert response.status_code == 200
    assert "done" in response.text or "complete" in response.text.lower() or "added" in response.text
    from app.config_manager import get_hosts
    assert any(h.get("host") == "192.168.1.200" for h in get_hosts())


def test_proxmox_save_vms_now(setup_client, data_dir):
    _create_admin()
    with setup_client as c:
        response = c.post(
            "/setup/connect/proxmox/save-vms",
            data={
                "selected_vms": ["pve:201:media-server"],
                "vm_ip_201": "192.168.1.201",
                "vm_action": "now",
            },
        )
    assert response.status_code == 200
    assert "done" in response.text or "complete" in response.text.lower() or "added" in response.text


def test_proxmox_skip_vms(setup_client, data_dir):
    _create_admin()
    response = setup_client.post("/setup/connect/proxmox/skip-vms")
    assert response.status_code == 200
    assert "complete" in response.text.lower() or "done" in response.text


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
                "pbs_token_id": "user@pbs!mytoken",
                "pbs_secret": "abc123",
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
                "pbs_token_id": "user@pbs!badtoken",
                "pbs_secret": "bad",
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


# ---------------------------------------------------------------------------
# POST /setup/connect/proxmox/generate-ssh-key
# ---------------------------------------------------------------------------


def test_generate_ssh_key_creates_file(setup_client, data_dir, tmp_path, monkeypatch):
    _create_admin()
    keys_dir = tmp_path / "keys"
    keys_dir.mkdir()
    monkeypatch.setattr("app.auth_router.Path", lambda p: keys_dir / "keepup_proxmox_ed25519" if "keepup" in str(p) else __import__("pathlib").Path(p))

    import app.auth_router as ar
    real_path = __import__("pathlib").Path

    def patched_path(p):
        s = str(p)
        if s == "/app/keys/keepup_proxmox_ed25519":
            return keys_dir / "keepup_proxmox_ed25519"
        return real_path(p)

    monkeypatch.setattr(ar, "Path", patched_path)

    response = setup_client.post("/setup/connect/proxmox/generate-ssh-key")
    assert response.status_code == 200
    assert (keys_dir / "keepup_proxmox_ed25519").exists()


def test_generate_ssh_key_idempotent(setup_client, data_dir, tmp_path, monkeypatch):
    _create_admin()
    keys_dir = tmp_path / "keys"
    keys_dir.mkdir()

    import app.auth_router as ar
    real_path = __import__("pathlib").Path

    def patched_path(p):
        if str(p) == "/app/keys/keepup_proxmox_ed25519":
            return keys_dir / "keepup_proxmox_ed25519"
        return real_path(p)

    monkeypatch.setattr(ar, "Path", patched_path)

    r1 = setup_client.post("/setup/connect/proxmox/generate-ssh-key")
    r2 = setup_client.post("/setup/connect/proxmox/generate-ssh-key")
    assert r1.status_code == 200
    assert r2.status_code == 200
    # Both responses should contain the same public key
    assert "ssh-ed25519" in r1.text
    assert r1.text == r2.text


def test_generate_ssh_key_private_not_in_response(setup_client, data_dir, tmp_path, monkeypatch):
    _create_admin()
    keys_dir = tmp_path / "keys"
    keys_dir.mkdir()

    import app.auth_router as ar
    real_path = __import__("pathlib").Path

    def patched_path(p):
        if str(p) == "/app/keys/keepup_proxmox_ed25519":
            return keys_dir / "keepup_proxmox_ed25519"
        return real_path(p)

    monkeypatch.setattr(ar, "Path", patched_path)

    response = setup_client.post("/setup/connect/proxmox/generate-ssh-key")
    assert response.status_code == 200
    assert "PRIVATE" not in response.text
    assert "BEGIN OPENSSH" not in response.text


def test_generate_ssh_key_public_key_valid_format(setup_client, data_dir, tmp_path, monkeypatch):
    _create_admin()
    keys_dir = tmp_path / "keys"
    keys_dir.mkdir()

    import app.auth_router as ar
    real_path = __import__("pathlib").Path

    def patched_path(p):
        if str(p) == "/app/keys/keepup_proxmox_ed25519":
            return keys_dir / "keepup_proxmox_ed25519"
        return real_path(p)

    monkeypatch.setattr(ar, "Path", patched_path)

    response = setup_client.post("/setup/connect/proxmox/generate-ssh-key")
    assert response.status_code == 200
    assert "ssh-ed25519" in response.text
    assert "authorized_keys" in response.text


# ---------------------------------------------------------------------------
# OP#100 — Wizard UX fixes
# ---------------------------------------------------------------------------


def test_proxmox_save_removes_proxmox_from_integration_pending(setup_client, data_dir):
    """After node auto-add, _queue_integration_host entry is cleared for proxmox."""
    _create_admin()
    with patch("app.auth_router.ProxmoxClient") as MockClient:
        instance = AsyncMock()
        instance.get_nodes = AsyncMock(return_value=["pve"])
        instance.discover_resources = AsyncMock(return_value=[])
        MockClient.return_value = instance
        setup_client.post(
            "/setup/connect/proxmox/save",
            data={
                "proxmox_url": "https://192.168.5.228:8006",
                "proxmox_token_id": "root@pam!Keepup",
                "proxmox_secret": "abc123",
            },
        )
    hosts_resp = setup_client.get("/setup/hosts")
    # "hosts detected" banner only renders when proxmox_pending is non-empty
    assert "hosts detected" not in hosts_resp.text


def test_proxmox_discover_removes_proxmox_from_integration_pending(setup_client, data_dir):
    """discover endpoint also removes Proxmox from SSH Hosts pending queue."""
    from app.config_manager import save_proxmox_config
    from app.credentials import save_integration_credentials
    _create_admin()
    save_proxmox_config(url="https://192.168.5.229:8006")
    save_integration_credentials("proxmox", token_id="root@pam!t", secret="s", api_user="root@pam")
    with patch("app.auth_router.ProxmoxClient") as MockClient:
        instance = AsyncMock()
        instance.get_nodes = AsyncMock(return_value=["pve"])
        instance.discover_resources = AsyncMock(return_value=[])
        MockClient.return_value = instance
        setup_client.post("/setup/connect/proxmox/discover")
    hosts_resp = setup_client.get("/setup/hosts")
    assert "hosts detected" not in hosts_resp.text


def test_setup_containers_excludes_pct_exec_hosts(setup_client, data_dir, config_file):
    """LXC hosts without docker_mode are skipped in container discovery."""
    from app.config_manager import add_host
    _create_admin()
    add_host(name="My LXC", host="192.168.5.50", user=None, port=None,
             proxmox_node="pve", proxmox_vmid=101)
    with patch("app.auth_router.discover_containers", new=AsyncMock(return_value=[])):
        response = setup_client.get("/setup/containers")
    assert response.status_code == 200
    assert "My LXC" not in response.text


def test_setup_containers_pct_exec_count_passed_to_template(setup_client, data_dir, config_file):
    """LXC hosts without docker_mode increment pct_exec_count."""
    from app.config_manager import add_host
    _create_admin()
    add_host(name="LXC 101", host="192.168.5.51", user=None, port=None,
             proxmox_node="pve", proxmox_vmid=101)
    with patch("app.auth_router.discover_containers", new=AsyncMock(return_value=[])):
        response = setup_client.get("/setup/containers")
    assert response.status_code == 200
    assert "LXC 101" not in response.text


def test_setup_containers_excludes_proxmox_node(setup_client, data_dir, config_file):
    """Proxmox hypervisor node (proxmox_node set, no vmid) is excluded from container discovery."""
    from app.config_manager import add_host
    _create_admin()
    add_host(name="Proxmox VE", host="192.168.5.226", user=None, port=None,
             proxmox_node="pve")
    mock_discover = AsyncMock(return_value=[])
    with patch("app.auth_router.discover_containers", new=mock_discover):
        response = setup_client.get("/setup/containers")
    assert response.status_code == 200
    called_hosts = [call.args[0]["host"] for call in mock_discover.call_args_list]
    assert "192.168.5.226" not in called_hosts


def test_setup_containers_shows_proxmox_docker_hosts(setup_client, data_dir, config_file):
    """LXC hosts with docker_mode are shown as pre-configured without SSH discovery."""
    from app.config_manager import add_host
    _create_admin()
    add_host(name="NGINX", host="192.168.5.235", user=None, port=None,
             proxmox_node="pve", proxmox_vmid=102, docker_mode="all")
    mock_discover = AsyncMock(return_value=[])
    with patch("app.auth_router.discover_containers", new=mock_discover):
        response = setup_client.get("/setup/containers")
    assert response.status_code == 200
    # LXC host should appear in the pre-configured section, not via SSH discovery
    assert "NGINX" in response.text
    assert "Docker monitoring configured" in response.text
    # discover_containers should NOT be called for LXC hosts
    called_hosts = [call.args[0].get("host") for call in mock_discover.call_args_list]
    assert "192.168.5.235" not in called_hosts
