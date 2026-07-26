"""Tests for registry_client.py."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.registry_client import (
    DigestResult,
    _get_bearer_token_from_challenge,
    check_image_update,
    extract_local_digest,
    get_remote_digest,
    parse_image_ref,
    reason_label,
    resolve_image_ref,
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
# resolve_image_ref
# ---------------------------------------------------------------------------


def test_resolve_normal_ref_returned_unchanged():
    assert resolve_image_ref("linuxserver/calibre:latest", [], []) == (
        "linuxserver/calibre:latest"
    )


def test_resolve_bare_sha256_uses_first_repo_tag():
    resolved = resolve_image_ref(
        "sha256:a4cf2c928f",
        ["linuxserver/calibre:7.16", "linuxserver/calibre:latest"],
        [],
    )
    assert resolved == "linuxserver/calibre:7.16"


def test_resolve_bare_sha256_skips_none_repo_tag():
    resolved = resolve_image_ref(
        "sha256:a4cf2c928f",
        ["<none>:<none>", "linuxserver/calibre:latest"],
        [],
    )
    assert resolved == "linuxserver/calibre:latest"


def test_resolve_bare_sha256_falls_back_to_repo_digest_as_latest():
    resolved = resolve_image_ref(
        "sha256:a4cf2c928f",
        ["<none>:<none>"],
        ["linuxserver/calibre@sha256:deadbeef"],
    )
    assert resolved == "linuxserver/calibre:latest"


def test_resolve_bare_sha256_dangling_returns_none():
    assert resolve_image_ref("sha256:a4cf2c928f", [], []) is None


def test_resolve_bare_sha256_none_metadata_returns_none():
    assert resolve_image_ref("sha256:a4cf2c928f", None, None) is None


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
    with patch("app.registry_client.make_client", return_value=mock_client):
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
    with patch("app.registry_client.make_client", return_value=mock_client):
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
        "app.registry_client.make_client", side_effect=Exception("network")
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

    with patch("app.registry_client.make_client", return_value=mock_client):
        result = await get_remote_digest("nginx:latest")

    assert result.digest == "sha256:newdigest"


@pytest.mark.asyncio
async def test_get_remote_digest_ghcr_200():
    """ghcr.io returns 200 directly (no auth challenge needed)."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"Docker-Content-Digest": "sha256:ghcrdigest"}

    mock_client = _make_mock_client([mock_resp])
    with patch("app.registry_client.make_client", return_value=mock_client):
        result = await get_remote_digest("ghcr.io/owner/app:latest")

    assert result.digest == "sha256:ghcrdigest"


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
    with patch("app.registry_client.make_client", return_value=mock_client):
        result = await get_remote_digest("ghcr.io/owner/app:latest")

    assert result.digest == "sha256:authed"


@pytest.mark.asyncio
async def test_get_remote_digest_401_no_token():
    """401 with no valid WWW-Authenticate challenge returns None."""
    challenge_resp = MagicMock()
    challenge_resp.status_code = 401
    challenge_resp.headers = {}

    mock_client = _make_mock_client([challenge_resp])
    with patch("app.registry_client.make_client", return_value=mock_client):
        result = await get_remote_digest("ghcr.io/owner/app:latest")

    assert result.digest is None


