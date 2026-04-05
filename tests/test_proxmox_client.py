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


@pytest.mark.asyncio
async def test_get_version_returns_data(mock_client):
    version_data = {"version": "8.1.4", "release": "8"}
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = _make_response(version_data)
        result = await mock_client.get_version()
    assert result["version"] == "8.1.4"


@pytest.mark.asyncio
async def test_discover_resources_returns_vms_and_lxc(mock_client):
    nodes = [{"node": "pve"}]
    vms = [{"vmid": 100, "name": "ubuntu-vm", "status": "running"}]
    containers = [{"vmid": 101, "name": "debian-ct", "status": "running"}]

    responses = [
        _make_response(nodes),
        _make_response(vms),
        _make_response(containers),
    ]
    call_count = 0

    async def fake_get(path, **kwargs):
        nonlocal call_count
        resp = responses[call_count]
        call_count += 1
        return resp

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=fake_get):
        result = await mock_client.discover_resources()

    assert len(result) == 2
    names = {r["name"] for r in result}
    assert "ubuntu-vm" in names
    assert "debian-ct" in names


@pytest.mark.asyncio
async def test_discover_resources_handles_vm_failure_gracefully(mock_client):
    """If VM listing fails, LXC listing still works."""
    nodes = [{"node": "pve"}]
    containers = [{"vmid": 200, "name": "web-ct", "status": "stopped"}]

    call_count = 0

    async def fake_get(path, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _make_response(nodes)
        elif call_count == 2:
            raise httpx.ConnectError("timeout")
        else:
            return _make_response(containers)

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=fake_get):
        result = await mock_client.discover_resources()

    assert len(result) == 1
    assert result[0]["name"] == "web-ct"
    assert result[0]["type"] == "lxc"


@pytest.mark.asyncio
async def test_discover_resources_empty_node(mock_client):
    """Handles a node with no VMs or LXCs."""
    nodes = [{"node": "pve"}]

    call_count = 0

    async def fake_get(path, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _make_response(nodes)
        return _make_response([])  # empty VMs and LXCs

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=fake_get):
        result = await mock_client.discover_resources()

    assert result == []


@pytest.mark.asyncio
async def test_discover_resources_multiple_nodes(mock_client):
    """Resources from multiple nodes are all returned."""
    nodes = [{"node": "pve1"}, {"node": "pve2"}]
    vms_node1 = [{"vmid": 100, "name": "vm-1", "status": "running"}]
    vms_node2 = [{"vmid": 200, "name": "vm-2", "status": "stopped"}]

    call_count = 0

    async def fake_get(path, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _make_response(nodes)
        elif call_count == 2:
            return _make_response(vms_node1)
        elif call_count == 3:
            return _make_response([])  # no LXC on node1
        elif call_count == 4:
            return _make_response(vms_node2)
        else:
            return _make_response([])  # no LXC on node2

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=fake_get):
        result = await mock_client.discover_resources()

    assert len(result) == 2
    vmids = {r["vmid"] for r in result}
    assert 100 in vmids
    assert 200 in vmids


@pytest.mark.asyncio
async def test_discover_resources_uses_vmid_as_fallback_name(mock_client):
    """VMs/LXCs without a 'name' field fall back to 'vm-{vmid}'."""
    nodes = [{"node": "pve"}]
    vms = [{"vmid": 101, "status": "running"}]  # no 'name' key

    call_count = 0

    async def fake_get(path, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _make_response(nodes)
        elif call_count == 2:
            return _make_response(vms)
        return _make_response([])

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=fake_get):
        result = await mock_client.discover_resources()

    assert result[0]["name"] == "vm-101"
