"""OP#275 — Arcane edge cases found in the 2026-11-23 code review.

Paging, case-insensitive project matching, SSH updates of Arcane-owned
projects, stale stack schedules, the project list across restarts, and
error text that stays readable whatever Arcane (or a proxy) sends back.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import yaml

import app.arcane_client as arcane_client_mod
import app.backends.arcane_backend as arcane_mod
from app.arcane_client import ArcaneClient, ArcaneError
from app.backends import ArcaneBackend, SSHDockerBackend
from app.backends.arcane_backend import classify_error
from app.backends.protocol import StackUpdateError
from app.registry_client import (
    REASON_AUTH_FAILED,
    REASON_NOT_FOUND,
    REASON_RATE_LIMITED,
    REASON_REGISTRY_ERROR,
)


def _transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real = arcane_client_mod.make_client

    def make_client(**kw):
        c = real(**kw)
        c._transport = transport
        return c

    monkeypatch.setattr(arcane_client_mod, "make_client", make_client)


@pytest.fixture(autouse=True)
def coverage_file(tmp_path, monkeypatch):
    path = tmp_path / "arcane_coverage.json"
    monkeypatch.setattr(arcane_mod, "_COVERAGE_PATH", path)
    return path


def _client():
    return ArcaneClient("http://10.0.0.5:3552", "k")


# ---------------------------------------------------------------------------
# Paging
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_page_is_read(monkeypatch):
    """Arcane returns 20 per page by default; project 21+ must not vanish."""
    projects = [{"id": f"p{i}", "name": f"proj{i}"} for i in range(250)]
    starts = []

    def handler(request):
        start = int(request.url.params["start"])
        limit = int(request.url.params["limit"])
        starts.append(start)
        page = projects[start:start + limit]
        return httpx.Response(200, json={
            "data": page,
            "pagination": {"hasNextPage": start + limit < len(projects)},
        })

    _transport(monkeypatch, handler)
    got = await _client().list_projects("e")
    assert [p["id"] for p in got] == [p["id"] for p in projects]
    assert starts == [0, 100, 200]


@pytest.mark.asyncio
async def test_paging_stops_on_an_empty_page(monkeypatch):
    """A server that always claims another page must not loop forever."""
    calls = []

    def handler(request):
        calls.append(1)
        data = [{"id": "x"}] if len(calls) == 1 else []
        return httpx.Response(200, json={"data": data, "pagination": {"hasNextPage": True}})

    _transport(monkeypatch, handler)
    assert len(await _client().list_environments()) == 1
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_paging_is_capped(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"data": [{"id": "x"}], "pagination": {"hasNextPage": True}})

    _transport(monkeypatch, handler)
    assert len(await _client().list_environments()) == arcane_client_mod._MAX_PAGES


@pytest.mark.asyncio
async def test_non_list_data_is_a_readable_error(monkeypatch):
    _transport(monkeypatch, lambda r: httpx.Response(200, json={"data": {"oops": 1}}))
    with pytest.raises(ArcaneError, match="unexpected list"):
        await _client().list_environments()


# ---------------------------------------------------------------------------
# Readable errors whatever the body
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proxy_error_with_a_json_string_body(monkeypatch):
    """NPM's 502 can be a bare JSON string; that used to raise AttributeError."""
    _transport(monkeypatch, lambda r: httpx.Response(502, json="Bad Gateway"))
    with pytest.raises(ArcaneError, match="HTTP 502: Bad Gateway"):
        await _client().list_environments()


@pytest.mark.asyncio
async def test_error_with_a_json_list_body(monkeypatch):
    _transport(monkeypatch, lambda r: httpx.Response(500, json=[1, 2]))
    with pytest.raises(ArcaneError, match=r"HTTP 500: \[1,2\]"):
        await _client().list_environments()


@pytest.mark.asyncio
async def test_ok_status_with_a_non_object_body(monkeypatch):
    _transport(monkeypatch, lambda r: httpx.Response(200, text="<html>login</html>"))
    with pytest.raises(ArcaneError, match="unexpected response: <html>login</html>"):
        await _client().list_environments()


@pytest.mark.asyncio
async def test_empty_ok_body_says_so(monkeypatch):
    _transport(monkeypatch, lambda r: httpx.Response(200, text=""))
    with pytest.raises(ArcaneError, match="empty body"):
        await _client().check_project_updates("e", "p")


@pytest.mark.asyncio
async def test_check_with_non_object_data_is_empty(monkeypatch):
    _transport(monkeypatch, lambda r: httpx.Response(200, json={"data": "nope"}))
    assert await _client().check_project_updates("e", "p") == {}


