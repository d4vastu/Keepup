"""OP#273 — check and apply container updates through Arcane.

Arcane manages the Compose projects on every host with an agent. Keepup reads
their update status from it, asks it to pull and bring a project up, and leaves
those projects out of its own SSH checks so nothing is checked twice.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import yaml

import app.arcane_client as arcane_client_mod
from app.arcane_client import DEPLOY_TIMEOUT, ArcaneClient, ArcaneError
from app.backends import ArcaneBackend, SSHDockerBackend
from app.backends.arcane_backend import classify_error, image_statuses, rollup
from app.backends.protocol import StackUpdateError
from app.registry_client import (
    REASON_AUTH_FAILED,
    REASON_ENDPOINT_UNREACHABLE,
    REASON_NO_CONTAINERS,
    REASON_NO_LOCAL_DIGEST,
    REASON_NOT_FOUND,
    REASON_RATE_LIMITED,
    REASON_REGISTRY_ERROR,
    ImageCheck,
)

# ---------------------------------------------------------------------------
# Fixtures: a fake Arcane served through httpx.MockTransport
# ---------------------------------------------------------------------------

ENV_MANAGER = {"id": "0", "name": "Arcane", "apiUrl": "https://arcane.example.test"}
ENV_OMV = {
    "id": "env-omv", "name": "omv2023", "apiUrl": "agent://omv2023",
    "agentHost": {"publicIp": "192.168.1.10"},
}
ENV_MC = {
    "id": "env-mc", "name": "Minecraft", "apiUrl": "agent://minecraft",
    "agentHost": {"publicIp": "192.168.1.99"},
}


def _info(**refs) -> dict:
    """updateInfo with one entry per image ref: value True/False/'error text'."""
    by_ref = {}
    for ref, v in refs.items():
        ref = ref.replace("__", "/").replace("_", ":")
        if isinstance(v, str):
            by_ref[ref] = {"hasUpdate": False, "error": v}
        else:
            by_ref[ref] = {"hasUpdate": v, "error": ""}
    return {"updateInfoByRef": by_ref, "imageRefs": list(by_ref)}


def _project(pid, name, info=None, container_id="aaaaaaaaaaaa1111"):
    return {
        "id": pid, "name": name, "updateInfo": info or {},
        "runtimeServices": [{"containerId": container_id}],
    }


class FakeArcane:
    """Minimal Arcane API: envs, projects, update checks, and streamed deploys."""

    def __init__(self):
        self.envs = [ENV_MANAGER, ENV_OMV]
        self.projects = {
            "0": [_project("p-keepup", "keepup", container_id="5e1f5e1f5e1f9999")],
            "env-omv": [
                _project("p-plex", "plex"),
                _project("p-cf", "cloudflared"),
            ],
        }
        self.checks = {
            "p-plex": _info(**{"lscr.io__linuxserver__plex_latest": True}),
            "p-cf": _info(**{"cloudflare__cloudflared_latest": False}),
        }
        self.failing_envs: set[str] = set()
        self.failing_checks: set[str] = set()
        self.deploy_streams: dict[str, list[dict]] = {}
        self.calls: list[tuple[str, str]] = []
        self.timeouts: list = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path + ("?" + request.url.query.decode() if request.url.query else "")))
        self.timeouts.append(request.extensions.get("timeout"))
        if request.headers.get("X-API-Key") != "k":
            return httpx.Response(401, json={"title": "Unauthorized", "detail": "Unauthorized: invalid API key"})
        parts = path.split("/")
        if path == "/api/agents":
            return httpx.Response(200, json={"data": self.envs})
        if path.endswith("/projects") and request.method == "GET":
            env = parts[3]
            if env in self.failing_envs:
                return httpx.Response(502, json={"detail": "agent offline"})
            return httpx.Response(200, json={"data": self.projects.get(env, [])})
        if "/updater/projects/" in path:
            pid = parts[-2]
            if pid in self.failing_checks:
                raise httpx.ReadTimeout("")
            return httpx.Response(200, json={"data": self.checks.get(pid, {})})
        if path.endswith("/deploy"):
            action = request.url.params["action"]
            events = self.deploy_streams.get(action, [{"log": f"{action} ok"}, {"done": True}])
            body = "\n".join(json.dumps(e) for e in events) + "\n"
            return httpx.Response(200, content=body.encode())
        return httpx.Response(404, json={"detail": "nope"})


@pytest.fixture
def fake(monkeypatch):
    fake = FakeArcane()
    transport = httpx.MockTransport(fake.handler)
    real = arcane_client_mod.make_client

    def make_client(**kw):
        client = real(**kw)
        client._transport = transport
        return client

    monkeypatch.setattr(arcane_client_mod, "make_client", make_client)
    return fake


def _client(url="http://192.168.1.50:3552", key="k"):
    return ArcaneClient(url=url, api_key=key)


def _hosts(config_file, hosts):
    raw = yaml.safe_load(config_file.read_text())
    raw["hosts"] = hosts
    config_file.write_text(yaml.dump(raw))


KEEPUP_HOSTS = [
    {"name": "Arcane CT", "host": "192.168.1.50", "user": "root", "docker_mode": "all"},
    {"name": "OMV", "host": "192.168.1.10", "user": "root", "docker_mode": "all"},
]


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


def test_deploy_timeout_is_generous():
    """OP#228: a long pull must not be cut off by a short inherited default."""
    assert DEPLOY_TIMEOUT.read >= 600


