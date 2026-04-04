"""Tests for container backend abstraction."""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.backends import ContainerBackend, PortainerBackend, SSHDockerBackend
from app.backends.ssh_docker_backend import _parse_json_output, _rollup_status


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

def test_portainer_backend_satisfies_protocol():
    mock_client = MagicMock()
    b = PortainerBackend(mock_client)
    assert isinstance(b, ContainerBackend)
    assert b.BACKEND_KEY == "portainer"


def test_ssh_docker_backend_satisfies_protocol():
    b = SSHDockerBackend()
    assert isinstance(b, ContainerBackend)
    assert b.BACKEND_KEY == "ssh"


# ---------------------------------------------------------------------------
# PortainerBackend
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_portainer_backend_enriches_update_path():
    raw = [{"id": 10, "name": "sonarr", "endpoint_id": 1,
            "endpoint_name": "primary", "update_status": "up_to_date", "images": []}]
    mock_client = MagicMock()
    mock_client.get_stacks_with_update_status = AsyncMock(return_value=raw)
    backend = PortainerBackend(mock_client)

    stacks = await backend.get_stacks_with_update_status()

    assert len(stacks) == 1
    assert stacks[0]["update_path"] == "portainer/10:1"
    assert stacks[0]["id"] == "10"
    assert stacks[0]["endpoint_id"] == "1"


@pytest.mark.asyncio
async def test_portainer_backend_update_stack_decodes_ref():
    mock_client = MagicMock()
    mock_client.update_stack = AsyncMock(return_value={})
    backend = PortainerBackend(mock_client)

    await backend.update_stack("42:3")

    mock_client.update_stack.assert_called_once_with(42, 3)


# ---------------------------------------------------------------------------
# _parse_json_output helper
# ---------------------------------------------------------------------------

def test_parse_json_output_array():
    text = json.dumps([{"Name": "sonarr"}, {"Name": "radarr"}])
    result = _parse_json_output(text)
    assert len(result) == 2
    assert result[0]["Name"] == "sonarr"


def test_parse_json_output_ndjson():
    text = '{"Name": "sonarr"}\n{"Name": "radarr"}\n'
    result = _parse_json_output(text)
    assert len(result) == 2


def test_parse_json_output_empty():
    assert _parse_json_output("") == []
    assert _parse_json_output("   ") == []


def test_parse_json_output_single_object():
    text = json.dumps({"Name": "sonarr"})
    result = _parse_json_output(text)
    assert result == [{"Name": "sonarr"}]


# ---------------------------------------------------------------------------
# _rollup_status helper
# ---------------------------------------------------------------------------

def test_rollup_all_up_to_date():
    images = [{"name": "img1", "status": "up_to_date"}, {"name": "img2", "status": "up_to_date"}]
    assert _rollup_status(images) == "up_to_date"


def test_rollup_all_update_available():
    images = [{"name": "img1", "status": "update_available"}]
    assert _rollup_status(images) == "update_available"


def test_rollup_mixed():
    images = [{"name": "img1", "status": "update_available"}, {"name": "img2", "status": "up_to_date"}]
    assert _rollup_status(images) == "mixed"


def test_rollup_empty():
    assert _rollup_status([]) == "unknown"


def test_rollup_all_unknown():
    images = [{"name": "img1", "status": "unknown"}]
    assert _rollup_status(images) == "unknown"


# ---------------------------------------------------------------------------
# SSHDockerBackend — ref encoding
# ---------------------------------------------------------------------------

def test_ssh_backend_ref_roundtrip():
    b = SSHDockerBackend()
    ref = b._make_ref("my-server", "sonarr")
    slug, name = b._parse_ref(ref)
    assert slug == "my-server"
    assert name == "sonarr"


def test_ssh_backend_ref_encodes_special_chars():
    b = SSHDockerBackend()
    ref = b._make_ref("my-server", "my stack/v2")
    slug, name = b._parse_ref(ref)
    assert slug == "my-server"
    assert name == "my stack/v2"


