"""Tests for portainer_client.py."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.portainer_client import PortainerClient


@pytest.fixture
def client():
    return PortainerClient(url="https://portainer.test:9443", api_key="test-key")


ENDPOINTS = [
    {"Id": 1, "Name": "primary", "Type": 1},
    {"Id": 2, "Name": "agent", "Type": 2},
    {"Id": 3, "Name": "other", "Type": 3},  # should be filtered out
]

STACKS = [
    {"Id": 10, "Name": "sonarr", "EndpointId": 1, "Env": []},
    {"Id": 11, "Name": "radarr", "EndpointId": 1, "Env": []},
]

CONTAINERS = [
    {
        "Id": "c1",
        "Image": "linuxserver/sonarr:latest",
        "ImageID": "sha256:localsonarr",
        "Labels": {"com.docker.compose.project": "sonarr"},
    },
    {
        "Id": "c2",
        "Image": "linuxserver/radarr:latest",
        "ImageID": "sha256:localradarr",
        "Labels": {"com.docker.compose.project": "radarr"},
    },
]

IMAGE_INFO = {
    "RepoDigests": ["linuxserver/sonarr@sha256:currentdigest"],
}


# ---------------------------------------------------------------------------
# get / put wrappers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_raises_on_http_error(client):
    mock_resp = MagicMock()  # sync mock — raise_for_status is not awaited
    mock_resp.raise_for_status.side_effect = Exception("404")

    mock_http = MagicMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)
    mock_http.get = AsyncMock(return_value=mock_resp)

    with patch("app.portainer_client.httpx.AsyncClient", return_value=mock_http):
        with pytest.raises(Exception, match="404"):
            await client.get("/api/stacks")


# ---------------------------------------------------------------------------
# get_endpoints
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_endpoints_filters_non_docker(client):
    with patch.object(client, "get", new=AsyncMock(return_value=ENDPOINTS)):
        result = await client.get_endpoints()
    assert len(result) == 2
    assert all(e["Type"] in (1, 2) for e in result)


# ---------------------------------------------------------------------------
# get_stacks / get_stack_file
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_stacks(client):
    with patch.object(client, "get", new=AsyncMock(return_value=STACKS)):
        result = await client.get_stacks()
    assert len(result) == 2


@pytest.mark.asyncio
async def test_get_stack_file(client):
    with patch.object(client, "get", new=AsyncMock(return_value={"StackFileContent": "version: '3'"})):
        content = await client.get_stack_file(10)
    assert content == "version: '3'"


@pytest.mark.asyncio
async def test_get_stack_file_missing_key(client):
    with patch.object(client, "get", new=AsyncMock(return_value={})):
        content = await client.get_stack_file(10)
    assert content == ""


# ---------------------------------------------------------------------------
# update_stack
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_update_stack(client):
    stack_data = {"Id": 10, "Env": [{"name": "KEY", "value": "val"}]}
    put_mock = AsyncMock(return_value={"Id": 10})

    with patch.object(client, "get", new=AsyncMock(return_value=stack_data)), \
         patch.object(client, "get_stack_file", new=AsyncMock(return_value="version: '3'")), \
         patch.object(client, "put", new=put_mock):
        result = await client.update_stack(10, 1)

    assert result == {"Id": 10}
    put_call_kwargs = put_mock.call_args.kwargs
    assert put_call_kwargs["json"]["pullImage"] is True
    assert put_call_kwargs["json"]["prune"] is False


# ---------------------------------------------------------------------------
# get_stacks_with_update_status
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stacks_with_update_status_up_to_date(client):
    with patch.object(client, "get_endpoints", new=AsyncMock(return_value=ENDPOINTS[:2])), \
         patch.object(client, "get_stacks", new=AsyncMock(return_value=[STACKS[0]])), \
         patch.object(client, "_get_containers", new=AsyncMock(return_value=[CONTAINERS[0]])), \
         patch.object(client, "_get_image_info", new=AsyncMock(return_value=IMAGE_INFO)), \
         patch("app.portainer_client.check_image_update", new=AsyncMock(return_value="up_to_date")), \
         patch("app.portainer_client.extract_local_digest", return_value="sha256:current"):
        results = await client.get_stacks_with_update_status()

    assert len(results) == 1
    assert results[0]["update_status"] == "up_to_date"


@pytest.mark.asyncio
async def test_stacks_with_update_status_update_available(client):
    with patch.object(client, "get_endpoints", new=AsyncMock(return_value=ENDPOINTS[:1])), \
         patch.object(client, "get_stacks", new=AsyncMock(return_value=[STACKS[0]])), \
         patch.object(client, "_get_containers", new=AsyncMock(return_value=[CONTAINERS[0]])), \
         patch.object(client, "_get_image_info", new=AsyncMock(return_value=IMAGE_INFO)), \
         patch("app.portainer_client.check_image_update", new=AsyncMock(return_value="update_available")), \
         patch("app.portainer_client.extract_local_digest", return_value="sha256:old"):
        results = await client.get_stacks_with_update_status()

    assert results[0]["update_status"] == "update_available"


@pytest.mark.asyncio
async def test_stacks_with_mixed_status(client):
    containers = [
        {**CONTAINERS[0], "Id": "c1"},
        {**CONTAINERS[0], "Id": "c2", "Image": "linuxserver/sonarr:2.0"},
    ]
    statuses = ["update_available", "up_to_date"]
    call_count = {"n": 0}

    async def mock_check(image, local_digest, creds=None):
        result = statuses[call_count["n"] % len(statuses)]
        call_count["n"] += 1
        return result

    with patch.object(client, "get_endpoints", new=AsyncMock(return_value=ENDPOINTS[:1])), \
         patch.object(client, "get_stacks", new=AsyncMock(return_value=[STACKS[0]])), \
         patch.object(client, "_get_containers", new=AsyncMock(return_value=containers)), \
         patch.object(client, "_get_image_info", new=AsyncMock(return_value=IMAGE_INFO)), \
         patch("app.portainer_client.check_image_update", new=mock_check), \
         patch("app.portainer_client.extract_local_digest", return_value="sha256:x"):
        results = await client.get_stacks_with_update_status()

    assert results[0]["update_status"] == "mixed"


@pytest.mark.asyncio
async def test_stacks_unknown_when_no_containers(client):
    with patch.object(client, "get_endpoints", new=AsyncMock(return_value=ENDPOINTS[:1])), \
         patch.object(client, "get_stacks", new=AsyncMock(return_value=[STACKS[0]])), \
         patch.object(client, "_get_containers", new=AsyncMock(return_value=[])):
        results = await client.get_stacks_with_update_status()

    assert results[0]["update_status"] == "unknown"


@pytest.mark.asyncio
async def test_stacks_endpoint_container_fetch_failure(client):
    """Container fetch failure for an endpoint should not crash — uses empty list."""
    with patch.object(client, "get_endpoints", new=AsyncMock(return_value=ENDPOINTS[:1])), \
         patch.object(client, "get_stacks", new=AsyncMock(return_value=[STACKS[0]])), \
         patch.object(client, "_get_containers", new=AsyncMock(side_effect=Exception("timeout"))):
        results = await client.get_stacks_with_update_status()

    assert results[0]["update_status"] == "unknown"


@pytest.mark.asyncio
async def test_stacks_image_check_exception_gives_unknown(client):
    with patch.object(client, "get_endpoints", new=AsyncMock(return_value=ENDPOINTS[:1])), \
         patch.object(client, "get_stacks", new=AsyncMock(return_value=[STACKS[0]])), \
         patch.object(client, "_get_containers", new=AsyncMock(return_value=[CONTAINERS[0]])), \
         patch.object(client, "_get_image_info", new=AsyncMock(side_effect=Exception("err"))), \
         patch("app.portainer_client.extract_local_digest", return_value="sha256:x"):
        results = await client.get_stacks_with_update_status()

    assert results[0]["images"][0]["status"] == "unknown"


@pytest.mark.asyncio
async def test_stacks_skips_duplicate_images(client):
    """Two containers with the same image — should only check once."""
    containers = [
        {**CONTAINERS[0], "Id": "c1"},
        {**CONTAINERS[0], "Id": "c2"},  # same Image as c1
    ]
    check_mock = AsyncMock(return_value="up_to_date")
    with patch.object(client, "get_endpoints", new=AsyncMock(return_value=ENDPOINTS[:1])), \
         patch.object(client, "get_stacks", new=AsyncMock(return_value=[STACKS[0]])), \
         patch.object(client, "_get_containers", new=AsyncMock(return_value=containers)), \
         patch.object(client, "_get_image_info", new=AsyncMock(return_value=IMAGE_INFO)), \
         patch("app.portainer_client.check_image_update", new=check_mock), \
         patch("app.portainer_client.extract_local_digest", return_value="sha256:x"):
        results = await client.get_stacks_with_update_status()

    # Image was only checked once despite two containers
    assert check_mock.call_count == 1
    assert len(results[0]["images"]) == 1


@pytest.mark.asyncio
async def test_put_method(client):
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"ok": True}

    mock_http = MagicMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)
    mock_http.put = AsyncMock(return_value=mock_resp)

    with patch("app.portainer_client.httpx.AsyncClient", return_value=mock_http):
        result = await client.put("/api/stacks/1", json={"key": "val"})

    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_get_containers(client):
    with patch.object(client, "get", new=AsyncMock(return_value=[{"Id": "abc"}])):
        result = await client._get_containers(1)
    assert result == [{"Id": "abc"}]


@pytest.mark.asyncio
async def test_get_image_info(client):
    with patch.object(client, "get", new=AsyncMock(return_value={"RepoDigests": []})):
        result = await client._get_image_info(1, "sha256:abc")
    assert result == {"RepoDigests": []}


@pytest.mark.asyncio
async def test_stacks_sorted_by_endpoint_then_name(client):
    stacks = [
        {"Id": 1, "Name": "zoo", "EndpointId": 1, "Env": []},
        {"Id": 2, "Name": "app", "EndpointId": 1, "Env": []},
    ]
    with patch.object(client, "get_endpoints", new=AsyncMock(return_value=ENDPOINTS[:1])), \
         patch.object(client, "get_stacks", new=AsyncMock(return_value=stacks)), \
         patch.object(client, "_get_containers", new=AsyncMock(return_value=[])):
        results = await client.get_stacks_with_update_status()

    assert results[0]["name"] == "app"
    assert results[1]["name"] == "zoo"
