"""Tests for container backend abstraction."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.backends import ContainerBackend, PortainerBackend, SSHDockerBackend
from app.backends.ssh_docker_backend import (
    _build_docker_run_cmd,
    _parse_docker_ps_labels,
    _parse_json_output,
    _rollup_status,
)


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
    raw = [
        {
            "id": 10,
            "name": "sonarr",
            "endpoint_id": 1,
            "endpoint_name": "primary",
            "update_status": "up_to_date",
            "images": [],
        }
    ]
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
    images = [
        {"name": "img1", "status": "up_to_date"},
        {"name": "img2", "status": "up_to_date"},
    ]
    assert _rollup_status(images) == "up_to_date"


def test_rollup_all_update_available():
    images = [{"name": "img1", "status": "update_available"}]
    assert _rollup_status(images) == "update_available"


def test_rollup_mixed():
    images = [
        {"name": "img1", "status": "update_available"},
        {"name": "img2", "status": "up_to_date"},
    ]
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
    output = json.dumps(
        [
            {
                "Name": "sonarr",
                "Status": "running(1)",
                "ConfigFiles": "/opt/stacks/sonarr/docker-compose.yml",
            },
            {
                "Name": "radarr",
                "Status": "running(1)",
                "ConfigFiles": "/opt/stacks/radarr/docker-compose.yml",
            },
        ]
    )
    conn = _make_ssh_conn(stdout=output)
    host = {"name": "Test Host", "host": "192.168.1.10", "slug": "test-host"}

    with patch(
        "app.backends.ssh_docker_backend._connect", new=AsyncMock(return_value=conn)
    ):
        backend = SSHDockerBackend()
        stacks = await backend.discover_stacks(host)

    assert len(stacks) == 2
    assert stacks[0]["name"] == "sonarr"
    assert stacks[1]["name"] == "radarr"


@pytest.mark.asyncio
async def test_discover_stacks_empty_returns_empty(config_file, data_dir):
    conn = _make_ssh_conn(stdout="[]")
    host = {"name": "Test Host", "host": "192.168.1.10", "slug": "test-host"}

    with patch(
        "app.backends.ssh_docker_backend._connect", new=AsyncMock(return_value=conn)
    ):
        backend = SSHDockerBackend()
        stacks = await backend.discover_stacks(host)

    assert stacks == []


@pytest.mark.asyncio
async def test_discover_stacks_ssh_error_returns_empty(config_file, data_dir):
    host = {"name": "Test Host", "host": "192.168.1.10", "slug": "test-host"}

    with patch(
        "app.backends.ssh_docker_backend._connect",
        new=AsyncMock(side_effect=Exception("SSH failed")),
    ):
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
    import yaml

    # Add docker_mode to the test host
    raw = yaml.safe_load(config_file.read_text())
    raw["hosts"][0]["docker_mode"] = "all"
    config_file.write_text(yaml.dump(raw))

    ls_output = json.dumps(
        [{"Name": "sonarr", "ConfigFiles": "/opt/sonarr/docker-compose.yml"}]
    )
    pull_result = MagicMock(stdout="Pulled", returncode=0)
    up_result = MagicMock(stdout="Started", returncode=0)

    conn = MagicMock()
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    conn.run = AsyncMock(
        side_effect=[
            MagicMock(stdout=ls_output, returncode=0),  # ls for config file
            pull_result,
            up_result,
        ]
    )

    with patch(
        "app.backends.ssh_docker_backend._connect", new=AsyncMock(return_value=conn)
    ):
        backend = SSHDockerBackend()
        await backend.update_stack("test-host/sonarr")

    calls = [c.args[0] for c in conn.run.call_args_list]
    assert any("pull" in c for c in calls)
    assert any("up -d" in c for c in calls)


# ---------------------------------------------------------------------------
# Admin routes — docker discovery
# ---------------------------------------------------------------------------


def test_docker_discover_no_stacks_returns_empty(client):
    with patch(
        "app.backends.ssh_docker_backend.SSHDockerBackend.discover_stacks",
        new=AsyncMock(return_value=[]),
    ):
        response = client.get("/admin/hosts/test-host/docker-discover")
    assert response.status_code == 200
    assert response.text.strip() == ""


def test_docker_discover_returns_prompt_when_stacks_found(client):
    stacks = [
        {"name": "sonarr", "config_file": "/opt/stacks/sonarr/docker-compose.yml"},
        {"name": "radarr", "config_file": "/opt/stacks/radarr/docker-compose.yml"},
    ]
    with patch(
        "app.backends.ssh_docker_backend.SSHDockerBackend.discover_stacks",
        new=AsyncMock(return_value=stacks),
    ):
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

    response = client.post(
        "/admin/hosts/test-host/docker-monitoring", data={"docker_mode": "all"}
    )
    assert response.status_code == 200
    raw = yaml.safe_load(config_file.read_text())
    host = next(h for h in raw["hosts"] if h["name"] == "Test Host")
    assert host["docker_mode"] == "all"


def test_docker_monitoring_save_selected(client, config_file):
    import yaml

    response = client.post(
        "/admin/hosts/test-host/docker-monitoring",
        data={"docker_mode": "selected", "docker_stacks": ["sonarr", "radarr"]},
    )
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
    response = client.post(
        "/admin/hosts/test-host/docker-monitoring", data={"docker_mode": "none"}
    )
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

    # New flow: docker ps -a (one entry per container), then image inspect per container
    docker_ps_output = json.dumps(
        {"Names": "/sonarr", "Image": "sonarr:latest",
         "Labels": "com.docker.compose.project=sonarr"}
    )
    inspect_output = '["sonarr@sha256:abc123"]'

    conn = _make_multi_conn(
        [
            MagicMock(stdout=docker_ps_output, returncode=0),  # docker ps -a
            MagicMock(stdout=inspect_output, returncode=0),    # image inspect
        ]
    )

    with (
        patch(
            "app.backends.ssh_docker_backend._connect", new=AsyncMock(return_value=conn)
        ),
        patch(
            "app.backends.ssh_docker_backend.check_image_update",
            new=AsyncMock(return_value="up_to_date"),
        ),
    ):
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

    # docker ps -a returns both containers; only radarr passes the project filter
    docker_ps_output = "\n".join([
        json.dumps({"Names": "/sonarr", "Image": "sonarr:latest",
                    "Labels": "com.docker.compose.project=sonarr"}),
        json.dumps({"Names": "/radarr", "Image": "radarr:latest",
                    "Labels": "com.docker.compose.project=radarr"}),
    ])
    inspect_output = '["radarr@sha256:abc"]'

    conn = _make_multi_conn(
        [
            MagicMock(stdout=docker_ps_output, returncode=0),  # docker ps -a
            MagicMock(stdout=inspect_output, returncode=0),    # image inspect (radarr only)
        ]
    )

    with (
        patch(
            "app.backends.ssh_docker_backend._connect", new=AsyncMock(return_value=conn)
        ),
        patch(
            "app.backends.ssh_docker_backend.check_image_update",
            new=AsyncMock(return_value="up_to_date"),
        ),
    ):
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
    conn = _make_multi_conn(
        [
            MagicMock(stdout=ls_output, returncode=0),
            MagicMock(stdout="error output", returncode=1),  # pull fails
        ]
    )

    with patch(
        "app.backends.ssh_docker_backend._connect", new=AsyncMock(return_value=conn)
    ):
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
    conn = _make_multi_conn(
        [
            MagicMock(stdout=ls_output, returncode=0),
            MagicMock(stdout="Pulled", returncode=0),  # pull succeeds
            MagicMock(stdout="error output", returncode=1),  # up fails
        ]
    )

    with patch(
        "app.backends.ssh_docker_backend._connect", new=AsyncMock(return_value=conn)
    ):
        backend = SSHDockerBackend()
        with pytest.raises(RuntimeError, match="up -d failed"):
            await backend.update_stack("test-host/sonarr")


# ---------------------------------------------------------------------------
# _parse_docker_ps_labels
# ---------------------------------------------------------------------------


def test_parse_docker_ps_labels_basic():
    labels = _parse_docker_ps_labels(
        "com.docker.compose.project=sonarr,com.docker.compose.service=sonarr"
    )
    assert labels["com.docker.compose.project"] == "sonarr"
    assert labels["com.docker.compose.service"] == "sonarr"


def test_parse_docker_ps_labels_empty():
    assert _parse_docker_ps_labels("") == {}
    assert _parse_docker_ps_labels(None) == {}


# ---------------------------------------------------------------------------
# _build_docker_run_cmd
# ---------------------------------------------------------------------------


def _minimal_inspect(name="myapp", image="myimage:latest") -> dict:
    return {
        "Id": "abc123def456",
        "Name": f"/{name}",
        "Config": {
            "Image": image,
            "Hostname": name,
            "Env": [],
            "Cmd": None,
            "Entrypoint": None,
            "Labels": {},
        },
        "HostConfig": {
            "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
            "NetworkMode": "bridge",
            "Privileged": False,
            "CapAdd": None,
            "CapDrop": None,
            "Binds": None,
            "PortBindings": {},
            "Devices": None,
            "Dns": None,
            "ExtraHosts": None,
            "Tmpfs": None,
            "PidMode": "",
            "IpcMode": "private",
            "LogConfig": {"Type": "json-file", "Config": {}},
        },
        "NetworkSettings": {"Networks": {}},
    }


def test_build_docker_run_cmd_minimal():
    cmd = _build_docker_run_cmd(_minimal_inspect())
    assert "docker run" in cmd
    assert "-d" in cmd
    assert "--name myapp" in cmd
    assert "--restart unless-stopped" in cmd
    assert "myimage:latest" in cmd


def test_build_docker_run_cmd_ports():
    data = _minimal_inspect()
    data["HostConfig"]["PortBindings"] = {
        "8080/tcp": [{"HostIp": "", "HostPort": "9090"}]
    }
    cmd = _build_docker_run_cmd(data)
    assert "-p 9090:8080/tcp" in cmd


def test_build_docker_run_cmd_volumes():
    data = _minimal_inspect()
    data["HostConfig"]["Binds"] = ["/host/data:/container/data:rw"]
    cmd = _build_docker_run_cmd(data)
    assert "-v /host/data:/container/data:rw" in cmd


def test_build_docker_run_cmd_env():
    data = _minimal_inspect()
    data["Config"]["Env"] = ["FOO=bar", "BAZ=qux"]
    cmd = _build_docker_run_cmd(data)
    assert "-e FOO=bar" in cmd
    assert "-e BAZ=qux" in cmd


def test_build_docker_run_cmd_privileged():
    data = _minimal_inspect()
    data["HostConfig"]["Privileged"] = True
    cmd = _build_docker_run_cmd(data)
    assert "--privileged" in cmd


def test_build_docker_run_cmd_skips_compose_labels():
    data = _minimal_inspect()
    data["Config"]["Labels"] = {
        "com.docker.compose.project": "myproject",
        "my.custom.label": "keep-this",
    }
    cmd = _build_docker_run_cmd(data)
    assert "com.docker.compose.project" not in cmd
    assert "keep-this" in cmd


def test_build_docker_run_cmd_host_ip_binding():
    data = _minimal_inspect()
    data["HostConfig"]["PortBindings"] = {
        "80/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8080"}]
    }
    cmd = _build_docker_run_cmd(data)
    assert "127.0.0.1:8080:80/tcp" in cmd


def test_build_docker_run_cmd_no_restart():
    data = _minimal_inspect()
    data["HostConfig"]["RestartPolicy"] = {"Name": "no", "MaximumRetryCount": 0}
    cmd = _build_docker_run_cmd(data)
    assert "--restart" not in cmd


def test_build_docker_run_cmd_on_failure_with_retries():
    data = _minimal_inspect()
    data["HostConfig"]["RestartPolicy"] = {"Name": "on-failure", "MaximumRetryCount": 3}
    cmd = _build_docker_run_cmd(data)
    assert "--restart on-failure:3" in cmd


# ---------------------------------------------------------------------------
# SSHDockerBackend — standalone container discovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_standalone_containers_appear_in_all_mode(config_file, data_dir):
    """Containers without a compose project label show up as individual entries."""
    import yaml

    raw = yaml.safe_load(config_file.read_text())
    raw["hosts"][0]["docker_mode"] = "all"
    config_file.write_text(yaml.dump(raw))

    docker_ps_output = "\n".join([
        json.dumps({"Names": "/compose-app", "Image": "compose-app:latest",
                    "Labels": "com.docker.compose.project=mystack"}),
        json.dumps({"Names": "/standalone-app", "Image": "standalone:latest",
                    "Labels": ""}),
    ])
    inspect_compose = '["compose-app@sha256:aaa"]'
    inspect_standalone = '["standalone@sha256:bbb"]'

    conn = _make_multi_conn(
        [
            MagicMock(stdout=docker_ps_output, returncode=0),    # docker ps -a
            MagicMock(stdout=inspect_compose, returncode=0),     # image inspect compose-app
            MagicMock(stdout=inspect_standalone, returncode=0),  # image inspect standalone
        ]
    )

    with (
        patch("app.backends.ssh_docker_backend._connect", new=AsyncMock(return_value=conn)),
        patch("app.backends.ssh_docker_backend.check_image_update",
              new=AsyncMock(return_value="up_to_date")),
    ):
        backend = SSHDockerBackend()
        stacks = await backend.get_stacks_with_update_status()

    assert len(stacks) == 2
    names = {s["name"] for s in stacks}
    assert "compose-app" in names
    assert "standalone-app" in names
    # Compose container ref uses project:container format
    compose_entry = next(s for s in stacks if s["name"] == "compose-app")
    assert "mystack:compose-app" in compose_entry["update_path"]
    # Standalone uses ~ prefix
    standalone_entry = next(s for s in stacks if s["name"] == "standalone-app")
    assert "/~" in standalone_entry["update_path"]


@pytest.mark.asyncio
async def test_selected_mode_excludes_standalone_containers(config_file, data_dir):
    """In selected mode, containers without a matching compose project are excluded."""
    import yaml

    raw = yaml.safe_load(config_file.read_text())
    raw["hosts"][0]["docker_mode"] = "selected"
    raw["hosts"][0]["docker_stacks"] = ["allowed-stack"]
    config_file.write_text(yaml.dump(raw))

    docker_ps_output = "\n".join([
        json.dumps({"Names": "/in-stack", "Image": "in-stack:latest",
                    "Labels": "com.docker.compose.project=allowed-stack"}),
        json.dumps({"Names": "/standalone", "Image": "standalone:latest",
                    "Labels": ""}),
        json.dumps({"Names": "/other-stack", "Image": "other:latest",
                    "Labels": "com.docker.compose.project=other-stack"}),
    ])
    inspect_output = '["in-stack@sha256:aaa"]'

    conn = _make_multi_conn(
        [
            MagicMock(stdout=docker_ps_output, returncode=0),
            MagicMock(stdout=inspect_output, returncode=0),
        ]
    )

    with (
        patch("app.backends.ssh_docker_backend._connect", new=AsyncMock(return_value=conn)),
        patch("app.backends.ssh_docker_backend.check_image_update",
              new=AsyncMock(return_value="up_to_date")),
    ):
        backend = SSHDockerBackend()
        stacks = await backend.get_stacks_with_update_status()

    assert len(stacks) == 1
    assert stacks[0]["name"] == "in-stack"


# ---------------------------------------------------------------------------
# SSHDockerBackend — update routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_compose_ref_triggers_compose_update(config_file, data_dir):
    """A ref with project:container format runs docker compose pull + up."""
    import yaml

    raw = yaml.safe_load(config_file.read_text())
    raw["hosts"][0]["docker_mode"] = "all"
    config_file.write_text(yaml.dump(raw))

    ls_output = json.dumps([{"Name": "mystack", "ConfigFiles": "/opt/mystack/dc.yml"}])
    conn = _make_multi_conn(
        [
            MagicMock(stdout=ls_output, returncode=0),     # compose ls (get config file)
            MagicMock(stdout="Pulled", returncode=0),      # compose pull
            MagicMock(stdout="Started", returncode=0),     # compose up -d
        ]
    )

    with patch("app.backends.ssh_docker_backend._connect", new=AsyncMock(return_value=conn)):
        backend = SSHDockerBackend()
        # ref in compose format: slug/project:container
        await backend.update_stack("test-host/mystack:myapp")

    calls = [call.args[0] for call in conn.run.call_args_list]
    assert any("compose" in c and "pull" in c for c in calls)
    assert any("compose" in c and "up" in c and "-d" in c for c in calls)


@pytest.mark.asyncio
async def test_update_standalone_ref_triggers_recreate(config_file, data_dir):
    """A ref with ~ prefix pulls image and recreates the container."""
    import yaml

    raw = yaml.safe_load(config_file.read_text())
    raw["hosts"][0]["docker_mode"] = "all"
    config_file.write_text(yaml.dump(raw))

    inspect_data = json.dumps([{
        "Name": "/myapp",
        "Id": "abc123",
        "Config": {"Image": "myimage:latest", "Env": [], "Cmd": None,
                   "Entrypoint": None, "Labels": {}, "Hostname": "myapp"},
        "HostConfig": {
            "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
            "NetworkMode": "bridge", "Privileged": False,
            "Binds": None, "PortBindings": {}, "Devices": None,
            "CapAdd": None, "CapDrop": None, "Dns": None,
            "ExtraHosts": None, "Tmpfs": None, "PidMode": "",
            "IpcMode": "private", "LogConfig": {"Type": "json-file", "Config": {}},
        },
        "NetworkSettings": {"Networks": {}},
    }])

    conn = _make_multi_conn(
        [
            MagicMock(stdout=inspect_data, returncode=0),    # docker inspect
            MagicMock(stdout="Pulled", returncode=0),        # docker pull
            MagicMock(stdout="", returncode=0),              # docker stop
            MagicMock(stdout="", returncode=0),              # docker rm
            MagicMock(stdout="new-container-id", returncode=0),  # docker run
        ]
    )

    with patch("app.backends.ssh_docker_backend._connect", new=AsyncMock(return_value=conn)):
        backend = SSHDockerBackend()
        await backend.update_stack("test-host/~myapp")

    calls = [call.args[0] for call in conn.run.call_args_list]
    assert any("docker pull" in c for c in calls)
    assert any("docker stop" in c for c in calls)
    assert any("docker rm" in c for c in calls)
    assert any("docker run" in c and "-d" in c for c in calls)


@pytest.mark.asyncio
async def test_update_standalone_pull_failure_raises(config_file, data_dir):
    import yaml

    raw = yaml.safe_load(config_file.read_text())
    raw["hosts"][0]["docker_mode"] = "all"
    config_file.write_text(yaml.dump(raw))

    inspect_data = json.dumps([{
        "Name": "/myapp", "Id": "abc123",
        "Config": {"Image": "myimage:latest", "Env": [], "Cmd": None,
                   "Entrypoint": None, "Labels": {}, "Hostname": "myapp"},
        "HostConfig": {
            "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
            "NetworkMode": "bridge", "Privileged": False,
            "Binds": None, "PortBindings": {}, "Devices": None,
            "CapAdd": None, "CapDrop": None, "Dns": None,
            "ExtraHosts": None, "Tmpfs": None, "PidMode": "",
            "IpcMode": "private", "LogConfig": {"Type": "json-file", "Config": {}},
        },
        "NetworkSettings": {"Networks": {}},
    }])

    conn = _make_multi_conn(
        [
            MagicMock(stdout=inspect_data, returncode=0),        # docker inspect
            MagicMock(stdout="pull error", returncode=1),        # docker pull fails
        ]
    )

    with patch("app.backends.ssh_docker_backend._connect", new=AsyncMock(return_value=conn)):
        backend = SSHDockerBackend()
        with pytest.raises(RuntimeError, match="pull failed"):
            await backend.update_stack("test-host/~myapp")


# ---------------------------------------------------------------------------
# _build_docker_run_cmd — branch coverage
# ---------------------------------------------------------------------------


def test_build_docker_run_cmd_custom_network_mode():
    data = _minimal_inspect()
    data["HostConfig"]["NetworkMode"] = "host"
    cmd = _build_docker_run_cmd(data)
    assert "--network host" in cmd


def test_build_docker_run_cmd_custom_network_from_settings():
    data = _minimal_inspect()
    data["HostConfig"]["NetworkMode"] = "bridge"
    data["NetworkSettings"]["Networks"] = {"my-custom-net": {}}
    cmd = _build_docker_run_cmd(data)
    assert "--network my-custom-net" in cmd


def test_build_docker_run_cmd_hostname_set():
    data = _minimal_inspect(name="myapp")
    data["Config"]["Hostname"] = "custom-hostname"
    cmd = _build_docker_run_cmd(data)
    assert "--hostname custom-hostname" in cmd


def test_build_docker_run_cmd_cap_add_drop():
    data = _minimal_inspect()
    data["HostConfig"]["CapAdd"] = ["NET_ADMIN"]
    data["HostConfig"]["CapDrop"] = ["MKNOD"]
    cmd = _build_docker_run_cmd(data)
    assert "--cap-add NET_ADMIN" in cmd
    assert "--cap-drop MKNOD" in cmd


def test_build_docker_run_cmd_pid_mode():
    data = _minimal_inspect()
    data["HostConfig"]["PidMode"] = "host"
    cmd = _build_docker_run_cmd(data)
    assert "--pid host" in cmd


def test_build_docker_run_cmd_ipc_mode():
    data = _minimal_inspect()
    data["HostConfig"]["IpcMode"] = "host"
    cmd = _build_docker_run_cmd(data)
    assert "--ipc host" in cmd


def test_build_docker_run_cmd_tmpfs():
    data = _minimal_inspect()
    data["HostConfig"]["Tmpfs"] = {"/run": "rw,noexec"}
    cmd = _build_docker_run_cmd(data)
    assert "--tmpfs /run" in cmd


def test_build_docker_run_cmd_port_no_host_port():
    """Port exposed in container but not bound to host — just expose the port."""
    data = _minimal_inspect()
    data["HostConfig"]["PortBindings"] = {
        "8080/tcp": [{"HostIp": "", "HostPort": ""}]
    }
    cmd = _build_docker_run_cmd(data)
    assert "-p 8080/tcp" in cmd


def test_build_docker_run_cmd_devices():
    data = _minimal_inspect()
    data["HostConfig"]["Devices"] = [
        {"PathOnHost": "/dev/sda", "PathInContainer": "/dev/sda", "CgroupPermissions": "r"}
    ]
    cmd = _build_docker_run_cmd(data)
    assert "--device /dev/sda:/dev/sda:r" in cmd


def test_build_docker_run_cmd_dns():
    data = _minimal_inspect()
    data["HostConfig"]["Dns"] = ["1.1.1.1", "8.8.8.8"]
    cmd = _build_docker_run_cmd(data)
    assert "--dns 1.1.1.1" in cmd
    assert "--dns 8.8.8.8" in cmd


def test_build_docker_run_cmd_extra_hosts():
    data = _minimal_inspect()
    data["HostConfig"]["ExtraHosts"] = ["myhost:192.168.1.10"]
    cmd = _build_docker_run_cmd(data)
    assert "--add-host myhost:192.168.1.10" in cmd


def test_build_docker_run_cmd_custom_log_driver():
    data = _minimal_inspect()
    data["HostConfig"]["LogConfig"] = {
        "Type": "syslog",
        "Config": {"syslog-address": "udp://1.2.3.4:514"},
    }
    cmd = _build_docker_run_cmd(data)
    assert "--log-driver syslog" in cmd
    assert "--log-opt syslog-address=udp://1.2.3.4:514" in cmd


def test_build_docker_run_cmd_entrypoint_and_cmd():
    data = _minimal_inspect()
    data["Config"]["Entrypoint"] = ["/entrypoint.sh", "--flag"]
    data["Config"]["Cmd"] = ["arg1", "arg2"]
    cmd = _build_docker_run_cmd(data)
    assert "--entrypoint /entrypoint.sh" in cmd
    assert "arg1" in cmd
    assert "arg2" in cmd


# ---------------------------------------------------------------------------
# SSHDockerBackend — edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_containers_for_host_skips_entries_with_no_image(config_file, data_dir):
    """Containers with empty image field are skipped."""
    import yaml

    raw = yaml.safe_load(config_file.read_text())
    raw["hosts"][0]["docker_mode"] = "all"
    config_file.write_text(yaml.dump(raw))

    docker_ps_output = "\n".join([
        json.dumps({"Names": "/good-container", "Image": "real:latest", "Labels": ""}),
        json.dumps({"Names": "/no-image-container", "Image": "", "Labels": ""}),
    ])
    inspect_output = '["real@sha256:aaa"]'

    conn = _make_multi_conn(
        [
            MagicMock(stdout=docker_ps_output, returncode=0),
            MagicMock(stdout=inspect_output, returncode=0),
        ]
    )

    with (
        patch("app.backends.ssh_docker_backend._connect", new=AsyncMock(return_value=conn)),
        patch("app.backends.ssh_docker_backend.check_image_update",
              new=AsyncMock(return_value="up_to_date")),
    ):
        backend = SSHDockerBackend()
        stacks = await backend.get_stacks_with_update_status()

    assert len(stacks) == 1
    assert stacks[0]["name"] == "good-container"


@pytest.mark.asyncio
async def test_get_stacks_skips_host_on_connection_error(config_file, data_dir):
    """Hosts that raise a connection exception are skipped (warning logged)."""
    import yaml

    raw = yaml.safe_load(config_file.read_text())
    raw["hosts"][0]["docker_mode"] = "all"
    config_file.write_text(yaml.dump(raw))

    with patch(
        "app.backends.ssh_docker_backend._connect",
        new=AsyncMock(side_effect=ConnectionError("refused")),
    ):
        backend = SSHDockerBackend()
        stacks = await backend.get_stacks_with_update_status()

    assert stacks == []


@pytest.mark.asyncio
async def test_update_standalone_container_not_found_raises(config_file, data_dir):
    """Standalone update raises when docker inspect finds no container."""
    import yaml

    raw = yaml.safe_load(config_file.read_text())
    raw["hosts"][0]["docker_mode"] = "all"
    config_file.write_text(yaml.dump(raw))

    conn = _make_multi_conn(
        [MagicMock(stdout="", returncode=1)]  # inspect fails
    )

    with patch("app.backends.ssh_docker_backend._connect", new=AsyncMock(return_value=conn)):
        backend = SSHDockerBackend()
        with pytest.raises(RuntimeError, match="not found"):
            await backend.update_stack("test-host/~missing-container")


@pytest.mark.asyncio
async def test_update_standalone_run_failure_raises(config_file, data_dir):
    """Standalone update raises when docker run fails after pull."""
    import yaml

    raw = yaml.safe_load(config_file.read_text())
    raw["hosts"][0]["docker_mode"] = "all"
    config_file.write_text(yaml.dump(raw))

    inspect_data = json.dumps([{
        "Name": "/myapp", "Id": "abc123",
        "Config": {"Image": "myimage:latest", "Env": [], "Cmd": None,
                   "Entrypoint": None, "Labels": {}, "Hostname": "myapp"},
        "HostConfig": {
            "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
            "NetworkMode": "bridge", "Privileged": False,
            "Binds": None, "PortBindings": {}, "Devices": None,
            "CapAdd": None, "CapDrop": None, "Dns": None,
            "ExtraHosts": None, "Tmpfs": None, "PidMode": "",
            "IpcMode": "private", "LogConfig": {"Type": "json-file", "Config": {}},
        },
        "NetworkSettings": {"Networks": {}},
    }])

    conn = _make_multi_conn(
        [
            MagicMock(stdout=inspect_data, returncode=0),
            MagicMock(stdout="Pulled", returncode=0),    # pull
            MagicMock(stdout="", returncode=0),          # stop
            MagicMock(stdout="", returncode=0),          # rm
            MagicMock(stdout="run error", returncode=1), # run fails
        ]
    )

    with patch("app.backends.ssh_docker_backend._connect", new=AsyncMock(return_value=conn)):
        backend = SSHDockerBackend()
        with pytest.raises(RuntimeError, match="docker run failed"):
            await backend.update_stack("test-host/~myapp")


def test_parse_json_output_ignores_invalid_lines():
    """NDJSON parser skips lines that are not valid JSON."""
    text = '{"Name": "good"}\nnot-json-at-all\n{"Name": "also-good"}'
    result = _parse_json_output(text)
    assert len(result) == 2
    assert result[0]["Name"] == "good"
    assert result[1]["Name"] == "also-good"