@pytest.mark.asyncio
async def test_deploy_uses_the_long_timeout(fake):
    await _client().deploy("env-omv", "p-plex", "pull")
    deploy_timeout = fake.timeouts[-1]
    assert deploy_timeout["read"] == DEPLOY_TIMEOUT.read


@pytest.mark.asyncio
async def test_deploy_returns_streamed_log_lines(fake):
    fake.deploy_streams["pull"] = [
        {"type": "activity", "activityId": "x"},
        {"log": "latest: Pulling from linuxserver/plex"},
        {"message": "pulled"},
        "not json",
        {"done": True},
    ]
    lines = await _client().deploy("env-omv", "p-plex", "pull")
    assert lines == ["latest: Pulling from linuxserver/plex", "pulled", '"not json"']
    assert ("POST", "/api/agents/env-omv/projects/p-plex/deploy?action=pull") in fake.calls


@pytest.mark.asyncio
async def test_deploy_error_inside_a_200_stream_is_a_failure(fake):
    """Arcane answers 200 and reports failure in the stream itself."""
    fake.deploy_streams["up"] = [{"log": "starting"}, {"error": "project not found"}]
    with pytest.raises(ArcaneError) as exc:
        await _client().deploy("env-omv", "p-plex", "up")
    assert "project not found" in str(exc.value)
    assert exc.value.lines == ["starting", "Error: project not found"]


@pytest.mark.asyncio
async def test_deploy_stream_that_never_finishes_is_a_failure(fake):
    fake.deploy_streams["pull"] = [{"log": "half way"}]
    with pytest.raises(ArcaneError, match="before reporting it was done"):
        await _client().deploy("env-omv", "p-plex", "pull")


@pytest.mark.asyncio
async def test_bad_key_names_the_cause(fake):
    with pytest.raises(ArcaneError, match="401.*invalid API key"):
        await _client(key="wrong").list_environments()


@pytest.mark.asyncio
async def test_deploy_http_error_names_the_cause(fake):
    with pytest.raises(ArcaneError, match="401"):
        await _client(key="wrong").deploy("env-omv", "p-plex", "pull")


@pytest.mark.asyncio
async def test_non_json_error_body_is_still_reported(monkeypatch):
    transport = httpx.MockTransport(lambda r: httpx.Response(500, text="boom"))
    real = arcane_client_mod.make_client

    def make_client(**kw):
        c = real(**kw)
        c._transport = transport
        return c

    monkeypatch.setattr(arcane_client_mod, "make_client", make_client)
    with pytest.raises(ArcaneError, match="HTTP 500: boom"):
        await _client().list_environments()


