"""
Checks whether a Docker image has a newer version available on its registry
by comparing the remote manifest digest against the local digest stored in
the image's RepoDigests field. Also owns the coarse failure-reason taxonomy
(REASON_* constants, REASON_LABELS, reason_label()) used to explain why a
check came back "unknown" instead of a definitive up-to-date/update-available
answer (OP#217).
"""

import logging
import re
from typing import NamedTuple

import httpx

from .httpx_client import make_client

log = logging.getLogger(__name__)

MANIFEST_ACCEPT = (
    "application/vnd.docker.distribution.manifest.list.v2+json,"
    "application/vnd.docker.distribution.manifest.v2+json,"
    "application/vnd.oci.image.index.v1+json,"
    "application/vnd.oci.image.manifest.v1+json"
)

# Coarse reasons a container's update status is "unknown" (OP#217). The label
# map is the single source of display text — templates never branch on these.
REASON_UNRESOLVABLE_IMAGE = "unresolvable_image"
REASON_NO_LOCAL_DIGEST = "no_local_digest"
REASON_RATE_LIMITED = "rate_limited"
REASON_AUTH_FAILED = "auth_failed"
REASON_NOT_FOUND = "not_found"
REASON_UNSUPPORTED_REGISTRY = "unsupported_registry"
REASON_REGISTRY_ERROR = "registry_error"
REASON_UNREACHABLE = "unreachable"

REASON_LABELS = {
    REASON_UNRESOLVABLE_IMAGE: "Image not resolvable",
    REASON_NO_LOCAL_DIGEST: "Nothing to compare",
    REASON_RATE_LIMITED: "Rate limited",
    REASON_AUTH_FAILED: "Auth failed",
    REASON_NOT_FOUND: "Tag not found",
    REASON_UNSUPPORTED_REGISTRY: "Unsupported registry",
    REASON_REGISTRY_ERROR: "Registry error",
    REASON_UNREACHABLE: "Registry unreachable",
}


def reason_label(reason: str | None) -> str:
    """Display text for an unknown-status reason; "Unknown" when unrecognised."""
    return REASON_LABELS.get(reason or "", "Unknown")


class DigestResult(NamedTuple):
    """Remote digest lookup outcome. ``reason`` is set only when digest is None."""

    digest: str | None
    reason: str | None = None


class ImageCheck(NamedTuple):
    """Update check outcome. ``reason`` is set only when status is "unknown"."""

    status: str
    reason: str | None = None


def parse_image_ref(image: str) -> tuple[str, str, str]:
    """
    Returns (registry, repository, tag).

    Examples:
      "nginx"                          -> ("registry-1.docker.io", "library/nginx", "latest")
      "linuxserver/sonarr:latest"      -> ("registry-1.docker.io", "linuxserver/sonarr", "latest")
      "ghcr.io/linuxserver/sonarr:latest" -> ("ghcr.io", "linuxserver/sonarr", "latest")
      "lscr.io/linuxserver/sonarr:latest" -> ("lscr.io", "linuxserver/sonarr", "latest")
    """
    last_segment = image.rsplit("/", 1)[-1]
    if ":" in last_segment:
        image, tag = image.rsplit(":", 1)
    else:
        tag = "latest"

    parts = image.split("/")
    first = parts[0]

    if "." in first or ":" in first or first == "localhost":
        registry = first
        repo = "/".join(parts[1:])
    else:
        registry = "registry-1.docker.io"
        repo = f"library/{parts[0]}" if len(parts) == 1 else "/".join(parts)

    return registry, repo, tag


def extract_local_digest(repo_digests: list[str], image_name: str) -> str | None:
    """
    Pulls the sha256:... digest out of a RepoDigests entry.
    e.g. "linuxserver/sonarr@sha256:abc123" -> "sha256:abc123"
    """
    for entry in repo_digests:
        if "@sha256:" in entry:
            return "sha256:" + entry.split("@sha256:")[-1]
    return None


def resolve_image_ref(
    image: str,
    repo_tags: list[str] | None = None,
    repo_digests: list[str] | None = None,
) -> str | None:
    """
    Resolves a container's Image value to a real repo:tag suitable for a
    registry lookup, using image-inspect metadata.

    Docker / Portainer report a container's Image as a bare "sha256:<id>" once
    the tag it was started from is reassigned to a newer pull. Handing that bare
    digest to parse_image_ref yields a bogus registry-1.docker.io/library/sha256
    lookup that 401s, so the update is never surfaced (OP#215).

    A normal reference is returned unchanged. For a bare digest we recover the
    reference from, in order:
      1. the first real RepoTags entry (skipping "<none>:<none>"), else
      2. the repository from RepoDigests ("repo@sha256:..." -> "repo:latest").
    Returns None when nothing usable is available (a truly dangling image).
    """
    if not image.startswith("sha256:"):
        return image

    for entry in repo_tags or []:
        if entry and "<none>" not in entry:
            return entry

    for entry in repo_digests or []:
        if "@sha256:" in entry:
            repo = entry.split("@sha256:")[0]
            if repo and "<none>" not in repo:
                return f"{repo}:latest"

    return None


async def _get_dockerhub_token(repo: str, creds: dict | None) -> str:
    params = {"service": "registry.docker.io", "scope": f"repository:{repo}:pull"}
    auth = (creds["username"], creds["token"]) if creds else None
    async with make_client() as client:
        resp = await client.get(
            "https://auth.docker.io/token", params=params, auth=auth
        )
        resp.raise_for_status()
        return resp.json()["token"]


