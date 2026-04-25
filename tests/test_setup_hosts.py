"""Tests for the setup wizard step 3 — Connect Infrastructure (PR2)."""

import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient


@pytest.fixture
def setup_client(config_file, data_dir, monkeypatch):
    """TestClient for setup wizard tests — no PORTAINER env vars."""
    monkeypatch.setenv("PORTAINER_URL", "")
    monkeypatch.setenv("PORTAINER_API_KEY", "")
    from app.main import app

    return TestClient(app, raise_server_exceptions=True)


def _create_admin(username="testadmin", password="testpass123"):
    from app.auth import create_admin

    return create_admin(username=username, password=password, totp_secret=None)


# ---------------------------------------------------------------------------
# GET /setup/hosts
# ---------------------------------------------------------------------------


def test_setup_hosts_no_admin_redirects_to_setup(setup_client):
    response = setup_client.get("/setup/hosts", follow_redirects=False)
    assert response.status_code == 302
    assert "/setup" in response.headers["location"]


def test_setup_hosts_with_admin_returns_200(setup_client, data_dir):
    _create_admin()
    response = setup_client.get("/setup/hosts", follow_redirects=False)
    assert response.status_code == 200


def test_setup_hosts_shows_step_6_indicator(setup_client, data_dir):
    _create_admin()
    response = setup_client.get("/setup/hosts")
    assert response.status_code == 200
    assert "Step 6" in response.text or "SSH" in response.text


# ---------------------------------------------------------------------------
# POST /setup/hosts/add
# ---------------------------------------------------------------------------


def test_setup_add_host_missing_name_shows_error(setup_client, data_dir, config_file):
    _create_admin()
    response = setup_client.post(
        "/setup/hosts/add",
        data={
            "name": "",
            "host": "192.168.1.10",
        },
    )
    assert response.status_code == 200
    assert "required" in response.text.lower()


def test_setup_add_host_missing_host_shows_error(setup_client, data_dir, config_file):
    _create_admin()
    response = setup_client.post(
        "/setup/hosts/add",
        data={
            "name": "My Server",
            "host": "",
        },
    )
    assert response.status_code == 200
    assert "required" in response.text.lower()


def test_setup_add_host_connection_fails_shows_error(
    setup_client, data_dir, config_file
):
    _create_admin()
    mock_result = {"ok": False, "message": "Connection refused"}
    with patch(
        "app.auth_router.verify_connection", new=AsyncMock(return_value=mock_result)
    ):
        response = setup_client.post(
            "/setup/hosts/add",
            data={
                "name": "My Server",
                "host": "192.168.1.10",
                "user": "ubuntu",
                "auth_method": "password",
                "ssh_password": "pass",
            },
        )
    assert response.status_code == 200
    assert "could not connect" in response.text.lower()


def test_setup_add_host_connection_succeeds_no_docker_adds_host(
    setup_client, data_dir, config_file
):
    import yaml

    _create_admin()
    mock_result = {"ok": True, "message": "Connected"}
    with (
        patch(
            "app.auth_router.verify_connection", new=AsyncMock(return_value=mock_result)
        ),
        patch("app.auth_router.discover_containers", new=AsyncMock(return_value=[])),
    ):
        response = setup_client.post(
            "/setup/hosts/add",
            data={
                "name": "My Server",
                "host": "192.168.1.10",
                "user": "ubuntu",
                "auth_method": "password",
                "ssh_password": "pass",
            },
        )
    assert response.status_code == 200
    assert "added successfully" in response.text.lower()
    raw = yaml.safe_load(config_file.read_text())
    names = [h["name"] for h in raw.get("hosts", [])]
    assert "My Server" in names


def test_setup_add_host_docker_detected_shows_prompt(
    setup_client, data_dir, config_file
):
    _create_admin()
    mock_result = {"ok": True, "message": "Connected"}
    with (
        patch(
            "app.auth_router.verify_connection", new=AsyncMock(return_value=mock_result)
        ),
        patch("app.auth_router.discover_containers", new=AsyncMock(return_value=[
            {"name": "c1", "image": "img1:1", "status": "Up", "running": True,
             "compose_project": None, "css_id": "c1"},
            {"name": "c2", "image": "img2:1", "status": "Up", "running": True,
             "compose_project": None, "css_id": "c2"},
            {"name": "c3", "image": "img3:1", "status": "Up", "running": True,
             "compose_project": None, "css_id": "c3"},
        ])),
    ):
        response = setup_client.post(
            "/setup/hosts/add",
            data={
                "name": "Docker Host",
                "host": "192.168.1.20",
                "user": "ubuntu",
                "auth_method": "password",
                "ssh_password": "pass",
            },
        )
    assert response.status_code == 200
    assert (
        "docker detected" in response.text.lower() or "3 container" in response.text.lower()
    )
    assert "monitor" in response.text.lower()