# ---------------------------------------------------------------------------
# SSHDockerBackend — discover_stacks
# ---------------------------------------------------------------------------

def _make_ssh_conn(stdout: str = "", returncode: int = 0) -> MagicMock:
    result = MagicMock(stdout=stdout, returncode=returncode)
    conn = MagicMock()
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    conn.run = AsyncMock(return_value=result)
    return conn


@pytest.mark.asyncio
async def test_discover_stacks_returns_projects(config_file, data_dir):
    output = json.dumps([
        {"Name": "sonarr", "Status": "running(1)", "ConfigFiles": "/opt/stacks/sonarr/docker-compose.yml"},
        {"Name": "radarr", "Status": "running(1)", "ConfigFiles": "/opt/stacks/radarr/docker-compose.yml"},
    ])
    conn = _make_ssh_conn(stdout=output)
    host = {"name": "Test Host", "host": "192.168.1.10", "slug": "test-host"}

    with patch("app.backends.ssh_docker_backend._connect", new=AsyncMock(return_value=conn)):
        backend = SSHDockerBackend()
        stacks = await backend.discover_stacks(host)

    assert len(stacks) == 2
    assert stacks[0]["name"] == "sonarr"
    assert stacks[1]["name"] == "radarr"


@pytest.mark.asyncio
async def test_discover_stacks_empty_returns_empty(config_file, data_dir):
    conn = _make_ssh_conn(stdout="[]")
    host = {"name": "Test Host", "host": "192.168.1.10", "slug": "test-host"}

    with patch("app.backends.ssh_docker_backend._connect", new=AsyncMock(return_value=conn)):
        backend = SSHDockerBackend()
        stacks = await backend.discover_stacks(host)

    assert stacks == []


@pytest.mark.asyncio
async def test_discover_stacks_ssh_error_returns_empty(config_file, data_dir):
    host = {"name": "Test Host", "host": "192.168.1.10", "slug": "test-host"}

    with patch("app.backends.ssh_docker_backend._connect", new=AsyncMock(side_effect=Exception("SSH failed"))):
        backend = SSHDockerBackend()
        stacks = await backend.discover_stacks(host)

    assert stacks == []


# ---------------------------------------------------------------------------
# SSHDockerBackend — update_stack
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_update_stack_unknown_host_raises(config_file, data_dir):
    backend = SSHDockerBackend()
    # No docker_mode hosts in config → _docker_hosts() returns []
    with pytest.raises(ValueError, match="my-server"):
        await backend.update_stack("my-server/sonarr")


@pytest.mark.asyncio
async def test_update_stack_runs_pull_and_up(config_file, data_dir, monkeypatch):
    import app.config_manager as cm
    import yaml

    # Add docker_mode to the test host
    raw = yaml.safe_load(config_file.read_text())
    raw["hosts"][0]["docker_mode"] = "all"
    config_file.write_text(yaml.dump(raw))

    ls_output = json.dumps([{"Name": "sonarr", "ConfigFiles": "/opt/sonarr/docker-compose.yml"}])
    pull_result = MagicMock(stdout="Pulled", returncode=0)
    up_result = MagicMock(stdout="Started", returncode=0)

    conn = MagicMock()
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    conn.run = AsyncMock(side_effect=[
        MagicMock(stdout=ls_output, returncode=0),  # ls for config file
        pull_result,
        up_result,
    ])

    with patch("app.backends.ssh_docker_backend._connect", new=AsyncMock(return_value=conn)):
        backend = SSHDockerBackend()
        await backend.update_stack("test-host/sonarr")

    calls = [c.args[0] for c in conn.run.call_args_list]
    assert any("pull" in c for c in calls)
    assert any("up -d" in c for c in calls)


# ---------------------------------------------------------------------------
# Admin routes — docker discovery
# ---------------------------------------------------------------------------