# ---------------------------------------------------------------------------
# Status mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,reason", [
    ("toomanyrequests: 429", REASON_RATE_LIMITED),
    ("401 Unauthorized", REASON_AUTH_FAILED),
    ("pull access denied", REASON_AUTH_FAILED),
    ("manifest unknown", REASON_NOT_FOUND),
    ("connection reset", REASON_REGISTRY_ERROR),
    ("", REASON_REGISTRY_ERROR),
])
def test_classify_error(text, reason):
    assert classify_error(text) == reason


def test_image_statuses_cover_every_case():
    info = {
        "imageRefs": ["a:1", "b:1", "c:1", "d:1"],
        "updateInfoByRef": {
            "a:1": {"hasUpdate": True, "error": ""},
            "b:1": {"hasUpdate": False, "error": ""},
            "c:1": {"hasUpdate": False, "error": "manifest unknown"},
        },
    }
    by_name = {i["name"]: i for i in image_statuses(info)}
    assert by_name["a:1"]["status"] == "update_available"
    assert by_name["b:1"]["status"] == "up_to_date"
    assert by_name["c:1"] == {
        "name": "c:1", "status": "unknown", "reason": REASON_NOT_FOUND,
        "detail": "manifest unknown",
    }
    assert by_name["d:1"]["reason"] == REASON_NO_LOCAL_DIGEST
    assert by_name["d:1"]["detail"]


def test_rollup():
    up = {"name": "a", "status": "up_to_date", "reason": None, "detail": ""}
    new = {"name": "b", "status": "update_available", "reason": None, "detail": ""}
    unk = {"name": "c", "status": "unknown", "reason": REASON_NOT_FOUND, "detail": "gone"}
    unk2 = {"name": "d", "status": "unknown", "reason": REASON_RATE_LIMITED, "detail": "429"}
    assert rollup([]) == ("unknown", REASON_NO_CONTAINERS, "Arcane lists no images for this project")
    assert rollup([new]) == ("update_available", None, "")
    assert rollup([new, up]) == ("mixed", None, "")
    assert rollup([up]) == ("up_to_date", None, "")
    assert rollup([up, unk]) == ("unknown", REASON_NOT_FOUND, "c: gone")
    status, reason, detail = rollup([unk, unk2])
    assert (status, reason) == ("unknown", None)
    assert "c: gone" in detail and "d: 429" in detail


# ---------------------------------------------------------------------------
# Backend: listing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_projects_are_filed_under_the_matching_keepup_host(fake, config_file):
    _hosts(config_file, KEEPUP_HOSTS)
    with patch("app.backends.arcane_backend.get_self_container_id", return_value=None):
        rows = await ArcaneBackend(_client()).get_stacks_with_update_status()

    by_name = {r["name"]: r for r in rows}
    assert by_name["plex"]["endpoint_name"] == "OMV"
    assert by_name["plex"]["update_status"] == "update_available"
    assert by_name["plex"]["update_path"] == "arcane/env-omv:p-plex"
    assert by_name["plex"]["connection_badge"] == "Arcane"
    assert by_name["plex"]["arcane_url"] == "https://arcane.example.test/projects/p-plex?env=env-omv"
    assert by_name["cloudflared"]["update_status"] == "up_to_date"
    # The manager's own Docker matches the host Keepup reaches Arcane on.
    assert by_name["keepup"]["endpoint_name"] == "Arcane CT"


@pytest.mark.asyncio
async def test_environment_without_a_keepup_host_keeps_its_arcane_name(fake, config_file):
    _hosts(config_file, KEEPUP_HOSTS)
    fake.envs = [ENV_MANAGER, ENV_OMV, ENV_MC]
    fake.projects["env-mc"] = [_project("p-mc", "minecraft")]
    rows = await ArcaneBackend(_client()).get_stacks_with_update_status()
    assert next(r for r in rows if r["name"] == "minecraft")["endpoint_name"] == "Minecraft"


@pytest.mark.asyncio
async def test_keepups_own_project_is_never_offered(fake, config_file):
    """Redeploying Keepup from inside Keepup would kill the job mid-run."""
    _hosts(config_file, KEEPUP_HOSTS)
    with patch("app.backends.arcane_backend.get_self_container_id", return_value="5e1f5e1f5e1f"):
        rows = await ArcaneBackend(_client()).get_stacks_with_update_status()
    assert "keepup" not in {r["name"] for r in rows}