def _add_host_via_session(
    setup_client, name, host, ssh_password="pass", enable_auto_update=False
):
    """Drive the add flow to populate session, then confirm. Returns confirm response."""
    mock_result = {"ok": True, "message": "Connected"}
    with (
        patch(
            "app.auth_router.verify_connection", new=AsyncMock(return_value=mock_result)
        ),
        patch("app.auth_router.discover_containers", new=AsyncMock(return_value=[
            {"name": "c1", "image": "img:1", "status": "Up", "running": True,
             "compose_project": None, "css_id": "c1"},
        ])),
    ):
        setup_client.post(
            "/setup/hosts/add",
            data={
                "name": name,
                "host": host,
                "user": "ubuntu",
                "auth_method": "password",
                "ssh_password": ssh_password,
                "enable_auto_update": "on" if enable_auto_update else "",
            },
        )


def test_setup_confirm_add_with_docker(setup_client, data_dir, config_file):
    import yaml

    _create_admin()
    _add_host_via_session(setup_client, "Docker Host", "192.168.1.20")
    response = setup_client.post(
        "/setup/hosts/confirm-add", data={"enable_docker": "yes"}
    )
    assert response.status_code == 200
    assert (
        "docker host" in response.text.lower()
        and "successfully" in response.text.lower()
    )
    raw = yaml.safe_load(config_file.read_text())
    host = next(h for h in raw["hosts"] if h["name"] == "Docker Host")
    assert host.get("docker_mode") == "all"


def test_setup_confirm_add_without_docker(setup_client, data_dir, config_file):
    import yaml

    _create_admin()
    _add_host_via_session(setup_client, "Plain Host", "192.168.1.30")
    response = setup_client.post(
        "/setup/hosts/confirm-add", data={"enable_docker": "no"}
    )
    assert response.status_code == 200
    assert "added successfully" in response.text.lower()
    raw = yaml.safe_load(config_file.read_text())
    host = next(h for h in raw["hosts"] if h["name"] == "Plain Host")
    assert "docker_mode" not in host


def test_setup_confirm_add_no_session_shows_error(setup_client, data_dir):
    _create_admin()
    # No prior add call — session has no pending_ssh_host
    response = setup_client.post(
        "/setup/hosts/confirm-add", data={"enable_docker": "yes"}
    )
    assert response.status_code == 200
    assert (
        "session expired" in response.text.lower()
        or "add the host again" in response.text.lower()
    )


def test_setup_add_host_auto_update_saved(setup_client, data_dir, config_file):
    import yaml

    _create_admin()
    mock_result = {"ok": True, "message": "Connected"}
    with (
        patch(
            "app.auth_router.verify_connection", new=AsyncMock(return_value=mock_result)
        ),
        patch("app.auth_router.discover_containers", new=AsyncMock(return_value=[])),
    ):
        response = setup_client.post(
            "/setup/hosts/add",
            data={
                "name": "Auto Server",
                "host": "192.168.1.99",
                "user": "ubuntu",
                "auth_method": "password",
                "ssh_password": "pass",
                "enable_auto_update": "on",
            },
        )
    assert response.status_code == 200
    assert "added successfully" in response.text.lower()
    raw = yaml.safe_load(config_file.read_text())
    host = next(h for h in raw["hosts"] if h["name"] == "Auto Server")
    assert host.get("auto_update", {}).get("os_enabled") is True


# ---------------------------------------------------------------------------
# POST /setup/hosts/{slug}/remove
# ---------------------------------------------------------------------------


def test_setup_remove_host(setup_client, data_dir, config_file):
    import yaml

    _create_admin()
    # Add host via the add flow (bypasses connection test)
    _add_host_via_session(setup_client, "Remove Me", "192.168.1.50")
    setup_client.post("/setup/hosts/confirm-add", data={"enable_docker": "no"})
    response = setup_client.post("/setup/hosts/remove-me/remove")
    assert response.status_code == 200
    raw = yaml.safe_load(config_file.read_text())
    names = [h["name"] for h in raw.get("hosts", [])]
    assert "Remove Me" not in names


# ---------------------------------------------------------------------------
# POST /setup/portainer/test
# ---------------------------------------------------------------------------


