"""OP#239 — the shared detection layer behind both the routes and the job.

Detection used to live inline in two request handlers, so a background job had
no way to reach it. `update_scan` holds it once; `docker_check` and `host_check`
render what it returns, and the scheduled job calls the same functions with no
HTTP request in sight.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

SSH_HOST = {"name": "Web", "slug": "web", "host": "10.0.0.1", "user": "root"}
LXC_HOST = {
    "name": "Portainer",
    "slug": "portainer",
    "host": "10.0.0.2",
    "proxmox_node": "pve",
    "proxmox_vmid": 101,
}
NODE_HOST = {"name": "pve", "slug": "pve", "host": "10.0.0.3", "proxmox_node": "pve"}

PKG = {"name": "curl", "current": "1.0", "available": "1.1"}


# ---------------------------------------------------------------------------
# scan_host — the three branches, extracted from the route
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ssh_host_reports_its_packages(data_dir):
    import app.update_scan as us

    with patch(
        "app.update_scan.check_host_updates",
        new=AsyncMock(
            return_value={
                "packages": [PKG],
                "reboot_required": True,
                "package_manager": "apt",
            }
        ),
    ):
        result = await us.scan_host(SSH_HOST, {})

    assert result["packages"] == [PKG]
    assert result["reboot_required"] is True
    assert result["package_manager"] == "apt"


@pytest.mark.asyncio
async def test_lxc_resolves_the_same_server_the_upgrade_would(data_dir):
    """OP#210: a check and an upgrade of one LXC must never target different
    servers, so both go through `_lxc_ssh_context` / `server_context`."""
    import app.update_scan as us

    client = MagicMock()
    client.get_lxc_updates = AsyncMock(return_value=[PKG])

    with (
        patch(
            "app.host_ops._lxc_ssh_context", return_value=("px.example", {"user": "root"})
        ) as ctx,
        patch("app.host_ops.server_context", return_value=({"url": "https://px"}, None)),
        patch("app.update_scan.client_from_config", return_value=client),
    ):
        result = await us.scan_host(LXC_HOST, {})

    ctx.assert_called_once_with(LXC_HOST)
    client.get_lxc_updates.assert_awaited_once_with(
        "pve", 101, "px.example", {"user": "root"}
    )
    assert result["packages"] == [PKG]
    assert result["proxmox_url"] == "https://px"
    assert "pct exec" in result["package_manager"]


@pytest.mark.asyncio
async def test_proxmox_node_reports_reboot_required(data_dir):
    import app.update_scan as us

    client = MagicMock()
    client.get_node_updates = AsyncMock(return_value=[PKG])

    with (
        patch("app.update_scan.client_from_config", return_value=client),
        patch("app.update_scan.reboot_required_typed", new=AsyncMock(return_value=True)),
        patch("app.update_scan.get_proxmox_config", return_value={"url": "https://px"}),
    ):
        result = await us.scan_host(NODE_HOST, {})

    assert result["packages"] == [PKG]
    assert result["reboot_required"] is True
    assert result["is_proxmox_node"] is True


# ---------------------------------------------------------------------------
# scan_hosts — isolation and concurrency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_dead_host_does_not_stop_the_others(data_dir):
    """A backend or host failing its check must not prevent the rest."""
    import app.update_scan as us

    async def fake(host, creds):
        if host["slug"] == "web":
            raise OSError("connection refused")
        return {"packages": [], "reboot_required": False, "package_manager": "apt"}

    with (
        patch("app.update_scan.scan_host", new=fake),
        patch("app.update_scan.get_credentials", return_value={}),
    ):
        results = await us.scan_hosts([SSH_HOST, NODE_HOST])

    by_slug = {h["slug"]: r for h, r in results}
    assert isinstance(by_slug["web"], Exception)
    assert by_slug["pve"]["packages"] == []


@pytest.mark.asyncio
async def test_host_checks_are_concurrency_bounded(data_dir):
    """The job must not open an SSH connection to every host at once."""
    import app.update_scan as us

    live = 0
    peak = 0

    async def fake(host, creds):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.01)
        live -= 1
        return {"packages": [], "reboot_required": False, "package_manager": "apt"}

    hosts = [{"slug": f"h{i}", "name": f"h{i}", "host": "x"} for i in range(12)]

    with (
        patch("app.update_scan.scan_host", new=fake),
        patch("app.update_scan.get_credentials", return_value={}),
    ):
        await us.scan_hosts(hosts)

    assert peak <= us.MAX_CONCURRENT_HOST_CHECKS
    assert us.MAX_CONCURRENT_HOST_CHECKS < 12, "an unbounded limit is not a limit"


@pytest.mark.asyncio
async def test_a_message_less_host_failure_still_names_its_cause(data_dir, caplog):
    """`TimeoutError()` stringifies to "" — the recorded failure must not."""
    import logging

    import app.update_scan as us

    caplog.set_level(logging.WARNING)

    async def fake(host, creds):
        raise TimeoutError()

    with (
        patch("app.update_scan.scan_host", new=fake),
        patch("app.update_scan.get_credentials", return_value={}),
    ):
        results = await us.scan_hosts([SSH_HOST])

    assert "TimeoutError" in caplog.text
    assert isinstance(results[0][1], Exception)


# ---------------------------------------------------------------------------
# scan_containers
# ---------------------------------------------------------------------------


def _backend(key, result):
    b = MagicMock()
    b.BACKEND_KEY = key
    if isinstance(result, Exception):
        b.get_stacks_with_update_status = AsyncMock(side_effect=result)
    else:
        b.get_stacks_with_update_status = AsyncMock(return_value=result)
    return b


@pytest.mark.asyncio
async def test_container_scan_collects_stacks_and_names_failed_backends(data_dir):
    import app.update_scan as us

    ok = _backend("portainer", [{"name": "stack", "update_status": "update_available"}])
    bad = _backend("ssh", RuntimeError("docker is dead"))

    with (
        patch("app.update_scan.get_backends", return_value=[ok, bad]),
        patch("app.update_scan.get_hosts", return_value=[{"docker_mode": "compose"}]),
        patch("app.update_scan.get_dockerhub_creds", return_value={}),
        patch("app.update_scan.check_and_notify"),
    ):
        result = await us.scan_containers()

    assert len(result["stacks"]) == 1
    assert result["failed_backends"], "a failed backend must be reported, not hidden"


# ---------------------------------------------------------------------------
# run_scan — the job body
# ---------------------------------------------------------------------------


@pytest.fixture
def stores(data_dir, monkeypatch):
    """Point both persisted stores at the temp data dir."""
    import app.last_check as lc
    import app.update_notifier as un

    monkeypatch.setattr(lc, "_PATH", data_dir / "last_check.json")
    monkeypatch.setattr(un, "_PATH", data_dir / "notified_updates.json")
    return lc


@pytest.mark.asyncio
async def test_the_job_notifies_with_no_http_request_involved(stores):
    """The whole point of the story: notifications without a browser."""
    import app.update_scan as us

    sent = []

    async def fake_scan_host(host, creds):
        return {"packages": [PKG], "reboot_required": False, "package_manager": "apt"}

    with (
        patch("app.update_scan.get_hosts", return_value=[SSH_HOST]),
        patch("app.update_scan.get_credentials", return_value={}),
        patch("app.update_scan.scan_host", new=fake_scan_host),
        patch(
            "app.update_scan.scan_containers",
            new=AsyncMock(return_value={"stacks": [], "failed_backends": []}),
        ),
        patch("app.update_scan.notify", side_effect=lambda *a, **k: sent.append(a)),
    ):
        await us.run_scan()

    assert sent, "a host with pending updates fired no notification"
    assert "Web" in " ".join(str(a) for a in sent[0])


@pytest.mark.asyncio
async def test_the_job_records_when_it_checked(stores):
    import app.update_scan as us

    lc = stores

    async def fake_scan_host(host, creds):
        return {"packages": [], "reboot_required": False, "package_manager": "apt"}

    with (
        patch("app.update_scan.get_hosts", return_value=[SSH_HOST]),
        patch("app.update_scan.get_credentials", return_value={}),
        patch("app.update_scan.scan_host", new=fake_scan_host),
        patch(
            "app.update_scan.scan_containers",
            new=AsyncMock(return_value={"stacks": [], "failed_backends": []}),
        ),
        patch("app.update_scan.notify"),
    ):
        await us.run_scan()

    assert lc.oldest_host_check(["web"]) is not None
    assert lc.container_check() is not None


@pytest.mark.asyncio
async def test_a_failed_host_does_not_get_a_check_timestamp(stores):
    """Recording a time for a check that never completed would report freshness
    the dashboard does not have."""
    import app.update_scan as us

    lc = stores

    async def fake_scan_host(host, creds):
        raise OSError("unreachable")

    with (
        patch("app.update_scan.get_hosts", return_value=[SSH_HOST]),
        patch("app.update_scan.get_credentials", return_value={}),
        patch("app.update_scan.scan_host", new=fake_scan_host),
        patch(
            "app.update_scan.scan_containers",
            new=AsyncMock(return_value={"stacks": [], "failed_backends": []}),
        ),
        patch("app.update_scan.notify"),
    ):
        await us.run_scan()

    assert lc.oldest_host_check(["web"]) is None


# ---------------------------------------------------------------------------
# Timeouts — CLAUDE.md's QA rules, from OP#228
# ---------------------------------------------------------------------------


def test_the_host_check_carries_an_explicit_generous_timeout():
    """The job must not wait forever on a wedged host, nor give up so fast that
    a slow-but-working `apt-get update` is reported as a failure. Inheriting a
    default is a decision; this is the visible version of it."""
    import app.ssh_client as sc

    assert sc._CHECK_TIMEOUT >= 60, "a check budget under a minute will report lies"
    assert sc._CONNECT_TIMEOUT >= 10


@pytest.mark.asyncio
async def test_a_wedged_host_cannot_hold_the_whole_scan_open(data_dir, monkeypatch):
    """One host that never answers must not stop the scan from finishing.

    The mocked suite is otherwise blind to this: mocks answer instantly, which
    is exactly how OP#228's inherited 15-second redeploy timeout survived.
    """
    import app.update_scan as us

    async def wedged(host, creds):
        await asyncio.sleep(30)

    async def quick(host, creds):
        return {"packages": [], "reboot_required": False, "package_manager": "apt"}

    async def dispatch(host, creds):
        return await (wedged if host["slug"] == "web" else quick)(host, creds)

    with (
        patch("app.update_scan.scan_host", new=dispatch),
        patch("app.update_scan.get_credentials", return_value={}),
        patch("app.update_scan.HOST_SCAN_TIMEOUT", 0.05),
    ):
        results = await asyncio.wait_for(us.scan_hosts([SSH_HOST, NODE_HOST]), timeout=5)

    by_slug = {h["slug"]: r for h, r in results}
    assert isinstance(by_slug["web"], Exception)
    assert by_slug["pve"]["packages"] == []