@pytest.mark.parametrize("text,reason", [
    ("manifest unknown: sha256:9f429c0d", REASON_NOT_FOUND),
    ("failed to generate token", REASON_REGISTRY_ERROR),
    ("separate error", REASON_REGISTRY_ERROR),
    ("HTTP 429 Too Many Requests", REASON_RATE_LIMITED),
    ("toomanyrequests: rate limit exceeded", REASON_RATE_LIMITED),
    ("401 Unauthorized", REASON_AUTH_FAILED),
    ("denied: requested access to the resource is denied", REASON_AUTH_FAILED),
    ("blob sha256:ab403cd unreachable", REASON_REGISTRY_ERROR),
])
def test_classifier_matches_whole_words(text, reason):
    assert classify_error(text) == reason


# ---------------------------------------------------------------------------
# Case-insensitive matching + coverage persistence
# ---------------------------------------------------------------------------


def _backend_with(envs_projects):
    client = MagicMock()
    client.base = "http://10.0.0.5:3552"
    client.list_environments = AsyncMock(return_value=[e for e, _ in envs_projects])
    by_id = {e["id"]: p for e, p in envs_projects}
    client.list_projects = AsyncMock(side_effect=lambda env: by_id[env])
    return ArcaneBackend(client)


OMV = {"id": "omv", "name": "omv", "agentHost": {"publicIp": "10.0.0.10"}}


@pytest.mark.asyncio
async def test_coverage_names_are_lowercased():
    backend = _backend_with([(OMV, [{"id": "p1", "name": "ActualBudget"}])])
    assert await backend.projects_by_host() == {"10.0.0.10": {"actualbudget"}}


@pytest.mark.asyncio
async def test_coverage_survives_a_restart_during_an_outage(coverage_file):
    """Keepup restarting while Arcane is down must not hand projects to SSH."""
    first = _backend_with([(OMV, [{"id": "p1", "name": "plex"}])])
    await first.projects_by_host()
    assert json.loads(coverage_file.read_text()) == {"10.0.0.10": ["plex"]}

    restarted = _backend_with([])
    restarted._client.list_environments = AsyncMock(side_effect=httpx.ConnectError(""))
    assert await restarted.projects_by_host() == {"10.0.0.10": {"plex"}}


def test_unreadable_coverage_file_is_ignored(coverage_file):
    coverage_file.write_text("not json")
    assert arcane_mod._load_coverage() == {}
    coverage_file.write_text('["a list"]')
    assert arcane_mod._load_coverage() == {}


def test_unwritable_coverage_file_only_warns(monkeypatch, tmp_path, caplog):
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setattr(arcane_mod, "_COVERAGE_PATH", blocker / "sub" / "c.json")
    arcane_mod._save_coverage({"a": {"b"}})
    assert "could not save the project list" in caplog.text


@pytest.mark.asyncio
async def test_find_project_ref(config_file):
    backend = _backend_with([
        ({"id": "0", "name": "Arcane"}, [{"id": "k", "name": "keepup"}]),
        (OMV, [{"id": "p1", "name": "ActualBudget"}]),
    ])
    assert await backend.find_project_ref({"host": "10.0.0.10"}, "actualbudget") == "omv:p1"
    assert await backend.find_project_ref({"host": "10.0.0.10"}, "nope") is None
    assert await backend.find_project_ref({"host": ""}, "actualbudget") is None
    assert await backend.find_project_ref({"host": "10.0.0.5"}, "keepup") == "0:k"


# ---------------------------------------------------------------------------
# SSH backend: case-insensitive skip, refusal, missing compose file
# ---------------------------------------------------------------------------


def _conn(ps_rows, run_overrides=None):
    ps = "\n".join(json.dumps(r) for r in ps_rows)

    async def run(cmd, check=False):
        for needle, result in (run_overrides or {}).items():
            if needle in cmd:
                return result
        if "docker ps -a" in cmd:
            return MagicMock(stdout=ps, returncode=0)
        return MagicMock(stdout="[]\t[]", returncode=0)

    conn = MagicMock()
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    conn.run = AsyncMock(side_effect=run)
    return conn


def _one_docker_host(config_file):
    raw = yaml.safe_load(config_file.read_text())
    raw["hosts"] = [{"name": "OMV", "host": "10.0.0.10", "user": "root", "docker_mode": "all"}]
    config_file.write_text(yaml.dump(raw))


def _arcane_covering(names):
    arcane = MagicMock()
    arcane.projects_by_host = AsyncMock(return_value={"10.0.0.10": set(names)})
    return arcane