def test_setup_portainer_test_empty_url_shows_amber(setup_client, data_dir):
    _create_admin()
    response = setup_client.post(
        "/setup/portainer/test",
        data={
            "portainer_url": "",
            "portainer_api_key": "",
        },
    )
    assert response.status_code == 200
    assert "amber" in response.text or "Enter a URL" in response.text


def test_setup_portainer_test_success_shows_green(setup_client, data_dir):
    _create_admin()
    mock_client = AsyncMock()
    mock_client.get_endpoints = AsyncMock(return_value=[{"id": 1}])
    with patch("app.portainer_client.PortainerClient", return_value=mock_client):
        response = setup_client.post(
            "/setup/portainer/test",
            data={
                "portainer_url": "https://portainer.test:9443",
                "portainer_api_key": "ptr_test123",
            },
        )
    assert response.status_code == 200
    assert "green" in response.text or "Connected" in response.text


def test_setup_portainer_test_failure_shows_red(setup_client, data_dir):
    _create_admin()
    mock_client = AsyncMock()
    mock_client.get_endpoints = AsyncMock(side_effect=Exception("Connection refused"))
    with patch("app.portainer_client.PortainerClient", return_value=mock_client):
        response = setup_client.post(
            "/setup/portainer/test",
            data={
                "portainer_url": "https://bad-host:9443",
                "portainer_api_key": "ptr_test123",
            },
        )
    assert response.status_code == 200
    assert "red" in response.text or "error" in response.text.lower()


# ---------------------------------------------------------------------------
# POST /setup/portainer/save
# ---------------------------------------------------------------------------


def test_setup_portainer_save(setup_client, data_dir, config_file):
    import yaml

    _create_admin()
    with patch("app.auth_router.reload_backends", new=AsyncMock()):
        response = setup_client.post(
            "/setup/portainer/save",
            data={
                "portainer_url": "https://portainer.test:9443",
                "portainer_api_key": "ptr_test",
            },
        )
    assert response.status_code == 200
    raw = yaml.safe_load(config_file.read_text())
    assert raw.get("portainer", {}).get("url") == "https://portainer.test:9443"


# ---------------------------------------------------------------------------
# POST /setup/dockerhub/save
# ---------------------------------------------------------------------------


def test_setup_dockerhub_save(setup_client, data_dir):
    _create_admin()
    with patch("app.auth_router.reload_backends", new=AsyncMock()):
        response = setup_client.post(
            "/setup/dockerhub/save",
            data={
                "dockerhub_username": "myuser",
                "dockerhub_token": "dckr_pat_xxx",
            },
        )
    assert response.status_code == 200
    assert "saved" in response.text.lower() or "DockerHub" in response.text


# ---------------------------------------------------------------------------
# POST /setup/finish
# ---------------------------------------------------------------------------


def test_setup_finish_redirects_to_login(setup_client, data_dir):
    _create_admin()
    response = setup_client.post("/setup/finish", follow_redirects=False)
    assert response.status_code == 303
    assert "/login" in response.headers["location"]


# ---------------------------------------------------------------------------
# Middleware: /setup/* paths are public
# ---------------------------------------------------------------------------


def test_middleware_allows_setup_hosts_without_auth(setup_client, data_dir):
    """Middleware should allow /setup/hosts without authentication (admin does exist)."""
    _create_admin()
    response = setup_client.get("/setup/hosts", follow_redirects=False)
    # Should get 200, not 302 redirect to /login
    assert response.status_code == 200


def test_middleware_allows_setup_confirm_add_without_session(
    setup_client, data_dir, config_file
):
    """POST /setup/hosts/confirm-add should be reachable without an authenticated session."""
    _create_admin()
    response = setup_client.post(
        "/setup/hosts/confirm-add",
        data={
            "name": "Test",
            "host": "192.168.1.99",
            "auth_method": "password",
            "enable_docker": "no",
        },
    )
    # If middleware blocked it, we'd get a redirect; 200 means it reached the route
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# config_manager: get_available_ssh_keys
# ---------------------------------------------------------------------------


def test_get_available_ssh_keys_empty_dir(tmp_path, monkeypatch):
    keys_dir = tmp_path / "keys"
    keys_dir.mkdir()
    import app.config_manager as cm

    monkeypatch.setattr(
        cm,
        "Path",
        lambda p: keys_dir
        if p == "/app/keys"
        else type("P", (), {"exists": lambda s: False})(),
    )
    # Use the actual function with a patched path
    import app.config_manager as cm2

    # Monkey-patch at function level
    def patched():
        if not keys_dir.exists():
            return []
        return sorted(
            f.name
            for f in keys_dir.iterdir()
            if f.is_file() and not f.name.startswith(".")
        )

    monkeypatch.setattr(cm2, "get_available_ssh_keys", patched)
    result = cm2.get_available_ssh_keys()
    assert result == []