@pytest.mark.asyncio
async def test_get_remote_digest_other_registry_with_dot():
    """Any registry hostname containing a dot is attempted."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"Docker-Content-Digest": "sha256:quaydigest"}

    mock_client = _make_mock_client([mock_resp])
    with patch("app.registry_client.make_client", return_value=mock_client):
        result = await get_remote_digest("quay.io/prometheus/node-exporter:latest")

    assert result.digest == "sha256:quaydigest"


@pytest.mark.asyncio
async def test_get_remote_digest_no_dot_returns_none():
    """Registry without a dot (and not DockerHub) returns None without a network call."""
    result = await get_remote_digest("localhost/myapp:latest")
    assert result.digest is None


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

    with patch("app.registry_client.make_client", return_value=mock_client):
        result = await get_remote_digest("nginx:latest")

    assert result.digest is None


@pytest.mark.asyncio
async def test_get_remote_digest_exception_returns_none():
    with patch(
        "app.registry_client.make_client", side_effect=Exception("network error")
    ):
        result = await get_remote_digest("nginx:latest")
    assert result.digest is None


# ---------------------------------------------------------------------------
# check_image_update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_image_update_no_local_digest():
    result = await check_image_update("nginx:latest", local_digest=None)
    assert result == ("unknown", "no_local_digest")


@pytest.mark.asyncio
async def test_check_image_update_remote_unavailable():
    with patch(
        "app.registry_client.get_remote_digest",
        new=AsyncMock(return_value=DigestResult(None, "unreachable")),
    ):
        result = await check_image_update("nginx:latest", local_digest="sha256:abc")
    assert result == ("unknown", "unreachable")


@pytest.mark.asyncio
async def test_check_image_update_up_to_date():
    with patch(
        "app.registry_client.get_remote_digest",
        new=AsyncMock(return_value=DigestResult("sha256:abc", None)),
    ):
        result = await check_image_update("nginx:latest", local_digest="sha256:abc")
    assert result == ("up_to_date", None)


@pytest.mark.asyncio
async def test_check_image_update_available():
    with patch(
        "app.registry_client.get_remote_digest",
        new=AsyncMock(return_value=DigestResult("sha256:new", None)),
    ):
        result = await check_image_update("nginx:latest", local_digest="sha256:old")
    assert result == ("update_available", None)


@pytest.mark.asyncio
async def test_check_image_update_none_image_is_unknown():
    result = await check_image_update(None, local_digest="sha256:abc")
    assert result == ("unknown", "unresolvable_image")


# ---------------------------------------------------------------------------
# failure reasons (OP#217)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_image_update_no_image_ref():
    result = await check_image_update("", "sha256:local")
    assert result == ("unknown", "unresolvable_image")


@pytest.mark.asyncio
async def test_get_remote_digest_dockerhub_rate_limited():
    """A 429 on the Docker Hub token fetch is reported as rate_limited."""
    response = MagicMock()
    response.status_code = 429
    error = httpx.HTTPStatusError("429", request=MagicMock(), response=response)

    token_resp = MagicMock()
    token_resp.raise_for_status = MagicMock(side_effect=error)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=token_resp)

    with patch("app.registry_client.make_client", return_value=mock_client):
        result = await get_remote_digest("nginx:latest")

    assert result.digest is None
    assert result.reason == "rate_limited"


@pytest.mark.asyncio
async def test_get_remote_digest_manifest_rate_limited():
    resp = MagicMock()
    resp.status_code = 429
    resp.headers = {}

    mock_client = _make_mock_client([resp])
    with patch("app.registry_client.make_client", return_value=mock_client):
        result = await get_remote_digest("ghcr.io/owner/app:latest")

    assert result.reason == "rate_limited"


@pytest.mark.asyncio
async def test_get_remote_digest_not_found():
    resp = MagicMock()
    resp.status_code = 404
    resp.headers = {}

    mock_client = _make_mock_client([resp])
    with patch("app.registry_client.make_client", return_value=mock_client):
        result = await get_remote_digest("ghcr.io/owner/app:latest")

    assert result.reason == "not_found"


@pytest.mark.asyncio
async def test_get_remote_digest_other_status_is_registry_error():
    resp = MagicMock()
    resp.status_code = 500
    resp.headers = {}

    mock_client = _make_mock_client([resp])
    with patch("app.registry_client.make_client", return_value=mock_client):
        result = await get_remote_digest("ghcr.io/owner/app:latest")

    assert result.reason == "registry_error"


@pytest.mark.asyncio
async def test_get_remote_digest_exception_is_unreachable():
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.head = AsyncMock(side_effect=OSError("dns failure"))

    with patch("app.registry_client.make_client", return_value=mock_client):
        result = await get_remote_digest("ghcr.io/owner/app:latest")

    assert result.reason == "unreachable"


@pytest.mark.asyncio
async def test_get_remote_digest_dotless_registry_unsupported():
    result = await get_remote_digest("localhost/owner/app:latest")
    assert result.reason == "unsupported_registry"


@pytest.mark.asyncio
async def test_get_remote_digest_401_no_challenge_is_auth_failed():
    challenge_resp = MagicMock()
    challenge_resp.status_code = 401
    challenge_resp.headers = {}

    mock_client = _make_mock_client([challenge_resp])
    with patch("app.registry_client.make_client", return_value=mock_client):
        result = await get_remote_digest("ghcr.io/owner/app:latest")

    assert result.reason == "auth_failed"


def test_reason_label_falls_back():
    assert reason_label("rate_limited") == "Rate limited"
    assert reason_label(None) == "Unknown"
    assert reason_label("something_new") == "Unknown"


@pytest.mark.asyncio
async def test_get_remote_digest_200_missing_digest_header_is_registry_error():
    """A 200 with no Docker-Content-Digest header is a malformed answer, not a
    match — the registry replied, so this is registry_error, not unreachable."""
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {}

    mock_client = _make_mock_client([resp])
    with patch("app.registry_client.make_client", return_value=mock_client):
        result = await get_remote_digest("ghcr.io/owner/app:latest")

    assert result.digest is None
    assert result.reason == "registry_error"


@pytest.mark.asyncio
async def test_get_remote_digest_dockerhub_token_shape_error_is_registry_error():
    """The Docker Hub token endpoint answered but the response shape is
    unexpected (e.g. no "token" key) — this is a registry_error, not a
    network fault, so it must not be mislabelled unreachable."""
    token_resp = MagicMock()
    token_resp.raise_for_status = MagicMock()
    token_resp.json.return_value = {}  # no "token" key -> KeyError

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=token_resp)

    with patch("app.registry_client.make_client", return_value=mock_client):
        result = await get_remote_digest("nginx:latest")

    assert result.digest is None
    assert result.reason == "registry_error"
