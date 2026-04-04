"""Tests for registry_client.py."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.registry_client import (
    check_image_update,
    extract_local_digest,
    get_remote_digest,
    parse_image_ref,
)


# ---------------------------------------------------------------------------
# parse_image_ref
# ---------------------------------------------------------------------------

def test_parse_bare_image():
    registry, repo, tag = parse_image_ref("nginx")
    assert registry == "registry-1.docker.io"
    assert repo == "library/nginx"
    assert tag == "latest"


def test_parse_image_with_tag():
    registry, repo, tag = parse_image_ref("nginx:1.25")
    assert registry == "registry-1.docker.io"
    assert repo == "library/nginx"
    assert tag == "1.25"


def test_parse_namespaced_image():
    registry, repo, tag = parse_image_ref("linuxserver/sonarr:latest")
    assert registry == "registry-1.docker.io"
    assert repo == "linuxserver/sonarr"
    assert tag == "latest"


def test_parse_ghcr_image():
    registry, repo, tag = parse_image_ref("ghcr.io/linuxserver/sonarr:latest")
    assert registry == "ghcr.io"
    assert repo == "linuxserver/sonarr"
    assert tag == "latest"


def test_parse_lscr_image():
    registry, repo, tag = parse_image_ref("lscr.io/linuxserver/radarr:latest")
    assert registry == "lscr.io"
    assert repo == "linuxserver/radarr"
    assert tag == "latest"


def test_parse_localhost_image():
    registry, repo, tag = parse_image_ref("localhost/myapp:dev")
    assert registry == "localhost"
    assert repo == "myapp"
    assert tag == "dev"


def test_parse_image_no_tag_defaults_to_latest():
    _, _, tag = parse_image_ref("ghcr.io/owner/repo")
    assert tag == "latest"


# ---------------------------------------------------------------------------
# extract_local_digest
# ---------------------------------------------------------------------------

def test_extract_local_digest_found():
    digests = ["linuxserver/sonarr@sha256:abc123def456"]
    result = extract_local_digest(digests, "linuxserver/sonarr")
    assert result == "sha256:abc123def456"


def test_extract_local_digest_multiple_entries():
    # Returns the first digest that contains @sha256:
    digests = [
        "other/image@sha256:zzz",
        "myrepo/app@sha256:deadbeef",
    ]
    result = extract_local_digest(digests, "myrepo/app")
    assert result == "sha256:zzz"


def test_extract_local_digest_empty_list():
    assert extract_local_digest([], "nginx") is None


def test_extract_local_digest_no_sha():
    assert extract_local_digest(["nginx:latest"], "nginx") is None


# ---------------------------------------------------------------------------
# get_remote_digest
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_remote_digest_dockerhub():
    mock_token_resp = MagicMock()
    mock_token_resp.raise_for_status = MagicMock()
    mock_token_resp.json.return_value = {"token": "testtoken"}

    mock_manifest_resp = MagicMock()
    mock_manifest_resp.status_code = 200
    mock_manifest_resp.headers = {"Docker-Content-Digest": "sha256:newdigest"}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_token_resp)
    mock_client.head = AsyncMock(return_value=mock_manifest_resp)

    with patch("app.registry_client.httpx.AsyncClient", return_value=mock_client):
        digest = await get_remote_digest("nginx:latest")

    assert digest == "sha256:newdigest"


@pytest.mark.asyncio
async def test_get_remote_digest_ghcr():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"Docker-Content-Digest": "sha256:ghcrdigest"}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.head = AsyncMock(return_value=mock_resp)

    with patch("app.registry_client.httpx.AsyncClient", return_value=mock_client):
        digest = await get_remote_digest("ghcr.io/owner/app:latest")

    assert digest == "sha256:ghcrdigest"


@pytest.mark.asyncio
async def test_get_remote_digest_unsupported_registry():
    digest = await get_remote_digest("myregistry.internal/app:latest")
    assert digest is None


@pytest.mark.asyncio
async def test_get_remote_digest_non_200_returns_none():
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.headers = {}

    mock_token_resp = MagicMock()
    mock_token_resp.raise_for_status = MagicMock()
    mock_token_resp.json.return_value = {"token": "t"}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_token_resp)
    mock_client.head = AsyncMock(return_value=mock_resp)

    with patch("app.registry_client.httpx.AsyncClient", return_value=mock_client):
        digest = await get_remote_digest("nginx:latest")

    assert digest is None


@pytest.mark.asyncio
async def test_get_remote_digest_exception_returns_none():
    with patch("app.registry_client.httpx.AsyncClient", side_effect=Exception("network error")):
        digest = await get_remote_digest("nginx:latest")
    assert digest is None


# ---------------------------------------------------------------------------
# check_image_update
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_image_update_no_local_digest():
    result = await check_image_update("nginx:latest", local_digest=None)
    assert result == "unknown"


@pytest.mark.asyncio
async def test_check_image_update_remote_unavailable():
    with patch("app.registry_client.get_remote_digest", new=AsyncMock(return_value=None)):
        result = await check_image_update("nginx:latest", local_digest="sha256:abc")
    assert result == "unknown"


@pytest.mark.asyncio
async def test_check_image_update_up_to_date():
    with patch("app.registry_client.get_remote_digest", new=AsyncMock(return_value="sha256:abc")):
        result = await check_image_update("nginx:latest", local_digest="sha256:abc")
    assert result == "up_to_date"


@pytest.mark.asyncio
async def test_check_image_update_available():
    with patch("app.registry_client.get_remote_digest", new=AsyncMock(return_value="sha256:new")):
        result = await check_image_update("nginx:latest", local_digest="sha256:old")
    assert result == "update_available"
