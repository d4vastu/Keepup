"""OP#261 — a compose project under /data/compose/ that Portainer does not list.

The SSH backend used to leave every `/data/compose/…` project on an agent host
to the Portainer backend, on the strength of the path alone. A project whose
stack Portainer no longer has (deployed by an earlier Portainer install, say)
was then checked by nobody. These tests pin the rule that replaced it: a project
is only left to Portainer when Portainer actually lists that stack.
"""

import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import yaml

from app.backends import SSHDockerBackend
from app.backends.ssh_docker_backend import _portainer_managed_projects, _stack_index
from app.registry_client import ImageCheck


def _container(name: str, image: str, project: str = "", config_files: str = "") -> dict:
    labels = []
    if project:
        labels.append(f"com.docker.compose.project={project}")
    if config_files:
        labels.append(f"com.docker.compose.project.config_files={config_files}")
    return {"Names": f"/{name}", "Image": image, "Labels": ",".join(labels)}


AGENT = _container("portainer_agent", "portainer/agent:2.21.0")
ACTUAL = _container(
    "actualbudget-actual_server-1",
    "actualbudget/actual-server:latest",
    project="actualbudget",
    config_files="/data/compose/58/docker-compose.yml",
)


# ---------------------------------------------------------------------------
# _portainer_managed_projects — the rule itself
# ---------------------------------------------------------------------------


def test_project_portainer_does_not_list_is_not_managed():
    """Path says Portainer, Portainer has no stack 58 → not Portainer's."""
    known = {(3, "watchtower")}
    assert _portainer_managed_projects([AGENT, ACTUAL], known) == set()


def test_project_portainer_lists_is_managed():
    known = {(58, "actualbudget")}
    assert _portainer_managed_projects([AGENT, ACTUAL], known) == {"actualbudget"}


def test_stack_id_reused_by_a_different_stack_is_not_managed():
    """An old install's id 58 can collide with an unrelated current stack."""
    known = {(58, "plex")}
    assert _portainer_managed_projects([AGENT, ACTUAL], known) == set()


def test_stack_name_match_ignores_case():
    """Compose lowercases project names; Portainer stack names keep their case."""
    known = _stack_index([{"Id": 58, "Name": "ActualBudget"}])
    assert _portainer_managed_projects([AGENT, ACTUAL], known) == {"actualbudget"}


def test_versioned_compose_path_still_yields_the_stack_id():
    """Newer Portainer writes /data/compose/{id}/v{n}/docker-compose.yml."""
    versioned = _container(
        "a", "img:1", project="actualbudget",
        config_files="/data/compose/58/v3/docker-compose.yml",
    )
    assert _portainer_managed_projects(
        [AGENT, versioned], {(58, "actualbudget")}
    ) == {"actualbudget"}


def test_without_a_stack_list_the_path_alone_decides():
    """No Portainer to ask → the old path heuristic, unchanged."""
    assert _portainer_managed_projects([AGENT, ACTUAL]) == {"actualbudget"}
    assert _portainer_managed_projects([AGENT, ACTUAL], None) == {"actualbudget"}


# ---------------------------------------------------------------------------
# The scan — what the dashboard ends up showing
# ---------------------------------------------------------------------------


def _conn(ps_rows: list[dict]) -> MagicMock:
    ps = "\n".join(json.dumps(r) for r in ps_rows)

    async def run(cmd, check=False):
        if "docker ps -a" in cmd:
            return MagicMock(stdout=ps, returncode=0)
        return MagicMock(stdout="[]\t[]", returncode=0)

    conn = MagicMock()
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    conn.run = AsyncMock(side_effect=run)
    return conn


def _enable_docker(config_file) -> None:
    raw = yaml.safe_load(config_file.read_text())
    raw["hosts"] = raw["hosts"][:1]
    raw["hosts"][0]["docker_mode"] = "all"
    config_file.write_text(yaml.dump(raw))


async def _scan(portainer_client) -> list[dict]:
    with (
        patch(
            "app.backends.ssh_docker_backend._connect",
            new=AsyncMock(return_value=_conn([AGENT, ACTUAL])),
        ),
        patch(
            "app.backends.ssh_docker_backend.check_image_update",
            new=AsyncMock(return_value=ImageCheck("update_available", None)),
        ),
        patch("app.backends.ssh_docker_backend.get_self_container_id", return_value=None),
        patch(
            "app.backends.ssh_docker_backend._portainer_integration_active",
            return_value=True,
        ),
    ):
        return await SSHDockerBackend(
            portainer_client=portainer_client
        ).get_stacks_with_update_status()


def _portainer(stacks=None, error: Exception | None = None) -> MagicMock:
    client = MagicMock()
    client.get_stacks = AsyncMock(return_value=stacks or [], side_effect=error)
    return client


@pytest.mark.asyncio
async def test_scan_reports_project_portainer_does_not_list(config_file, data_dir):
    """The production case: stack 58 is gone from Portainer, so SSH must check it."""
    _enable_docker(config_file)
    result = await _scan(_portainer(stacks=[{"Id": 3, "Name": "watchtower"}]))

    row = next(s for s in result if s["name"] == "actualbudget-actual_server-1")
    assert row["update_status"] == "update_available"
    assert row["images"][0]["name"] == "actualbudget/actual-server:latest"


@pytest.mark.asyncio
async def test_scan_leaves_project_portainer_lists_to_portainer(config_file, data_dir):
    """No duplicate row for a stack the Portainer backend already reports."""
    _enable_docker(config_file)
    result = await _scan(_portainer(stacks=[{"Id": 58, "Name": "actualbudget"}]))

    assert [s["name"] for s in result] == ["portainer_agent"]