@pytest.mark.asyncio
async def test_failed_live_check_falls_back_to_arcanes_last_result(fake, config_file):
    _hosts(config_file, KEEPUP_HOSTS)
    fake.failing_checks.add("p-cf")
    fake.projects["env-omv"][1]["updateInfo"] = _info(**{"cloudflare__cloudflared_latest": True})
    rows = await ArcaneBackend(_client()).get_stacks_with_update_status()
    assert next(r for r in rows if r["name"] == "cloudflared")["update_status"] == "update_available"


@pytest.mark.asyncio
async def test_unreachable_environment_is_shown_not_dropped(fake, config_file):
    """OP#216: an environment that cannot be listed must not look like 'no updates'."""
    _hosts(config_file, KEEPUP_HOSTS)
    fake.failing_envs.add("env-omv")
    rows = await ArcaneBackend(_client()).get_stacks_with_update_status()
    row = next(r for r in rows if r["endpoint_id"] == "env-omv")
    assert row["update_status"] == "unknown"
    assert row["unknown_reason"] == REASON_ENDPOINT_UNREACHABLE
    assert "agent offline" in row["unknown_detail"]
    assert row["name"] == "omv2023 (Arcane environment)"


@pytest.mark.asyncio
async def test_unknown_image_carries_arcanes_error_text(fake, config_file):
    _hosts(config_file, KEEPUP_HOSTS)
    fake.checks["p-cf"] = _info(**{"cloudflare__cloudflared_latest": "toomanyrequests"})
    rows = await ArcaneBackend(_client()).get_stacks_with_update_status()
    row = next(r for r in rows if r["name"] == "cloudflared")
    assert row["update_status"] == "unknown"
    assert row["unknown_reason"] == REASON_RATE_LIMITED
    assert "toomanyrequests" in row["unknown_detail"]


@pytest.mark.asyncio
async def test_listing_is_cached_briefly(fake, config_file):
    """The SSH backend's coverage question and the check share one listing."""
    _hosts(config_file, KEEPUP_HOSTS)
    backend = ArcaneBackend(_client())
    await backend.projects_by_host()
    await backend.get_stacks_with_update_status()
    assert sum(1 for m, p in fake.calls if p == "/api/agents") == 1


@pytest.mark.asyncio
async def test_app_url_falls_back_to_the_configured_url(fake, config_file):
    _hosts(config_file, KEEPUP_HOSTS)
    fake.envs = [{**ENV_MANAGER, "apiUrl": ""}, ENV_OMV]
    rows = await ArcaneBackend(_client()).get_stacks_with_update_status()
    plex = next(r for r in rows if r["name"] == "plex")
    assert plex["arcane_url"].startswith("http://192.168.1.50:3552/projects/p-plex")


# ---------------------------------------------------------------------------
# Backend: coverage for the SSH backend
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_projects_by_host(fake):
    cov = await ArcaneBackend(_client()).projects_by_host()
    assert cov == {"192.168.1.50": {"keepup"}, "192.168.1.10": {"plex", "cloudflared"}}


@pytest.mark.asyncio
async def test_coverage_survives_an_arcane_outage(fake):
    """Arcane down must not make SSH take over (and re-notify) every project."""
    backend = ArcaneBackend(_client())
    first = await backend.projects_by_host()
    backend._snapshot = None
    fake.envs = None  # make /api/agents blow up
    with patch.object(backend._client, "list_environments", AsyncMock(side_effect=httpx.ConnectError(""))):
        assert await backend.projects_by_host() == first


@pytest.mark.asyncio
async def test_coverage_survives_one_agent_going_offline(fake):
    backend = ArcaneBackend(_client())
    await backend.projects_by_host()
    backend._snapshot = None
    fake.failing_envs.add("env-omv")
    cov = await backend.projects_by_host()
    assert cov["192.168.1.10"] == {"plex", "cloudflared"}


