"""Tests for registry_client.py."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.registry_client import (
    _get_bearer_token_from_challenge,
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
# _get_bearer_token_from_challenge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bearer_token_from_challenge_success():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"token": "mytoken"}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    www_auth = 'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:owner/app:pull"'
    with patch("app.registry_client.httpx.AsyncClient", return_value=mock_client):
        token = await _get_bearer_token_from_challenge(www_auth)
    assert token == "mytoken"


@pytest.mark.asyncio
async def test_bearer_token_from_challenge_access_token_key():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"access_token": "accesstok"}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    www_auth = 'Bearer realm="https://auth.example.com/token",service="example.com"'
    with patch("app.registry_client.httpx.AsyncClient", return_value=mock_client):
        token = await _get_bearer_token_from_challenge(www_auth)
    assert token == "accesstok"


@pytest.mark.asyncio
async def test_bearer_token_from_challenge_no_realm():
    token = await _get_bearer_token_from_challenge('Bearer service="ghcr.io"')
    assert token is None


@pytest.mark.asyncio
async def test_bearer_token_from_challenge_exception():
    www_auth = 'Bearer realm="https://ghcr.io/token",service="ghcr.io"'
    with patch(
        "app.registry_client.httpx.AsyncClient", side_effect=Exception("network")
    ):
        token = await _get_bearer_token_from_challenge(www_auth)
    assert token is None


# ---------------------------------------------------------------------------
# get_remote_digest
# ---------------------------------------------------------------------------


def _make_mock_client(head_responses, get_response=None):
    """Build an AsyncClient mock with a sequence of head() responses."""
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.head = AsyncMock(side_effect=head_responses)
    if get_response is not None:
        mock_client.get = AsyncMock(return_value=get_response)
    return mock_client


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
async def test_get_remote_digest_ghcr_200():
    """ghcr.io returns 200 directly (no auth challenge needed)."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"Docker-Content-Digest": "sha256:ghcrdigest"}

    mock_client = _make_mock_client([mock_resp])
    with patch("app.registry_client.httpx.AsyncClient", return_value=mock_client):
        digest = await get_remote_digest("ghcr.io/owner/app:latest")

    assert digest == "sha256:ghcrdigest"


@pytest.mark.asyncio
async def test_get_remote_digest_ghcr_401_then_200():
    """ghcr.io returns 401 first; we fetch a token then retry successfully."""
    challenge_resp = MagicMock()
    challenge_resp.status_code = 401
    challenge_resp.headers = {
        "www-authenticate": 'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:owner/app:pull"'
    }

    ok_resp = MagicMock()
    ok_resp.status_code = 200
    ok_resp.headers = {"Docker-Content-Digest": "sha256:authed"}

    token_resp = MagicMock()
    token_resp.status_code = 200
    token_resp.json.return_value = {"token": "ghcrtoken"}

    mock_client = _make_mock_client([challenge_resp, ok_resp], get_response=token_resp)
    with patch("app.registry_client.httpx.AsyncClient", return_value=mock_client):
        digest = await get_remote_digest("ghcr.io/owner/app:latest")

    assert digest == "sha256:authed"


@pytest.mark.asyncio
async def test_get_remote_digest_401_no_token():
    """401 with no valid WWW-Authenticate challenge returns None."""
    challenge_resp = MagicMock()
    challenge_resp.status_code = 401
    challenge_resp.headers = {}

    mock_client = _make_mock_client([challenge_resp])
    with patch("app.registry_client.httpx.AsyncClient", return_value=mock_client):
        digest = await get_remote_digest("ghcr.io/owner/app:latest")

    assert digest is None


@pytest.mark.asyncio
async def test_get_remote_digest_other_registry_with_dot():
    """Any registry hostname containing a dot is attempted."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"Docker-Content-Digest": "sha256:quaydigest"}

    mock_client = _make_mock_client([mock_resp])
    with patch("app.registry_client.httpx.AsyncClient", return_value=mock_client):
        digest = await get_remote_digest("quay.io/prometheus/node-exporter:latest")

    assert digest == "sha256:quaydigest"


@pytest.mark.asyncio
async def test_get_remote_digest_no_dot_returns_none():
    """Registry without a dot (and not DockerHub) returns None without a network call."""
    digest = await get_remote_digest("localhost/myapp:latest")
    assert digest is None


@pytest.mark.asyncio
async def test_get_remote_digest_non_200_non_401_returns_none():
    mock_resp = MagicMock()
    mock_resp.status_code = 403
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
    with patch(
        "app.registry_client.httpx.AsyncClient", side_effect=Exception("network error")
    ):
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
    with patch(
        "app.registry_client.get_remote_digest", new=AsyncMock(return_value=None)
    ):
        result = await check_image_update("nginx:latest", local_digest="sha256:abc")
    assert result == "unknown"


@pytest.mark.asyncio
async def test_check_image_update_up_to_date():
    with patch(
        "app.registry_client.get_remote_digest",
        new=AsyncMock(return_value="sha256:abc"),
    ):
        result = await check_image_update("nginx:latest", local_digest="sha256:abc")
    assert result == "up_to_date"


@pytest.mark.asyncio
async def test_check_image_update_available():
    with patch(
        "app.registry_client.get_remote_digest",
        new=AsyncMock(return_value="sha256:new"),
    ):
        result = await check_image_update("nginx:latest", local_digest="sha256:old")
    assert result == "update_available"