async def _get_bearer_token_from_challenge(www_authenticate: str) -> str | None:
    """Parse a WWW-Authenticate Bearer challenge and fetch an anonymous token."""
    realm_m = re.search(r'realm="([^"]+)"', www_authenticate)
    service_m = re.search(r'service="([^"]+)"', www_authenticate)
    scope_m = re.search(r'scope="([^"]+)"', www_authenticate)
    if not realm_m:
        return None
    params: dict[str, str] = {}
    if service_m:
        params["service"] = service_m.group(1)
    if scope_m:
        params["scope"] = scope_m.group(1)
    try:
        async with make_client() as client:
            resp = await client.get(realm_m.group(1), params=params)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("token") or data.get("access_token")
    except Exception as e:
        log.debug("Bearer token fetch failed: %s", e)
    return None


def _reason_for_status(status: int) -> str:
    """Map an HTTP status from a registry to a coarse unknown-status reason."""
    if status == 429:
        return REASON_RATE_LIMITED
    if status in (401, 403):
        return REASON_AUTH_FAILED
    if status == 404:
        return REASON_NOT_FOUND
    return REASON_REGISTRY_ERROR


def _log_check_failure(image: str, reason: str, message: str, *args) -> None:
    """Log a failed registry check — warning for actionable reasons someone
    would want to notice while filtering by level, debug otherwise."""
    level = log.warning if reason in (REASON_RATE_LIMITED, REASON_AUTH_FAILED) else log.debug
    level(message, *args)


async def get_remote_digest(
    image: str, dockerhub_creds: dict | None = None
) -> DigestResult:
    """
    Returns the current remote manifest digest for an image tag, plus the reason
    when the lookup failed. Handles DockerHub, ghcr.io, lscr.io, and any
    registry that uses Bearer auth challenges.
    """
    try:
        registry, repo, tag = parse_image_ref(image)
        headers = {"Accept": MANIFEST_ACCEPT}

        if registry == "registry-1.docker.io":
            try:
                token = await _get_dockerhub_token(repo, dockerhub_creds)
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                reason = _reason_for_status(status)
                _log_check_failure(
                    image,
                    reason,
                    "Docker Hub token fetch for %s returned HTTP %s",
                    image,
                    status,
                )
                return DigestResult(None, reason)
            headers["Authorization"] = f"Bearer {token}"
            url = f"https://registry-1.docker.io/v2/{repo}/manifests/{tag}"
        elif "." in registry:
            # Any other registry (ghcr.io, lscr.io, quay.io, cr.hotio.dev, etc.)
            url = f"https://{registry}/v2/{repo}/manifests/{tag}"
        else:
            return DigestResult(None, REASON_UNSUPPORTED_REGISTRY)

        async with make_client(follow_redirects=True) as client:
            resp = await client.head(url, headers=headers)

            # Handle Bearer auth challenge (ghcr.io, lscr.io, quay.io, etc.)
            if resp.status_code == 401:
                www_auth = resp.headers.get("www-authenticate", "")
                token = await _get_bearer_token_from_challenge(www_auth)
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                    resp = await client.head(url, headers=headers, timeout=15)
                else:
                    _log_check_failure(
                        image,
                        REASON_AUTH_FAILED,
                        "No bearer token for %s (401, no challenge): %s",
                        image,
                        www_auth,
                    )
                    return DigestResult(None, REASON_AUTH_FAILED)

            if resp.status_code == 200:
                digest = resp.headers.get("Docker-Content-Digest")
                if digest:
                    return DigestResult(digest, None)
                return DigestResult(None, REASON_REGISTRY_ERROR)

            reason = _reason_for_status(resp.status_code)
            _log_check_failure(
                image,
                reason,
                "Registry check for %s returned HTTP %s",
                image,
                resp.status_code,
            )
            return DigestResult(None, reason)

    except (httpx.HTTPError, OSError) as e:
        # The registry didn't answer at all — a real connectivity failure.
        log.debug("Registry check failed for %s: %s", image, e)
        return DigestResult(None, REASON_UNREACHABLE)
    except Exception as e:
        # The registry answered but something in our handling of that answer
        # broke (e.g. an unexpected token-response shape) — that's a registry
        # error, not a network fault, so don't mislabel it as "unreachable".
        log.debug("Registry check failed for %s: %s", image, e)
        return DigestResult(None, REASON_REGISTRY_ERROR)


async def check_image_update(
    image: str,
    local_digest: str | None,
    dockerhub_creds: dict | None = None,
) -> ImageCheck:
    """
    Returns an ImageCheck whose status is one of "update_available",
    "up_to_date", "unknown"; reason is set only for "unknown".
    """
    if not image:
        log.debug("No resolvable image reference — skipping registry check")
        return ImageCheck("unknown", REASON_UNRESOLVABLE_IMAGE)

    if not local_digest:
        log.debug("No local digest for %s — skipping registry check", image)
        return ImageCheck("unknown", REASON_NO_LOCAL_DIGEST)

    remote = await get_remote_digest(image, dockerhub_creds)
    if remote.digest is None:
        # get_remote_digest always sets a reason when digest is None; the
        # fallback is defensive belt-and-braces, not an expected path.
        return ImageCheck("unknown", remote.reason or REASON_REGISTRY_ERROR)

    status = "update_available" if remote.digest != local_digest else "up_to_date"
    return ImageCheck(status, None)
