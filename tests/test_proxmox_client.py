"""Tests for ProxmoxClient — connection and discovery logic."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import httpx


@pytest.fixture
def mock_client():
    """Return a ProxmoxClient with httpx calls mocked."""
    from app.proxmox_client import ProxmoxClient

    return ProxmoxClient(
        url="https://192.168.1.10:8006",
        api_token="user@pam!token=abc",
        verify_ssl=False,
    )


def _make_response(data, status_code=200):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = {"data": data}
    resp.raise_for_status = MagicMock()
    return resp


def _make_side_effect(cluster_data, nodes=None, lxc_by_node=None):
    """
    Build a fake_get side_effect that serves:
      1st call  → /cluster/resources  (cluster_data)
      2nd call  → /nodes              (nodes, default [{"node":"pve"}])
      3rd+ calls → /nodes/{name}/lxc  (lxc_by_node dict, default empty)
    """
    nodes = nodes or [{"node": "pve"}]
    lxc_by_node = lxc_by_node or {}
    calls = []

    async def fake_get(path, **kwargs):
        calls.append(path)
        if "cluster/resources" in path:
            return _make_response(cluster_data)
        if path.endswith("/nodes"):
            return _make_response(nodes)
        # /nodes/{name}/lxc
        for node_name, containers in lxc_by_node.items():
            if path.endswith(f"/{node_name}/lxc"):
                return _make_response(containers)
        return _make_response([])

    return fake_get


@pytest.mark.asyncio
async def test_get_version_returns_data(mock_client):
    version_data = {"version": "8.1.4", "release": "8"}
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = _make_response(version_data)
        result = await mock_client.get_version()
    assert result["version"] == "8.1.4"


@pytest.mark.asyncio
async def test_discover_resources_returns_vms_and_lxc(mock_client):
    cluster_data = [
        {"type": "qemu", "node": "pve", "vmid": 100, "name": "ubuntu-vm", "status": "running"},
        {"type": "lxc", "node": "pve", "vmid": 101, "name": "debian-ct", "status": "running"},
    ]
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock,
               side_effect=_make_side_effect(cluster_data)):
        result = await mock_client.discover_resources()

    assert len(result) == 2
    names = {r["name"] for r in result}
    assert "ubuntu-vm" in names
    assert "debian-ct" in names


@pytest.mark.asyncio
async def test_discover_resources_lxc_from_per_node_when_missing_from_cluster(mock_client):
    """LXCs absent from cluster/resources are picked up via per-node fallback."""
    cluster_data = [
        {"type": "qemu", "node": "pve", "vmid": 100, "name": "ubuntu-vm", "status": "running"},
    ]
    lxc_by_node = {
        "pve": [{"vmid": 201, "name": "web-ct", "status": "running"}],
    }
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock,
               side_effect=_make_side_effect(cluster_data, lxc_by_node=lxc_by_node)):
        result = await mock_client.discover_resources()

    assert len(result) == 2
    names = {r["name"] for r in result}
    assert "ubuntu-vm" in names
    assert "web-ct" in names
    assert next(r for r in result if r["name"] == "web-ct")["type"] == "lxc"


@pytest.mark.asyncio
async def test_discover_resources_no_duplicates_when_lxc_in_both_sources(mock_client):
    """An LXC that appears in cluster/resources is not duplicated by per-node fetch."""
    cluster_data = [
        {"type": "lxc", "node": "pve", "vmid": 101, "name": "debian-ct", "status": "running"},
    ]
    lxc_by_node = {
        "pve": [{"vmid": 101, "name": "debian-ct", "status": "running"}],
    }
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock,
               side_effect=_make_side_effect(cluster_data, lxc_by_node=lxc_by_node)):
        result = await mock_client.discover_resources()

    assert len(result) == 1


@pytest.mark.asyncio
async def test_discover_resources_filters_unknown_types(mock_client):
    """Non-VM/LXC resource types (storage, node) are excluded."""
    cluster_data = [
        {"type": "qemu", "node": "pve", "vmid": 100, "name": "my-vm", "status": "running"},
        {"type": "storage", "node": "pve", "storage": "local"},
        {"type": "node", "node": "pve"},
    ]
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock,
               side_effect=_make_side_effect(cluster_data)):
        result = await mock_client.discover_resources()

    assert len(result) == 1
    assert result[0]["name"] == "my-vm"


@pytest.mark.asyncio
async def test_discover_resources_empty(mock_client):
    """Handles a cluster with no VMs or LXCs."""
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock,
               side_effect=_make_side_effect([])):
        result = await mock_client.discover_resources()

    assert result == []


@pytest.mark.asyncio
async def test_discover_resources_multiple_nodes(mock_client):
    """Resources from multiple nodes are all returned."""
    cluster_data = [
        {"type": "qemu", "node": "pve1", "vmid": 100, "name": "vm-1", "status": "running"},
        {"type": "lxc", "node": "pve2", "vmid": 200, "name": "ct-1", "status": "stopped"},
    ]
    nodes = [{"node": "pve1"}, {"node": "pve2"}]
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock,
               side_effect=_make_side_effect(cluster_data, nodes=nodes)):
        result = await mock_client.discover_resources()

    assert len(result) == 2
    vmids = {r["vmid"] for r in result}
    assert 100 in vmids
    assert 200 in vmids


@pytest.mark.asyncio
async def test_discover_resources_uses_type_vmid_as_fallback_name(mock_client):
    """Items without a 'name' field fall back to '{type}-{vmid}'."""
    cluster_data = [
        {"type": "qemu", "node": "pve", "vmid": 101, "status": "running"},
        {"type": "lxc", "node": "pve", "vmid": 202, "status": "stopped"},
    ]
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock,
               side_effect=_make_side_effect(cluster_data)):
        result = await mock_client.discover_resources()

    names = {r["name"] for r in result}
    assert "qemu-101" in names
    assert "lxc-202" in names


@pytest.mark.asyncio
async def test_discover_resources_sorted_by_node_then_vmid(mock_client):
    """Results are sorted by node then vmid."""
    cluster_data = [
        {"type": "qemu", "node": "pve", "vmid": 200, "name": "b", "status": "running"},
        {"type": "lxc", "node": "pve", "vmid": 100, "name": "a", "status": "running"},
    ]
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock,
               side_effect=_make_side_effect(cluster_data)):
        result = await mock_client.discover_resources()

    assert result[0]["vmid"] == 100
    assert result[1]["vmid"] == 200


# ---------------------------------------------------------------------------
# get_version
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_version_returns_data(mock_client):
    resp = _make_response({"version": "8.1", "release": "8"})
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=resp):
        result = await mock_client.get_version()
    assert result["version"] == "8.1"


# ---------------------------------------------------------------------------
# get_node_updates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_node_updates_returns_packages(mock_client):
    packages = [
        {"Package": "curl", "OldVersion": "7.81", "Version": "7.90"},
        {"Package": "vim", "OldVersion": "9.0", "Version": "9.1"},
    ]
    resp = _make_response(packages)
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=resp):
        result = await mock_client.get_node_updates("pve")
    assert len(result) == 2
    assert result[0] == {"name": "curl", "current": "7.81", "available": "7.90"}
    assert result[1] == {"name": "vim", "current": "9.0", "available": "9.1"}


@pytest.mark.asyncio
async def test_get_node_updates_empty(mock_client):
    resp = _make_response([])
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=resp):
        result = await mock_client.get_node_updates("pve")
    assert result == []


# ---------------------------------------------------------------------------
# get_nodes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_nodes_returns_online(mock_client):
    resp = _make_response([
        {"node": "pve1", "status": "online"},
        {"node": "pve2", "status": "offline"},
        {"node": "pve3", "status": "online"},
    ])
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=resp):
        result = await mock_client.get_nodes()
    assert result == ["pve1", "pve3"]


@pytest.mark.asyncio
async def test_get_nodes_empty(mock_client):
    resp = _make_response([])
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=resp):
        result = await mock_client.get_nodes()
    assert result == []


# ---------------------------------------------------------------------------
# get_lxc_updates (pct exec path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_lxc_updates_no_credentials_raises(mock_client):
    from app.proxmox_client import ProxmoxClient
    with pytest.raises(RuntimeError, match="No SSH credentials"):
        await mock_client.get_lxc_updates(
            node="pve", vmid=100,
            ssh_host="192.168.1.10",
            ssh_cfg={},
            ssh_creds={},  # no key_path, no ssh_password
        )


@pytest.mark.asyncio
async def test_get_lxc_updates_parses_apt_output(mock_client):
    apt_output = (
        "Listing...\n"
        "curl/focal-security 7.90 amd64 [upgradable from: 7.81]\n"
        "vim/focal 9.1 amd64 [upgradable from: 9.0]\n"
    )
    mock_conn = MagicMock()
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=False)
    mock_result = MagicMock()
    mock_result.stdout = apt_output
    mock_conn.run = AsyncMock(return_value=mock_result)

    with patch("app.ssh_client._connect", new=AsyncMock(return_value=mock_conn)):
        result = await mock_client.get_lxc_updates(
            node="pve", vmid=100,
            ssh_host="192.168.1.10",
            ssh_cfg={},
            ssh_creds={"key_path": "/home/user/.ssh/id_rsa"},
        )

    assert len(result) == 2
    assert result[0] == {"name": "curl", "current": "7.81", "available": "7.90"}
    assert result[1] == {"name": "vim", "current": "9.0", "available": "9.1"}


@pytest.mark.asyncio
async def test_get_lxc_updates_no_packages(mock_client):
    mock_conn = MagicMock()
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=False)
    mock_result = MagicMock()
    mock_result.stdout = "Listing...\n"
    mock_conn.run = AsyncMock(return_value=mock_result)

    with patch("app.ssh_client._connect", new=AsyncMock(return_value=mock_conn)):
        result = await mock_client.get_lxc_updates(
            node="pve", vmid=100,
            ssh_host="192.168.1.10",
            ssh_cfg={},
            ssh_creds={"ssh_password": "secret"},
        )

    assert result == []


# ---------------------------------------------------------------------------
# upgrade_node
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upgrade_node_returns_log_lines(mock_client):
    """upgrade_node posts upgrade task, polls until stopped, returns log lines."""
    upid = "UPID:pve:00001234:00000001:65000000:aptupgrade:pve:root@pam:"

    post_resp = _make_response(upid)
    status_resp = _make_response({"status": "stopped", "exitstatus": "OK"})
    log_resp = _make_response([{"t": "Reading package lists..."}, {"t": "Done."}])

    async def fake_post(path, **kwargs):
        return post_resp

    async def fake_get(path, **kwargs):
        if "status" in path:
            return status_resp
        if "log" in path:
            return log_resp
        return _make_response({})

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=fake_post), \
         patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=fake_get), \
         patch("asyncio.sleep", new_callable=AsyncMock):
        result = await mock_client.upgrade_node("pve")

    assert result == ["Reading package lists...", "Done."]


@pytest.mark.asyncio
async def test_upgrade_node_raises_when_no_upid(mock_client):
    """upgrade_node raises RuntimeError when the API returns no task ID."""
    post_resp = _make_response(None)

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=post_resp):
        with pytest.raises(RuntimeError, match="did not return a task ID"):
            await mock_client.upgrade_node("pve")


@pytest.mark.asyncio
async def test_upgrade_node_logs_non_ok_exit_status(mock_client, caplog):
    """upgrade_node warns but does not raise when exitstatus is not OK."""
    import logging
    upid = "UPID:pve:abc"
    post_resp = _make_response(upid)
    status_resp = _make_response({"status": "stopped", "exitstatus": "1"})
    log_resp = _make_response([{"t": "error line"}])

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=post_resp), \
         patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=[status_resp, log_resp]), \
         patch("asyncio.sleep", new_callable=AsyncMock), \
         caplog.at_level(logging.WARNING, logger="app.proxmox_client"):
        result = await mock_client.upgrade_node("pve")

    assert "error line" in result
    assert any("1" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# _get_lxc_ip / _get_vm_ip (called during discover_resources)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discover_resources_fetches_lxc_ip(mock_client):
    """discover_resources calls _get_lxc_ip and populates the ip field."""
    cluster_data = [
        {"type": "lxc", "node": "pve", "vmid": 100, "name": "myct", "status": "running"},
    ]
    iface_resp = _make_response([
        {"name": "eth0", "inet": "192.168.5.50/24"},
    ])
    lxc_resp = _make_response([])  # per-node LXC list (already in cluster)

    call_count = [0]

    async def fake_get(path, **kwargs):
        call_count[0] += 1
        if "cluster/resources" in path:
            return _make_response(cluster_data)
        if path.endswith("/nodes"):
            return _make_response([{"node": "pve"}])
        if "lxc/100/interfaces" in path:
            return iface_resp
        return _make_response([])

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=fake_get):
        result = await mock_client.discover_resources()

    assert len(result) == 1
    assert result[0]["ip"] == "192.168.5.50"


@pytest.mark.asyncio
async def test_discover_resources_fetches_vm_ip(mock_client):
    """discover_resources calls _get_vm_ip via QEMU guest agent."""
    cluster_data = [
        {"type": "qemu", "node": "pve", "vmid": 200, "name": "myvm", "status": "running"},
    ]
    agent_resp = _make_response({
        "result": [
            {"name": "lo", "ip-addresses": [{"ip-address-type": "ipv4", "ip-address": "127.0.0.1"}]},
            {"name": "eth0", "ip-addresses": [{"ip-address-type": "ipv4", "ip-address": "10.0.0.5"}]},
        ]
    })

    async def fake_get(path, **kwargs):
        if "cluster/resources" in path:
            return _make_response(cluster_data)
        if path.endswith("/nodes"):
            return _make_response([{"node": "pve"}])
        if "agent/network-get-interfaces" in path:
            return agent_resp
        return _make_response([])

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=fake_get):
        result = await mock_client.discover_resources()

    assert result[0]["ip"] == "10.0.0.5"


@pytest.mark.asyncio
async def test_discover_resources_ip_fetch_failure_returns_empty(mock_client):
    """IP fetch failures are swallowed; ip field stays empty string."""
    cluster_data = [
        {"type": "lxc", "node": "pve", "vmid": 100, "name": "myct", "status": "running"},
    ]

    async def fake_get(path, **kwargs):
        if "cluster/resources" in path:
            return _make_response(cluster_data)
        if path.endswith("/nodes"):
            return _make_response([{"node": "pve"}])
        raise httpx.HTTPError("connection refused")

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=fake_get):
        result = await mock_client.discover_resources()

    assert result[0]["ip"] == ""


@pytest.mark.asyncio
async def test_discover_resources_cluster_failure_falls_back_to_node_lxc(mock_client):
    """When cluster/resources fails, per-node LXC list is used as fallback."""
    lxc_data = [{"vmid": 101, "name": "fallback-ct", "status": "running"}]

    call_count = {"cluster": 0}

    async def fake_get(path, **kwargs):
        if "cluster/resources" in path:
            raise httpx.HTTPError("forbidden")
        if path.endswith("/nodes"):
            return _make_response([{"node": "pve"}])
        if "/lxc" in path and "interfaces" not in path:
            return _make_response(lxc_data)
        return _make_response([])

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=fake_get):
        result = await mock_client.discover_resources()

    assert any(r["name"] == "fallback-ct" for r in result)