@pytest.mark.asyncio
async def test_ssh_skip_ignores_case(config_file, data_dir):
    from app.registry_client import ImageCheck

    _one_docker_host(config_file)
    rows = [{"Names": "/ab", "Image": "img", "Labels": "com.docker.compose.project=ActualBudget"}]
    with (
        patch("app.backends.ssh_docker_backend._connect", new=AsyncMock(return_value=_conn(rows))),
        patch("app.backends.ssh_docker_backend.check_image_update",
              new=AsyncMock(return_value=ImageCheck("up_to_date", None))),
        patch("app.backends.ssh_docker_backend.get_self_container_id", return_value=None),
    ):
        result = await SSHDockerBackend(arcane=_arcane_covering({"actualbudget"})).get_stacks_with_update_status()
    assert result == []


@pytest.mark.asyncio
async def test_ssh_refuses_to_update_an_arcane_project(config_file, data_dir):
    """An old SSH schedule must not redeploy an Arcane project behind its back."""
    _one_docker_host(config_file)
    backend = SSHDockerBackend(arcane=_arcane_covering({"plex"}))
    with patch("app.backends.ssh_docker_backend._connect") as connect:
        with pytest.raises(StackUpdateError, match="managed by Arcane"):
            await backend.update_stack("omv/Plex:plex")
    connect.assert_not_called()


@pytest.mark.asyncio
async def test_ssh_still_updates_standalone_and_other_projects(config_file, data_dir):
    _one_docker_host(config_file)
    backend = SSHDockerBackend(arcane=_arcane_covering({"plex"}))
    with patch.object(backend, "_update_compose_project", AsyncMock(return_value=["ok"])) as upd:
        assert await backend.update_stack("omv/other:c") == ["ok"]
    upd.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_compose_file_is_named_in_the_error(config_file, data_dir):
    """OP#132's clear message, lost with Portainer, is back for any missing file."""
    _one_docker_host(config_file)
    backend = SSHDockerBackend()
    conn = _conn([], {
        "test -f": MagicMock(stdout="", returncode=1),
        " pull": MagicMock(stdout="no configuration file provided: not found", returncode=1),
    })
    with (
        patch("app.backends.ssh_docker_backend._connect", new=AsyncMock(return_value=conn)),
        patch("app.backends.ssh_docker_backend.get_self_container_id", return_value=None),
        patch.object(backend, "_detect_compose_binary", AsyncMock(return_value="docker compose")),
        patch.object(backend, "_get_config_file", AsyncMock(return_value="/data/compose/9/docker-compose.yml")),
    ):
        with pytest.raises(StackUpdateError) as exc:
            await backend.update_stack("omv/old:old-1")
    msg = str(exc.value)
    assert "/data/compose/9/docker-compose.yml" in msg
    assert "is not on 10.0.0.10" in msg
    assert "no configuration file provided" in msg


# ---------------------------------------------------------------------------
# Stack schedule migration
# ---------------------------------------------------------------------------


def _schedules(config_file, sau):
    raw = yaml.safe_load(config_file.read_text())
    raw["hosts"] = [{"name": "OMV", "host": "10.0.0.10", "user": "root", "docker_mode": "all"}]
    raw["stack_auto_update"] = sau
    config_file.write_text(yaml.dump(raw))


def _fake_arcane(refs):
    arcane = MagicMock()
    arcane.BACKEND_KEY = "arcane"
    arcane.find_project_ref = AsyncMock(side_effect=lambda host, name: refs.get(name))
    return arcane


@pytest.mark.asyncio
async def test_portainer_schedules_are_dropped(config_file, data_dir):
    from app.auto_update_scheduler import migrate_stack_schedules
    from app.config_manager import get_all_stack_auto_updates

    _schedules(config_file, {"portainer/3:1": {"enabled": True, "schedule": "0 4 * * *", "name": "x"}})
    with patch("app.auto_update_scheduler.apply_stack_schedule") as apply:
        changes = await migrate_stack_schedules([])
    assert changes == ["dropped Portainer schedule portainer/3:1"]
    assert get_all_stack_auto_updates() == {}
    apply.assert_called_once_with("portainer/3:1")


