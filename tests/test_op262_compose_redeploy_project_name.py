"""OP#262 — a compose redeploy must name the project it is redeploying.

`docker compose -f <file>` with no `-p` names the project after the folder the
file is in. For a project started under another name (`-p`, or by Portainer,
which keeps files in `/data/compose/{id}/`) that is a *different* project, so
`up -d` created a second network and container beside the running ones.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.backends import SSHDockerBackend

COMPOSE_FILE = "/data/compose/58/docker-compose.yml"


def _conn(project: str, flavour: str = "v2") -> MagicMock:
    ps = json.dumps(
        {
            "Names": "/actual_server",
            "Image": "actualbudget/actual-server:latest",
            "Labels": f"com.docker.compose.project={project},"
            f"com.docker.compose.project.config_files={COMPOSE_FILE}",
        }
    )

    async def run(cmd, check=False):
        if "docker ps -a" in cmd:
            return MagicMock(stdout=ps, returncode=0)
        if "test -f" in cmd:
            return MagicMock(stdout="exists", returncode=0)
        return MagicMock(stdout=flavour, returncode=0)

    conn = MagicMock()
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    conn.run = AsyncMock(side_effect=run)
    return conn


async def _redeploy(conn, project: str) -> list[str]:
    host = {"slug": "h", "host": "1.2.3.4"}
    with (
        patch("app.backends.ssh_docker_backend._connect", new=AsyncMock(return_value=conn)),
        patch("app.backends.ssh_docker_backend.get_self_container_id", return_value=None),
    ):
        return await SSHDockerBackend()._update_compose_project(host, project)


def _commands(conn) -> list[str]:
    return [c.args[0] for c in conn.run.await_args_list]


@pytest.mark.asyncio
@pytest.mark.parametrize("step", ["pull", "up -d"])
async def test_redeploy_names_the_project_as_well_as_the_file(data_dir, step):
    """The production case: project `actualbudget` living in a folder named `58`."""
    conn = _conn("actualbudget")
    await _redeploy(conn, "actualbudget")

    cmd = next(c for c in _commands(conn) if f" {step}" in c)
    assert f"docker compose -p actualbudget -f {COMPOSE_FILE} {step}" in cmd


@pytest.mark.asyncio
async def test_redeploy_names_the_project_on_compose_v1_too(data_dir):
    conn = _conn("actualbudget", flavour="v1")
    await _redeploy(conn, "actualbudget")

    assert any(
        f"docker-compose -p actualbudget -f {COMPOSE_FILE} up -d" in c
        for c in _commands(conn)
    )


@pytest.mark.asyncio
async def test_redeploy_log_shows_the_command_that_ran(data_dir):
    """The Activity log is where a reader checks which project was touched."""
    lines = await _redeploy(_conn("actualbudget"), "actualbudget")

    assert f"$ docker compose -p actualbudget -f {COMPOSE_FILE} up -d" in lines


@pytest.mark.asyncio
async def test_redeploy_quotes_an_awkward_project_name(data_dir):
    """Project names come from container labels, so they are not trusted."""
    conn = _conn("my proj(1)")
    await _redeploy(conn, "my proj(1)")

    assert any("-p 'my proj(1)' -f" in c and " pull" in c for c in _commands(conn))
