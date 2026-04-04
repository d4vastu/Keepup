"""
Checks whether a Docker image has a newer version available on its registry
by comparing the remote manifest digest against the local digest stored in
the image's RepoDigests field.
"""
import httpx

MANIFEST_ACCEPT = (
    "application/vnd.docker.distribution.manifest.list.v2+json,"
    "application/vnd.docker.distribution.manifest.v2+json,"
    "application/vnd.oci.image.index.v1+json,"
    "application/vnd.oci.image.manifest.v1+json"
)


def parse_image_ref(image: str) -> tuple[str, str, str]:
    """
    Returns (registry, repository, tag).

    Examples:
      "nginx"                          -> ("registry-1.docker.io", "library/nginx", "latest")
      "linuxserver/sonarr:latest"      -> ("registry-1.docker.io", "linuxserver/sonarr", "latest")
      "ghcr.io/linuxserver/sonarr:latest" -> ("ghcr.io", "linuxserver/sonarr", "latest")
      "lscr.io/linuxserver/sonarr:latest" -> ("lscr.io", "linuxserver/sonarr", "latest")
    """
    # Split off tag
    last_segment = image.rsplit("/", 1)[-1]
    if ":" in last_segment:
        image, tag = image.rsplit(":", 1)
    else:
        tag = "latest"

    parts = image.split("/")
    first = parts[0]

    # Explicit registry if first part contains a dot, colon, or is 'localhost'
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


async def _get_dockerhub_token(repo: str, creds: dict | None) -> str:
    params = {"service": "registry.docker.io", "scope": f"repository:{repo}:pull"}
    auth = (creds["username"], creds["token"]) if creds else None
    async with httpx.AsyncClient() as client:
        resp = await client.get("https://auth.docker.io/token", params=params, auth=auth)
        resp.raise_for_status()
        return resp.json()["token"]


async def get_remote_digest(image: str, dockerhub_creds: dict | None = None) -> str | None:
    """
    Returns the current remote manifest digest for an image tag, or None on failure.
    """
    try:
        registry, repo, tag = parse_image_ref(image)
        headers = {"Accept": MANIFEST_ACCEPT}

        if registry == "registry-1.docker.io":
            token = await _get_dockerhub_token(repo, dockerhub_creds)
            headers["Authorization"] = f"Bearer {token}"
            url = f"https://registry-1.docker.io/v2/{repo}/manifests/{tag}"
        elif registry in ("ghcr.io", "lscr.io"):
            url = f"https://{registry}/v2/{repo}/manifests/{tag}"
        else:
            return None  # Unsupported registry

        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.head(url, headers=headers, timeout=15)
            if resp.status_code == 200:
                return resp.headers.get("Docker-Content-Digest")
    except Exception:
        pass
    return None


async def check_image_update(
    image: str,
    local_digest: str | None,
    dockerhub_creds: dict | None = None,
) -> str:
    """
    Returns one of: "update_available", "up_to_date", "unknown"
    """
    if not local_digest:
        return "unknown"

    remote_digest = await get_remote_digest(image, dockerhub_creds)
    if remote_digest is None:
        return "unknown"

    return "update_available" if remote_digest != local_digest else "up_to_date"