@pytest.mark.asyncio
async def test_ssh_schedule_for_an_arcane_project_moves_to_arcane(config_file, data_dir):
    from app.auto_update_scheduler import migrate_stack_schedules
    from app.config_manager import get_all_stack_auto_updates

    _schedules(config_file, {
        "ssh/omv/plex:plex": {"enabled": True, "schedule": "0 4 * * *", "name": "plex"},
        "ssh/omv/~loner": {"enabled": True, "schedule": "0 5 * * *", "name": "loner"},
        "ssh/omv/other:o": {"enabled": True, "schedule": "0 6 * * *", "name": "o"},
        "ssh/gone/plex:plex": {"enabled": True, "schedule": "0 7 * * *", "name": "plex"},
    })
    with patch("app.auto_update_scheduler.apply_stack_schedule") as apply:
        changes = await migrate_stack_schedules([_fake_arcane({"plex": "omv-env:p1"})])
    sau = get_all_stack_auto_updates()
    assert changes == ["moved schedule ssh/omv/plex:plex → arcane/omv-env:p1"]
    assert sau["arcane/omv-env:p1"] == {"enabled": True, "schedule": "0 4 * * *", "name": "plex"}
    assert "ssh/omv/plex:plex" not in sau
    # Standalone, non-Arcane, and unknown-host schedules stay with SSH.
    assert {"ssh/omv/~loner", "ssh/omv/other:o", "ssh/gone/plex:plex"} <= set(sau)
    assert {c.args[0] for c in apply.call_args_list} == {"ssh/omv/plex:plex", "arcane/omv-env:p1"}


@pytest.mark.asyncio
async def test_existing_arcane_schedule_is_not_overwritten(config_file, data_dir):
    from app.auto_update_scheduler import migrate_stack_schedules
    from app.config_manager import get_all_stack_auto_updates

    _schedules(config_file, {
        "ssh/omv/plex:plex": {"enabled": True, "schedule": "0 4 * * *", "name": "plex"},
        "arcane/omv-env:p1": {"enabled": True, "schedule": "0 2 * * *", "name": "plex"},
    })
    with patch("app.auto_update_scheduler.apply_stack_schedule"):
        await migrate_stack_schedules([_fake_arcane({"plex": "omv-env:p1"})])
    assert get_all_stack_auto_updates() == {
        "arcane/omv-env:p1": {"enabled": True, "schedule": "0 2 * * *", "name": "plex"},
    }


@pytest.mark.asyncio
async def test_no_arcane_or_no_schedules_changes_nothing(config_file, data_dir):
    from app.auto_update_scheduler import migrate_stack_schedules

    assert await migrate_stack_schedules([]) == []
    _schedules(config_file, {"ssh/omv/plex:plex": {"enabled": True, "schedule": "0 4 * * *", "name": "plex"}})
    assert await migrate_stack_schedules([]) == []


@pytest.mark.asyncio
async def test_arcane_lookup_failure_leaves_the_schedule(config_file, data_dir, caplog):
    from app.auto_update_scheduler import migrate_stack_schedules
    from app.config_manager import get_all_stack_auto_updates

    _schedules(config_file, {"ssh/omv/plex:plex": {"enabled": True, "schedule": "0 4 * * *", "name": "plex"}})
    arcane = _fake_arcane({})
    arcane.find_project_ref = AsyncMock(side_effect=httpx.ConnectError(""))
    assert await migrate_stack_schedules([arcane]) == []
    assert "ssh/omv/plex:plex" in get_all_stack_auto_updates()
    assert "ConnectError" in caplog.text


@pytest.mark.asyncio
async def test_startup_migration_failure_does_not_stop_startup(config_file, data_dir, caplog):
    import app.main as m

    with (
        patch.object(m, "_migrate_tofu_certs"),
        patch.object(m, "apply_all_schedules") as apply_all,
        patch.object(m.scheduler, "start"),
        patch.object(m, "migrate_proxmox_servers", return_value=None),
        patch.object(m, "migrate_stack_schedules", AsyncMock(side_effect=RuntimeError("")) ),
    ):
        await m._startup()
    apply_all.assert_called_once()
    assert "Stack schedule migration failed: RuntimeError" in caplog.text


# ---------------------------------------------------------------------------
# Admin: saved key only goes to the saved URL; save reports moved schedules
# ---------------------------------------------------------------------------


def test_saved_key_is_not_sent_to_a_different_url(client):
    from app.config_manager import save_arcane_config
    from app.credentials import save_integration_credentials

    save_arcane_config("http://10.0.0.5:3552")
    save_integration_credentials("arcane", api_key="secret")
    with patch("app.arcane_client.ArcaneClient.list_environments") as listing:
        resp = client.post("/admin/integrations/arcane/test", data={
            "arcane_url": "http://attacker.example", "arcane_api_key": "",
        })
    listing.assert_not_called()
    assert "only used with the saved URL" in resp.text


def test_save_reports_moved_schedules(client):
    with patch(
        "app.auto_update_scheduler.migrate_stack_schedules",
        AsyncMock(return_value=["moved schedule a → b"]),
    ):
        resp = client.post("/admin/integrations/arcane", data={
            "arcane_url": "http://10.0.0.5:3552", "arcane_api_key": "k",
        })
    assert "1 stack auto-update schedule(s) moved to Arcane" in resp.text