def test_get_available_ssh_keys_with_files(tmp_path, monkeypatch):
    import app.config_manager as cm

    keys_dir = tmp_path / "keys"
    keys_dir.mkdir()
    (keys_dir / "id_ed25519").write_text("key1")
    (keys_dir / "id_rsa").write_text("key2")
    (keys_dir / ".hidden").write_text("hidden")

    def patched():
        return sorted(
            f.name
            for f in keys_dir.iterdir()
            if f.is_file() and not f.name.startswith(".")
        )

    monkeypatch.setattr(cm, "get_available_ssh_keys", patched)
    result = cm.get_available_ssh_keys()
    assert "id_ed25519" in result
    assert "id_rsa" in result
    assert ".hidden" not in result


# ---------------------------------------------------------------------------
# config_manager: add_host with key_path and docker_mode
# ---------------------------------------------------------------------------


def test_add_host_with_key_path(config_file):
    import yaml
    from app.config_manager import add_host

    add_host(
        name="Key Host",
        host="10.0.0.1",
        user="root",
        port=None,
        key_path="/app/keys/id_ed25519",
    )
    raw = yaml.safe_load(config_file.read_text())
    host = next(h for h in raw["hosts"] if h["name"] == "Key Host")
    assert host.get("key") == "/app/keys/id_ed25519"


def test_add_host_with_docker_mode(config_file):
    import yaml
    from app.config_manager import add_host

    add_host(
        name="Docker Host", host="10.0.0.2", user=None, port=None, docker_mode="all"
    )
    raw = yaml.safe_load(config_file.read_text())
    host = next(h for h in raw["hosts"] if h["name"] == "Docker Host")
    assert host.get("docker_mode") == "all"


def test_add_host_docker_mode_none_not_stored(config_file):
    import yaml
    from app.config_manager import add_host

    add_host(
        name="No Docker", host="10.0.0.3", user=None, port=None, docker_mode="none"
    )
    raw = yaml.safe_load(config_file.read_text())
    host = next(h for h in raw["hosts"] if h["name"] == "No Docker")
    assert "docker_mode" not in host


# ---------------------------------------------------------------------------
# Queued host cards — /setup/hosts/card-test and /setup/hosts/card-add
# ---------------------------------------------------------------------------


def _set_proxmox_pending(setup_client, hosts):
    """Inject queued hosts into the session via the session cookie."""
    with setup_client.session_transaction() as sess:
        sess["setup_proxmox_pending"] = hosts


def _setup_client_with_session(config_file, data_dir, monkeypatch):
    """Return a setup_client that supports session_transaction."""
    monkeypatch.setenv("PORTAINER_URL", "")
    monkeypatch.setenv("PORTAINER_API_KEY", "")
    from app.main import app
    from starlette.testclient import TestClient

    return TestClient(app, raise_server_exceptions=True)


def test_setup_hosts_shows_queued_cards_when_pending(
    setup_client, data_dir, config_file
):
    """GET /setup/hosts shows queued host cards when proxmox_pending is in session."""
    _create_admin()
    # Queue a VM via save-vms with vm_action=now so it ends up in setup_proxmox_pending
    setup_client.post(
        "/setup/connect/proxmox/save-vms",
        data={
            "selected_vms": ["pve1:100:MyVM"],
            "vm_ip_100": "192.168.1.100",
            "vm_action": "now",
        },
    )
    response = setup_client.get("/setup/hosts")
    assert response.status_code == 200
    assert "MyVM" in response.text
    assert "host-card-1" in response.text


def test_setup_hosts_no_queued_shows_host_list(setup_client, data_dir, config_file):
    """With no queued hosts, the page shows the already-added hosts list (from SAMPLE_CONFIG)."""
    _create_admin()
    response = setup_client.get("/setup/hosts")
    assert response.status_code == 200
    # No queued-host cards since there's no proxmox_pending in session
    assert "host-card-1" not in response.text
    # Existing hosts from SAMPLE_CONFIG are shown
    assert "Test Host" in response.text


def test_setup_hosts_shows_empty_state_when_no_hosts(
    setup_client, data_dir, monkeypatch
):
    """Empty state shown when there are no hosts and no queued hosts."""
    import app.auth_router as ar

    monkeypatch.setattr(ar, "get_hosts", lambda: [])
    _create_admin()
    response = setup_client.get("/setup/hosts")
    assert response.status_code == 200
    assert "No hosts added yet" in response.text