# ---------------------------------------------------------------------------
# Backend: applying an update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_pulls_then_brings_the_project_up(fake):
    lines = await ArcaneBackend(_client()).update_stack("env-omv:p-plex")
    deploys = [p for m, p in fake.calls if p.endswith(("action=pull", "action=up"))]
    assert deploys == [
        "/api/agents/env-omv/projects/p-plex/deploy?action=pull",
        "/api/agents/env-omv/projects/p-plex/deploy?action=up",
    ]
    assert "pull ok" in lines and "up ok" in lines
    assert lines[-1] == "Arcane finished the update."


@pytest.mark.asyncio
async def test_failed_update_keeps_arcanes_output_and_reason(fake):
    fake.deploy_streams["up"] = [{"log": "Recreating plex"}, {"error": "port is already allocated"}]
    with pytest.raises(StackUpdateError) as exc:
        await ArcaneBackend(_client()).update_stack("env-omv:p-plex")
    assert "port is already allocated" in str(exc.value)
    assert "Recreating plex" in exc.value.lines
    assert "pull ok" in exc.value.lines


@pytest.mark.asyncio
async def test_message_less_failure_still_says_something(fake):
    """httpx.ReadTimeout('') must not become an empty error (CLAUDE.md QA rule)."""
    backend = ArcaneBackend(_client())
    with patch.object(backend._client, "deploy", AsyncMock(side_effect=httpx.ReadTimeout(""))):
        with pytest.raises(StackUpdateError) as exc:
            await backend.update_stack("env-omv:p-plex")
    assert str(exc.value) == "Arcane update failed: ReadTimeout"


@pytest.mark.asyncio
@pytest.mark.parametrize("ref", ["", "env-omv", "env-omv:*"])
async def test_update_refuses_a_non_project_ref(fake, ref):
    with pytest.raises(StackUpdateError, match="Not an Arcane project"):
        await ArcaneBackend(_client()).update_stack(ref)


@pytest.mark.asyncio
async def test_describe_ref(fake):
    backend = ArcaneBackend(_client())
    assert await backend.describe_ref("env-omv:p-plex") == "plex"
    assert await backend.describe_ref("env-omv:missing") == "env-omv:missing"
    fake.failing_envs.add("env-omv")
    assert await backend.describe_ref("env-omv:p-plex") == "env-omv:p-plex"


# ---------------------------------------------------------------------------
# SSH backend leaves Arcane's projects alone
# ---------------------------------------------------------------------------


def _container(name, image, project=""):
    labels = f"com.docker.compose.project={project}" if project else ""
    return {"Names": f"/{name}", "Image": image, "Labels": labels}


def _conn(rows):
    ps = "\n".join(json.dumps(r) for r in rows)

    async def run(cmd, check=False):
        if "docker ps -a" in cmd:
            return MagicMock(stdout=ps, returncode=0)
        return MagicMock(stdout="[]\t[]", returncode=0)

    conn = MagicMock()
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    conn.run = AsyncMock(side_effect=run)
    return conn


@pytest.mark.asyncio
async def test_ssh_backend_skips_projects_arcane_manages(config_file, data_dir):
    _hosts(config_file, [KEEPUP_HOSTS[1]])
    rows = [
        _container("plex", "lscr.io/linuxserver/plex", project="plex"),
        _container("arcane-agent", "ghcr.io/getarcaneapp/agent", project="arcane-agent"),
        _container("loner", "alpine:3"),
    ]
    arcane = MagicMock()
    arcane.projects_by_host = AsyncMock(return_value={"192.168.1.10": {"plex"}})
    with (
        patch("app.backends.ssh_docker_backend._connect", new=AsyncMock(return_value=_conn(rows))),
        patch(
            "app.backends.ssh_docker_backend.check_image_update",
            new=AsyncMock(return_value=ImageCheck("up_to_date", None)),
        ),
        patch("app.backends.ssh_docker_backend.get_self_container_id", return_value=None),
    ):
        result = await SSHDockerBackend(arcane=arcane).get_stacks_with_update_status()

    # Standalone containers and projects Arcane does not manage are still checked.
    assert sorted(r["name"] for r in result) == ["arcane-agent", "loner"]