def test_docker_discover_no_stacks_returns_empty(client):
    with patch("app.backends.ssh_docker_backend.SSHDockerBackend.discover_stacks",
               new=AsyncMock(return_value=[])):
        response = client.get("/admin/hosts/test-host/docker-discover")
    assert response.status_code == 200
    assert response.text.strip() == ""


def test_docker_discover_returns_prompt_when_stacks_found(client):
    stacks = [
        {"name": "sonarr", "config_file": "/opt/stacks/sonarr/docker-compose.yml"},
        {"name": "radarr", "config_file": "/opt/stacks/radarr/docker-compose.yml"},
    ]
    with patch("app.backends.ssh_docker_backend.SSHDockerBackend.discover_stacks",
               new=AsyncMock(return_value=stacks)):
        response = client.get("/admin/hosts/test-host/docker-discover")
    assert response.status_code == 200
    assert "sonarr" in response.text
    assert "radarr" in response.text
    assert "monitor" in response.text.lower()


def test_docker_discover_unknown_host_returns_empty(client):
    response = client.get("/admin/hosts/does-not-exist/docker-discover")
    assert response.status_code == 200
    assert response.text.strip() == ""


def test_docker_monitoring_save_all(client, config_file):
    import yaml
    response = client.post("/admin/hosts/test-host/docker-monitoring",
                           data={"docker_mode": "all"})
    assert response.status_code == 200
    raw = yaml.safe_load(config_file.read_text())
    host = next(h for h in raw["hosts"] if h["name"] == "Test Host")
    assert host["docker_mode"] == "all"


def test_docker_monitoring_save_selected(client, config_file):
    import yaml
    response = client.post("/admin/hosts/test-host/docker-monitoring",
                           data={"docker_mode": "selected", "docker_stacks": ["sonarr", "radarr"]})
    assert response.status_code == 200
    raw = yaml.safe_load(config_file.read_text())
    host = next(h for h in raw["hosts"] if h["name"] == "Test Host")
    assert host["docker_mode"] == "selected"
    assert set(host["docker_stacks"]) == {"sonarr", "radarr"}


def test_docker_monitoring_save_none_clears(client, config_file):
    import yaml
    # First set it
    client.post("/admin/hosts/test-host/docker-monitoring", data={"docker_mode": "all"})
    # Then clear it
    response = client.post("/admin/hosts/test-host/docker-monitoring", data={"docker_mode": "none"})
    assert response.status_code == 200
    raw = yaml.safe_load(config_file.read_text())
    host = next(h for h in raw["hosts"] if h["name"] == "Test Host")
    assert "docker_mode" not in host


def test_docker_prompt_dismiss(client):
    response = client.delete("/admin/hosts/test-host/docker-prompt")
    assert response.status_code == 200
    assert response.text.strip() == ""


# ---------------------------------------------------------------------------
# SSHDockerBackend — get_stacks_with_update_status (integration)
# ---------------------------------------------------------------------------

def _make_multi_conn(responses: list) -> MagicMock:
    """Connection whose .run() returns successive values."""
    conn = MagicMock()
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    conn.run = AsyncMock(side_effect=responses)
    return conn


@pytest.mark.asyncio
async def test_get_stacks_with_update_status_all_mode(config_file, data_dir):
    import yaml
    raw = yaml.safe_load(config_file.read_text())
    raw["hosts"][0]["docker_mode"] = "all"
    config_file.write_text(yaml.dump(raw))

    ls_output = json.dumps([{"Name": "sonarr", "ConfigFiles": "/opt/sonarr/docker-compose.yml"}])
    ps_output = json.dumps([{"Image": "sonarr:latest"}])
    inspect_output = '["sonarr@sha256:abc123"]'

    conn = _make_multi_conn([
        MagicMock(stdout=ls_output, returncode=0),   # compose ls
        MagicMock(stdout=ps_output, returncode=0),   # compose ps
        MagicMock(stdout=inspect_output, returncode=0),  # image inspect
    ])

    with patch("app.backends.ssh_docker_backend._connect", new=AsyncMock(return_value=conn)), \
         patch("app.backends.ssh_docker_backend.check_image_update", new=AsyncMock(return_value="up_to_date")):
        backend = SSHDockerBackend()
        stacks = await backend.get_stacks_with_update_status()

    assert len(stacks) == 1
    assert stacks[0]["name"] == "sonarr"
    assert stacks[0]["update_status"] == "up_to_date"
    assert stacks[0]["update_path"].startswith("ssh/test-host/")


