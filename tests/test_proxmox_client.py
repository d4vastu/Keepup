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
    cluster_data = [
        {"type": "qemu", "node": "pve", "vmid": 100, "name": "ubuntu-vm", "status": "running"},
        {"type": "lxc", "node": "pve", "vmid": 101, "name": "debian-ct", "status": "running"},
    ]

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = _make_response(cluster_data)
        result = await mock_client.discover_resources()

    assert len(result) == 2
    names = {r["name"] for r in result}
    assert "ubuntu-vm" in names
    assert "debian-ct" in names


@pytest.mark.asyncio
async def test_discover_resources_filters_unknown_types(mock_client):
    """Non-VM/LXC resource types (storage, node) are excluded."""
    cluster_data = [
        {"type": "qemu", "node": "pve", "vmid": 100, "name": "my-vm", "status": "running"},
        {"type": "storage", "node": "pve", "storage": "local"},
        {"type": "node", "node": "pve"},
    ]

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = _make_response(cluster_data)
        result = await mock_client.discover_resources()

    assert len(result) == 1
    assert result[0]["name"] == "my-vm"


@pytest.mark.asyncio
async def test_discover_resources_empty(mock_client):
    """Handles a cluster with no VMs or LXCs."""
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = _make_response([])
        result = await mock_client.discover_resources()

    assert result == []


@pytest.mark.asyncio
async def test_discover_resources_multiple_nodes(mock_client):
    """Resources from multiple nodes are all returned."""
    cluster_data = [
        {"type": "qemu", "node": "pve1", "vmid": 100, "name": "vm-1", "status": "running"},
        {"type": "lxc", "node": "pve2", "vmid": 200, "name": "ct-1", "status": "stopped"},
    ]

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = _make_response(cluster_data)
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

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = _make_response(cluster_data)
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

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = _make_response(cluster_data)
        result = await mock_client.discover_resources()

    assert result[0]["vmid"] == 100
    assert result[1]["vmid"] == 200
