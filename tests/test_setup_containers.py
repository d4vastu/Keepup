"""Tests for setup wizard step 7 — container monitoring."""

import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient


def _create_admin():
    from app.auth import create_admin

    return create_admin(username="admin", password="password123", totp_secret=None)


def _setup_client(config_file, data_dir, monkeypatch):
    monkeypatch.setenv("PORTAINER_URL", "")
    monkeypatch.setenv("PORTAINER_API_KEY", "")
    from app.main import app

    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# GET /setup/containers
# ---------------------------------------------------------------------------


def test_setup_containers_no_admin_redirects(config_file, data_dir, monkeypatch):
    client = _setup_client(config_file, data_dir, monkeypatch)
    response = client.get("/setup/containers", follow_redirects=False)
    assert response.status_code == 302
    assert "/setup" in response.headers["location"]


def test_setup_containers_no_hosts_shows_empty_state(
    config_file, data_dir, monkeypatch
):
    client = _setup_client(config_file, data_dir, monkeypatch)
    _create_admin()
    with patch("app.auth_router.discover_containers", new=AsyncMock(return_value=[])):
        response = client.get("/setup/containers")
    assert response.status_code == 200
    assert "Step 7 of 8" in response.text
    assert "Container monitoring" in response.text


def test_setup_containers_with_containers_renders_rows(
    config_file, data_dir, monkeypatch
):
    client = _setup_client(config_file, data_dir, monkeypatch)
    _create_admin()

    # conftest default config already has "Test Host" and "Custom User Host"
    mock_containers = [
        {"id": "plex", "name": "plex", "image": "linuxserver/plex:latest"},
        {"id": "sonarr", "name": "sonarr", "image": "linuxserver/sonarr:latest"},
    ]
    with patch(
        "app.auth_router.discover_containers",
        new=AsyncMock(return_value=mock_containers),
    ):
        response = client.get("/setup/containers")

    assert response.status_code == 200
    assert "plex" in response.text
    assert "sonarr" in response.text
    assert "linuxserver/plex:latest" in response.text
    assert "Select all" in response.text
    assert "Deselect all" in response.text


def test_setup_containers_host_with_no_containers_shows_empty_state(
    config_file, data_dir, monkeypatch
):
    client = _setup_client(config_file, data_dir, monkeypatch)
    _create_admin()

    # All hosts return no containers — should show empty state
    with patch("app.auth_router.discover_containers", new=AsyncMock(return_value=[])):
        response = client.get("/setup/containers")

    assert response.status_code == 200
    assert "No" in response.text and "container" in response.text.lower()


def test_setup_containers_all_checked_by_default(config_file, data_dir, monkeypatch):
    client = _setup_client(config_file, data_dir, monkeypatch)
    _create_admin()

    with patch(
        "app.auth_router.discover_containers",
        new=AsyncMock(
            return_value=[{"id": "nginx", "name": "nginx", "image": "nginx:latest"}]
        ),
    ):
        response = client.get("/setup/containers")

    # The hidden checkbox should be present and checked
    assert 'name="containers"' in response.text
    assert "checked" in response.text


# ---------------------------------------------------------------------------
# POST /setup/containers/save
# ---------------------------------------------------------------------------


def test_setup_containers_save_redirects_to_notifications(
    config_file, data_dir, monkeypatch
):
    client = _setup_client(config_file, data_dir, monkeypatch)
    _create_admin()

    # "Test Host" → slug "test-host" (from conftest default config)
    response = client.post(
        "/setup/containers/save",
        data={"containers": ["test-host:nginx", "test-host:plex"]},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "/setup/notifications" in response.headers["location"]


def test_setup_containers_save_stores_selection(config_file, data_dir, monkeypatch):
    client = _setup_client(config_file, data_dir, monkeypatch)
    _create_admin()

    # "Test Host" → slug "test-host" (from conftest default config)
    client.post(
        "/setup/containers/save",
        data={"containers": ["test-host:nginx", "test-host:plex"]},
        follow_redirects=False,
    )

    import yaml

    raw = yaml.safe_load(config_file.read_text())
    test_host = next(h for h in raw["hosts"] if h["name"] == "Test Host")
    assert test_host.get("docker_mode") == "selected"
    assert "nginx" in test_host.get("docker_containers", [])
    assert "plex" in test_host.get("docker_containers", [])


def test_setup_containers_save_empty_selection(config_file, data_dir, monkeypatch):
    """Empty selection (no containers checked) saves without error."""
    client = _setup_client(config_file, data_dir, monkeypatch)
    _create_admin()

    response = client.post("/setup/containers/save", data={}, follow_redirects=False)
    assert response.status_code == 303
    assert "/setup/notifications" in response.headers["location"]


# ---------------------------------------------------------------------------
# config_manager — save_wizard_container_selection
# ---------------------------------------------------------------------------


def test_save_wizard_container_selection_sets_docker_mode(config_file, data_dir):
    import yaml
    from app.config_manager import save_wizard_container_selection

    # conftest default: "Test Host" → slug "test-host"
    save_wizard_container_selection(["test-host:plex", "test-host:sonarr"])

    raw = yaml.safe_load(config_file.read_text())
    host = next(h for h in raw["hosts"] if h["name"] == "Test Host")
    assert host["docker_mode"] == "selected"
    assert set(host["docker_containers"]) == {"plex", "sonarr"}
    assert "docker_stacks" not in host


def test_save_wizard_container_selection_ignores_malformed(config_file, data_dir):
    from app.config_manager import save_wizard_container_selection

    # Should not raise even if some entries lack ':'
    save_wizard_container_selection(["no-colon-here", "valid:container"])


# ---------------------------------------------------------------------------
# ssh_client — discover_containers
# ---------------------------------------------------------------------------


def _make_conn(stdout=""):
    from unittest.mock import MagicMock

    result = MagicMock()
    result.stdout = stdout
    conn = MagicMock()
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    conn.run = AsyncMock(return_value=result)
    return conn


@pytest.mark.asyncio
async def test_discover_containers_parses_docker_output():
    from app.ssh_client import discover_containers

    stdout = '{"name":"plex","image":"linuxserver/plex:latest"}\n{"name":"nginx","image":"nginx:alpine"}\n'
    conn = _make_conn(stdout=stdout)
    with patch("app.ssh_client.asyncssh.connect", new=AsyncMock(return_value=conn)):
        result = await discover_containers({"name": "test", "host": "1.2.3.4", "user": "root"}, {})

    assert len(result) == 2
    assert result[0]["name"] == "plex"
    assert result[1]["image"] == "nginx:alpine"


@pytest.mark.asyncio
async def test_discover_containers_returns_empty_on_error():
    from app.ssh_client import discover_containers

    with patch(
        "app.ssh_client.asyncssh.connect", side_effect=Exception("connect failed")
    ):
        result = await discover_containers({"name": "test", "host": "1.2.3.4", "user": "root"}, {})

    assert result == []


@pytest.mark.asyncio
async def test_discover_containers_handles_empty_output():
    from app.ssh_client import discover_containers

    conn = _make_conn(stdout="")
    with patch("app.ssh_client.asyncssh.connect", new=AsyncMock(return_value=conn)):
        result = await discover_containers({"name": "test", "host": "1.2.3.4", "user": "root"}, {})

    assert result == []