@pytest.mark.asyncio
async def test_get_stacks_filters_by_selected_mode(config_file, data_dir):
    import yaml
    raw = yaml.safe_load(config_file.read_text())
    raw["hosts"][0]["docker_mode"] = "selected"
    raw["hosts"][0]["docker_stacks"] = ["radarr"]
    config_file.write_text(yaml.dump(raw))

    ls_output = json.dumps([
        {"Name": "sonarr", "ConfigFiles": "/opt/sonarr/dc.yml"},
        {"Name": "radarr", "ConfigFiles": "/opt/radarr/dc.yml"},
    ])
    ps_output = json.dumps([{"Image": "radarr:latest"}])
    inspect_output = '["radarr@sha256:abc"]'

    conn = _make_multi_conn([
        MagicMock(stdout=ls_output, returncode=0),
        MagicMock(stdout=ps_output, returncode=0),
        MagicMock(stdout=inspect_output, returncode=0),
    ])

    with patch("app.backends.ssh_docker_backend._connect", new=AsyncMock(return_value=conn)), \
         patch("app.backends.ssh_docker_backend.check_image_update", new=AsyncMock(return_value="up_to_date")):
        backend = SSHDockerBackend()
        stacks = await backend.get_stacks_with_update_status()

    assert len(stacks) == 1
    assert stacks[0]["name"] == "radarr"


@pytest.mark.asyncio
async def test_get_stacks_no_docker_hosts_returns_empty(config_file, data_dir):
    # No docker_mode on any host
    backend = SSHDockerBackend()
    stacks = await backend.get_stacks_with_update_status()
    assert stacks == []


@pytest.mark.asyncio
async def test_update_stack_pull_failure_raises(config_file, data_dir):
    import yaml
    raw = yaml.safe_load(config_file.read_text())
    raw["hosts"][0]["docker_mode"] = "all"
    config_file.write_text(yaml.dump(raw))

    ls_output = json.dumps([{"Name": "sonarr", "ConfigFiles": "/opt/sonarr/dc.yml"}])
    conn = _make_multi_conn([
        MagicMock(stdout=ls_output, returncode=0),
        MagicMock(stdout="error output", returncode=1),  # pull fails
    ])

    with patch("app.backends.ssh_docker_backend._connect", new=AsyncMock(return_value=conn)):
        backend = SSHDockerBackend()
        with pytest.raises(RuntimeError, match="pull failed"):
            await backend.update_stack("test-host/sonarr")


@pytest.mark.asyncio
async def test_update_stack_up_failure_raises(config_file, data_dir):
    import yaml
    raw = yaml.safe_load(config_file.read_text())
    raw["hosts"][0]["docker_mode"] = "all"
    config_file.write_text(yaml.dump(raw))

    ls_output = json.dumps([{"Name": "sonarr", "ConfigFiles": "/opt/sonarr/dc.yml"}])
    conn = _make_multi_conn([
        MagicMock(stdout=ls_output, returncode=0),
        MagicMock(stdout="Pulled", returncode=0),     # pull succeeds
        MagicMock(stdout="error output", returncode=1),  # up fails
    ])

    with patch("app.backends.ssh_docker_backend._connect", new=AsyncMock(return_value=conn)):
        backend = SSHDockerBackend()
        with pytest.raises(RuntimeError, match="up -d failed"):
            await backend.update_stack("test-host/sonarr")