@pytest.mark.asyncio
async def test_ssh_backend_without_arcane_checks_everything(config_file, data_dir):
    _hosts(config_file, [KEEPUP_HOSTS[1]])
    rows = [_container("plex", "lscr.io/linuxserver/plex", project="plex")]
    with (
        patch("app.backends.ssh_docker_backend._connect", new=AsyncMock(return_value=_conn(rows))),
        patch(
            "app.backends.ssh_docker_backend.check_image_update",
            new=AsyncMock(return_value=ImageCheck("up_to_date", None)),
        ),
        patch("app.backends.ssh_docker_backend.get_self_container_id", return_value=None),
    ):
        result = await SSHDockerBackend().get_stacks_with_update_status()
    assert [r["name"] for r in result] == ["plex"]


@pytest.mark.asyncio
async def test_hostname_and_ip_for_one_host_match():
    from app.backends.ssh_docker_backend import _arcane_projects_for

    with patch(
        "app.backends.arcane_backend.asyncio.get_running_loop"
    ) as loop:
        loop.return_value.getaddrinfo = AsyncMock(return_value=[(2, 1, 6, "", ("192.168.1.10", 0))])
        got = await _arcane_projects_for({"host": "omv.lan"}, {"192.168.1.10": {"plex"}})
    assert got == {"plex"}
    assert await _arcane_projects_for({"host": "10.0.0.1"}, {}) == set()


@pytest.mark.asyncio
async def test_unresolvable_hostname_is_compared_as_is():
    from app.backends.arcane_backend import _resolve

    with patch("app.backends.arcane_backend.asyncio.get_running_loop") as loop:
        loop.return_value.getaddrinfo = AsyncMock(side_effect=OSError("no such host"))
        assert await _resolve("nowhere.invalid") == "nowhere.invalid"
    assert await _resolve("") == ""


# ---------------------------------------------------------------------------
# Wiring: loader, admin card, dashboard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loader_builds_arcane_and_hands_it_to_ssh(config_file, data_dir):
    from app.backend_loader import reload_backends
    from app.config_manager import save_arcane_config
    from app.credentials import save_integration_credentials

    save_arcane_config("http://192.168.1.50:3552/", verify_ssl=False)
    save_integration_credentials("arcane", api_key="k")
    backends = await reload_backends()
    arcane = next(b for b in backends if b.BACKEND_KEY == "arcane")
    ssh = next(b for b in backends if b.BACKEND_KEY == "ssh")
    assert ssh._arcane is arcane
    assert arcane._client.base == "http://192.168.1.50:3552"
    assert arcane._client._verify is False


@pytest.mark.asyncio
async def test_loader_without_arcane(config_file, data_dir):
    from app.backend_loader import reload_backends

    backends = await reload_backends()
    assert "arcane" not in {b.BACKEND_KEY for b in backends}


def test_admin_page_shows_the_arcane_card(client):
    html = client.get("/admin/integrations").text
    assert 'id="config-arcane"' in html
    assert "/admin/integrations/arcane/test" in html
    assert "/admin/integrations/arcane/remove" not in html  # nothing to remove yet


def test_save_then_remove_arcane(client):
    from app.config_manager import get_arcane_config
    from app.credentials import get_integration_credentials

    resp = client.post("/admin/integrations/arcane", data={
        "arcane_url": "http://192.168.1.50:3552/", "arcane_api_key": "k",
    })
    assert "Arcane saved" in resp.text
    assert get_arcane_config() == {"url": "http://192.168.1.50:3552"}
    assert get_integration_credentials("arcane") == {"api_key": "k"}
    assert "/admin/integrations/arcane/remove" in client.get("/admin/integrations").text

    resp = client.post("/admin/integrations/arcane/remove")
    assert "Arcane removed" in resp.text
    assert resp.headers.get("HX-Refresh") == "true"
    assert get_arcane_config() == {}
    assert get_integration_credentials("arcane") == {}