@pytest.mark.asyncio
async def test_scan_asks_portainer_once_however_many_hosts(config_file, data_dir):
    raw = yaml.safe_load(config_file.read_text())
    raw["hosts"] = [
        {"name": "One", "host": "10.0.0.1", "user": "root", "docker_mode": "all"},
        {"name": "Two", "host": "10.0.0.2", "user": "root", "docker_mode": "all"},
    ]
    config_file.write_text(yaml.dump(raw))
    client = _portainer(stacks=[])

    await _scan(client)

    assert client.get_stacks.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error, named",
    [
        (httpx.ReadTimeout(""), "ReadTimeout"),
        (TimeoutError(), "TimeoutError"),
        (RuntimeError("Portainer returned 502"), "Portainer returned 502"),
    ],
)
async def test_scan_when_portainer_cannot_be_asked_falls_back_and_says_why(
    config_file, data_dir, caplog, error, named
):
    """Portainer down → keep the old path rule (no flood of duplicate rows while
    the Portainer backend is failing), and log a cause even when the exception
    carries no message."""
    _enable_docker(config_file)
    with caplog.at_level(logging.WARNING, logger="app.backends.ssh_docker_backend"):
        result = await _scan(_portainer(error=error))

    assert [s["name"] for s in result] == ["portainer_agent"]
    warning = next(r.getMessage() for r in caplog.records if "Portainer" in r.getMessage())
    assert named in warning
    assert "/data/compose/" in warning


@pytest.mark.asyncio
async def test_scan_logs_which_projects_it_took_over_from_portainer(
    config_file, data_dir, caplog
):
    _enable_docker(config_file)
    with caplog.at_level(logging.INFO, logger="app.backends.ssh_docker_backend"):
        await _scan(_portainer(stacks=[]))

    assert any(
        "actualbudget" in r.getMessage() and "not" in r.getMessage()
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reload_backends_gives_ssh_backend_the_portainer_client(
    config_file, data_dir
):
    """Without this the SSH backend has nobody to ask and the bug is back."""
    from app import backend_loader

    with (
        patch.object(
            backend_loader, "get_portainer_config",
            return_value={"url": "https://portainer.test:9443"},
        ),
        patch.object(
            backend_loader, "get_integration_credentials",
            return_value={"api_key": "k"},
        ),
    ):
        backends = await backend_loader.reload_backends()

    portainer = next(b for b in backends if b.BACKEND_KEY == "portainer")
    ssh = next(b for b in backends if b.BACKEND_KEY == "ssh")
    assert ssh._portainer is portainer._client


@pytest.mark.asyncio
async def test_reload_backends_without_portainer_gives_ssh_backend_none(
    config_file, data_dir
):
    from app import backend_loader

    with (
        patch.object(backend_loader, "get_portainer_config", return_value={}),
        patch.object(backend_loader, "get_integration_credentials", return_value={}),
    ):
        backends = await backend_loader.reload_backends()

    ssh = next(b for b in backends if b.BACKEND_KEY == "ssh")
    assert ssh._portainer is None


# ---------------------------------------------------------------------------
# Updating — the row is visible now, so its failure has to make sense
# ---------------------------------------------------------------------------


def _update_conn(file_exists: bool) -> MagicMock:
    ps = "\n".join(json.dumps(r) for r in [AGENT, ACTUAL])

    async def run(cmd, check=False):
        if "docker ps -a" in cmd:
            return MagicMock(stdout=ps, returncode=0)
        if "test -f" in cmd:
            return MagicMock(stdout="exists" if file_exists else "", returncode=0)
        return MagicMock(stdout="v2", returncode=0)

    conn = MagicMock()
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    conn.run = AsyncMock(side_effect=run)
    return conn


async def _update(portainer_client, conn) -> list[str]:
    host = {"slug": "h", "host": "1.2.3.4"}
    with (
        patch("app.backends.ssh_docker_backend._connect", new=AsyncMock(return_value=conn)),
        patch("app.backends.ssh_docker_backend.get_self_container_id", return_value=None),
        patch(
            "app.backends.ssh_docker_backend._portainer_integration_active",
            return_value=True,
        ),
    ):
        return await SSHDockerBackend(
            portainer_client=portainer_client
        )._update_compose_project(host, "actualbudget")


def _commands(conn) -> list[str]:
    return [c.args[0] for c in conn.run.await_args_list]


@pytest.mark.asyncio
async def test_update_of_unlisted_project_without_compose_file_says_so(data_dir):
    """Portainer has no entry to send the user to, so don't send them there."""
    conn = _update_conn(file_exists=False)
    with pytest.raises(RuntimeError) as exc:
        await _update(_portainer(stacks=[{"Id": 3, "Name": "watchtower"}]), conn)

    msg = str(exc.value)
    assert "/data/compose/58/docker-compose.yml" in msg
    assert "no longer lists" in msg
    assert "Portainer entry" not in msg
    assert not any(" pull" in c or " up -d" in c for c in _commands(conn))


@pytest.mark.asyncio
async def test_update_of_listed_project_still_points_to_its_portainer_entry(data_dir):
    conn = _update_conn(file_exists=False)
    with pytest.raises(RuntimeError) as exc:
        await _update(_portainer(stacks=[{"Id": 58, "Name": "actualbudget"}]), conn)

    assert "Portainer entry" in str(exc.value)


@pytest.mark.asyncio
async def test_update_of_unlisted_project_with_compose_file_on_host_runs(data_dir):
    """An old stack whose file is still on the host updates like any project."""
    conn = _update_conn(file_exists=True)
    lines = await _update(_portainer(stacks=[]), conn)

    assert lines[-1] == "Compose update complete."
    assert any(
        "-f /data/compose/58/docker-compose.yml pull" in c for c in _commands(conn)
    )