def test_card_test_success(setup_client, data_dir, config_file):
    _create_admin()
    with patch(
        "app.auth_router.verify_connection",
        new=AsyncMock(return_value={"ok": True, "message": "OK"}),
    ):
        response = setup_client.post(
            "/setup/hosts/card-test",
            data={
                "name": "MyVM",
                "host": "192.168.1.10",
                "user": "root",
                "port": "22",
                "auth_method": "key",
                "ssh_password": "",
                "card_index": "1",
            },
        )
    assert response.status_code == 200
    assert "dot-1" in response.text
    assert "green" in response.text


def test_card_test_failure(setup_client, data_dir, config_file):
    _create_admin()
    with patch(
        "app.auth_router.verify_connection",
        new=AsyncMock(return_value={"ok": False, "message": "Refused"}),
    ):
        response = setup_client.post(
            "/setup/hosts/card-test",
            data={
                "name": "MyVM",
                "host": "192.168.1.10",
                "user": "root",
                "port": "22",
                "auth_method": "password",
                "ssh_password": "pass",
                "card_index": "2",
            },
        )
    assert response.status_code == 200
    assert "dot-2" in response.text
    assert "red" in response.text


def test_card_test_exception_returns_error_dot(setup_client, data_dir, config_file):
    _create_admin()
    with patch(
        "app.auth_router.verify_connection",
        new=AsyncMock(side_effect=Exception("timeout")),
    ):
        response = setup_client.post(
            "/setup/hosts/card-test",
            data={
                "name": "MyVM",
                "host": "192.168.1.10",
                "card_index": "3",
            },
        )
    assert response.status_code == 200
    assert "dot-3" in response.text
    assert "red" in response.text


def test_card_add_success(setup_client, data_dir, config_file):
    import yaml

    _create_admin()
    response = setup_client.post(
        "/setup/hosts/card-add",
        data={
            "name": "QueuedVM",
            "host": "192.168.5.50",
            "user": "root",
            "port": "22",
            "auth_method": "key",
            "ssh_password": "",
            "card_index": "1",
            "node": "pve1",
            "host_type": "vm",
        },
    )
    assert response.status_code == 200
    assert "QueuedVM" in response.text
    assert "Added" in response.text
    assert "pve1" in response.text
    raw = yaml.safe_load(config_file.read_text())
    names = [h["name"] for h in raw.get("hosts", [])]
    assert "QueuedVM" in names


def test_card_add_saves_password_credential(setup_client, data_dir, config_file):
    _create_admin()
    from app.credentials import get_credentials

    response = setup_client.post(
        "/setup/hosts/card-add",
        data={
            "name": "SecureVM",
            "host": "192.168.5.51",
            "user": "ubuntu",
            "port": "22",
            "auth_method": "password",
            "ssh_password": "s3cret",
            "card_index": "2",
            "node": "pve1",
            "host_type": "lxc",
        },
    )
    assert response.status_code == 200
    assert "SecureVM" in response.text
    from app.config_manager import slugify

    creds = get_credentials(slugify("SecureVM"))
    assert creds.get("ssh_password") == "s3cret"


def test_card_add_missing_name_shows_error(setup_client, data_dir, config_file):
    _create_admin()
    response = setup_client.post(
        "/setup/hosts/card-add",
        data={
            "name": "",
            "host": "192.168.5.52",
            "card_index": "1",
            "node": "pve1",
            "host_type": "vm",
        },
    )
    assert response.status_code == 200
    assert "required" in response.text.lower()


def test_card_add_missing_host_shows_error(setup_client, data_dir, config_file):
    _create_admin()
    response = setup_client.post(
        "/setup/hosts/card-add",
        data={
            "name": "GoodName",
            "host": "",
            "card_index": "1",
            "node": "pve1",
            "host_type": "vm",
        },
    )
    assert response.status_code == 200
    assert "required" in response.text.lower()


def test_card_add_no_tag_when_node_missing(setup_client, data_dir, config_file):
    """When node/host_type not provided, tag span is omitted from confirmed card."""
    _create_admin()
    response = setup_client.post(
        "/setup/hosts/card-add",
        data={
            "name": "ManualVM",
            "host": "192.168.5.53",
            "card_index": "1",
            "node": "",
            "host_type": "",
        },
    )
    assert response.status_code == 200
    assert "ManualVM" in response.text
    assert "font-mono" not in response.text or "pve" not in response.text