def test_save_keeps_the_stored_key_when_left_blank(client):
    from app.credentials import get_integration_credentials

    client.post("/admin/integrations/arcane", data={"arcane_url": "http://a:3552", "arcane_api_key": "k"})
    resp = client.post("/admin/integrations/arcane", data={
        "arcane_url": "https://b:3552", "arcane_api_key": "", "arcane_verify_ssl": "off",
    })
    assert "Arcane saved" in resp.text
    assert get_integration_credentials("arcane") == {"api_key": "k"}
    from app.config_manager import get_arcane_config
    assert get_arcane_config() == {"url": "https://b:3552", "verify_ssl": False}


@pytest.mark.parametrize("data,msg", [
    ({"arcane_url": "", "arcane_api_key": "k"}, "Enter the Arcane URL"),
    ({"arcane_url": "ftp://x", "arcane_api_key": "k"}, "must start with http"),
    ({"arcane_url": "http://x:3552", "arcane_api_key": ""}, "Paste an Arcane API key"),
])
def test_save_rejects_incomplete_input(client, data, msg):
    assert msg in client.post("/admin/integrations/arcane", data=data).text


def test_connection_test_reports_what_keepup_would_see(client, fake):
    fake.envs = [ENV_MANAGER, ENV_OMV, ENV_MC]
    fake.failing_envs.add("env-mc")
    resp = client.post("/admin/integrations/arcane/test", data={
        "arcane_url": "http://192.168.1.50:3552", "arcane_api_key": "k",
    })
    assert "Connected — 3 environments, 3 projects." in resp.text
    assert "Not reachable: Minecraft." in resp.text


def test_connection_test_failure_names_the_cause(client, fake):
    resp = client.post("/admin/integrations/arcane/test", data={
        "arcane_url": "http://192.168.1.50:3552", "arcane_api_key": "wrong",
    })
    assert "Could not connect to Arcane" in resp.text
    assert "invalid API key" in resp.text


def test_connection_test_needs_url_and_key(client):
    resp = client.post("/admin/integrations/arcane/test", data={"arcane_url": "", "arcane_api_key": ""})
    assert "Enter the Arcane URL and an API key" in resp.text


def test_connection_test_uses_the_saved_key(client, fake):
    from app.credentials import save_integration_credentials

    save_integration_credentials("arcane", api_key="k")
    resp = client.post("/admin/integrations/arcane/test", data={
        "arcane_url": "http://192.168.1.50:3552", "arcane_api_key": "",
    })
    assert "Connected" in resp.text


def _render_status(stacks):
    from app.templates_env import make_templates

    tpl = make_templates().get_template("partials/docker_status.html")
    return tpl.render(request=MagicMock(), stacks=stacks, failed_backends=[])


def test_dashboard_links_arcane_projects_and_keeps_ids_css_safe():
    """Project names from Arcane end up in element ids; they must be CSS-safe."""
    row = {
        "id": "env(1):p(2)", "name": "media (old)", "endpoint_id": "env(1)",
        "endpoint_name": "OMV", "connection_badge": "Arcane",
        "update_status": "update_available", "unknown_reason": None,
        "images": [], "update_path": "arcane/env(1):p(2)",
        "arcane_url": "https://arcane.example.test/projects/p(2)?env=env(1)",
    }
    html = _render_status([row])
    assert 'href="https://arcane.example.test/projects/p(2)?env=env(1)"' in html
    import re

    ids = re.findall(r'id="(stack-[^"]*)"', html)
    assert ids and all(re.fullmatch(r"[a-z0-9-]+", i) for i in ids)
    assert "(" not in "".join(ids)


def test_unknown_row_shows_arcanes_error_on_hover():
    row = {
        "id": "e:p", "name": "cloudflared", "endpoint_id": "e", "endpoint_name": "OMV",
        "update_status": "unknown", "unknown_reason": REASON_RATE_LIMITED,
        "unknown_detail": "cloudflare/cloudflared:latest: toomanyrequests",
        "images": [], "update_path": "arcane/e:p", "arcane_url": "https://a/projects/p?env=e",
    }
    html = _render_status([row])
    assert 'title="cloudflare/cloudflared:latest: toomanyrequests"' in html
    assert "Rate limited" in html
