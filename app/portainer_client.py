"""
Portainer API client.

Handles:
  - Listing endpoints (Docker hosts)
  - Listing stacks and their containers
  - Checking Docker image update status via registry digest comparison
  - Triggering stack pull + redeploy
"""

import asyncio
import logging


from .httpx_client import make_breaker_client
from .registry_client import (
    ImageCheck,
    REASON_ENDPOINT_UNREACHABLE,
    REASON_NO_CONTAINERS,
    REASON_REGISTRY_ERROR,
    check_image_update,
    extract_local_digest,
    resolve_image_ref,
)
from .self_identity import get_self_container_id, get_self_container_name

log = logging.getLogger(__name__)


def _rollup_unknown_reason(image_statuses: list[dict]) -> str | None:
    """The single reason shared by a stack's unknown images, else None.

    Two images that failed for different reasons give the stack no honest
    single answer, so it reports none rather than picking one (OP#217).
    """
    reasons = {
        r.get("reason")
        for r in image_statuses
        if r.get("status") == "unknown" and r.get("reason")
    }
    return reasons.pop() if len(reasons) == 1 else None


class PortainerClient:
    def __init__(self, url: str, api_key: str, pinned_cert_pem: str = ""):
        self.base = url.rstrip("/")
        self.headers = {"X-API-Key": api_key}
        self._pinned_cert_pem = pinned_cert_pem

    def _ssl_ctx(self):
        if self._pinned_cert_pem:
            from .cert_utils import build_pinned_ssl_ctx
            return build_pinned_ssl_ctx(self._pinned_cert_pem)
        return None

    def _client(self):
        return make_breaker_client(
            base_url=self.base,
            headers=self.headers,
            ssl_context=self._ssl_ctx(),
        )

    async def get(self, path: str) -> dict | list:
        async with self._client() as c:
            resp = await c.get(path)
            resp.raise_for_status()
            return resp.json()

    async def put(self, path: str, json: dict) -> dict:
        async with self._client() as c:
            resp = await c.put(path, json=json)
            resp.raise_for_status()
            return resp.json()

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

    async def get_endpoints(self) -> list[dict]:
        data = await self.get("/api/endpoints")
        # Docker environments: 1 = local, 2 = agent, 4 = edge agent
        # Skip Kubernetes (5, 6) and Azure ACI (3)
        endpoints = [e for e in data if e.get("Type") in (1, 2, 4)]
        log.info("Portainer: found %d endpoint(s)", len(endpoints))
        return endpoints

    # ------------------------------------------------------------------
    # Stacks
    # ------------------------------------------------------------------

    async def get_stacks(self) -> list[dict]:
        return await self.get("/api/stacks")

    async def get_stack_file(self, stack_id: int) -> str:
        data = await self.get(f"/api/stacks/{stack_id}/file")
        return data.get("StackFileContent", "")

    async def update_stack(self, stack_id: int, endpoint_id: int) -> dict:
        """Pull latest images and redeploy the stack."""
        # Safety net: refuse to redeploy a stack that contains the self-container.
        self_id = get_self_container_id()
        self_name = get_self_container_name()
        if self_id or self_name:
            try:
                containers = await self._get_containers(endpoint_id)
                stack_meta = await self.get(f"/api/stacks/{stack_id}")
                stack_name_lower = stack_meta.get("Name", "").lower()
                stack_containers = [
                    c for c in containers
                    if c.get("Labels", {}).get("com.docker.compose.project", "").lower()
                    == stack_name_lower
                ]
                id_match = self_id and any(
                    (c.get("Id", "") or "")[:12] == self_id for c in stack_containers
                )
                name_match = self_name and any(
                    self_name in [n.lstrip("/") for n in c.get("Names", [])]
                    for c in stack_containers
                )
                if id_match or name_match:
                    raise ValueError(
                        f"Self-update refused: Keepup is in Portainer stack {stack_id}"
                    )
            except ValueError:
                raise
            except Exception as exc:
                log.warning("Portainer: self-update check failed for stack %s — %s", stack_id, exc)

        # Fetch current stack definition
        stack = await self.get(f"/api/stacks/{stack_id}")
        stack_name = stack.get("Name", str(stack_id))
        log.info("Portainer: updating stack %s on endpoint %s", stack_name, endpoint_id)
        compose_content = await self.get_stack_file(stack_id)

        payload = {
            "stackFileContent": compose_content,
            "env": stack.get("Env", []),
            "prune": False,
            "pullImage": True,
        }
        result = await self.put(
            f"/api/stacks/{stack_id}?endpointId={endpoint_id}", json=payload
        )
        log.info("Portainer: stack %s update complete", stack_name)
        return result

    # ------------------------------------------------------------------
    # Image update checking
    # ------------------------------------------------------------------

    async def _get_containers(self, endpoint_id: int) -> list[dict]:
        data = await self.get(
            f"/api/endpoints/{endpoint_id}/docker/containers/json?all=1"
        )
        return data

    async def _get_image_info(self, endpoint_id: int, image_id: str) -> dict:
        data = await self.get(
            f"/api/endpoints/{endpoint_id}/docker/images/{image_id}/json"
        )
        return data

    async def get_stacks_with_update_status(
        self, dockerhub_creds: dict | None = None
    ) -> list[dict]:
        """
        Returns stacks enriched with image update status.

        Each stack dict gets:
          "endpoint_name": str
          "update_status": "update_available" | "up_to_date" | "unknown" | "mixed"
          "unknown_reason": coarse reason when update_status == "unknown", else None
          "images": [{"name": str, "status": str, "reason": str | None}, ...]
        """
        endpoints = await self.get_endpoints()
        endpoint_map = {e["Id"]: e["Name"] for e in endpoints}

        stacks = await self.get_stacks()
        log.info(
            "Portainer: checking %d stacks across %d endpoints",
            len(stacks),
            len(endpoints),
        )

        # Build endpoint -> containers mapping (one API call per endpoint)
        endpoint_containers: dict[int, list[dict]] = {}
        unreachable_endpoints: set[int] = set()
        for ep in endpoints:
            try:
                endpoint_containers[ep["Id"]] = await self._get_containers(ep["Id"])
            except Exception:
                endpoint_containers[ep["Id"]] = []
                unreachable_endpoints.add(ep["Id"])

        self_id = get_self_container_id()
        self_name = get_self_container_name()

        results = []
        for stack in stacks:
            stack_id = stack["Id"]
            endpoint_id = stack.get("EndpointId", 0)
            stack_name = stack.get("Name", "unknown")

            containers = endpoint_containers.get(endpoint_id, [])
            # Containers belonging to this stack via Compose label.
            # Docker Compose normalises project names to lowercase, but
            # Portainer stack names can be mixed-case — compare case-insensitively.
            stack_name_lower = stack_name.lower()
            stack_containers = [
                c
                for c in containers
                if c.get("Labels", {}).get("com.docker.compose.project", "").lower()
                == stack_name_lower
            ]

            # Skip the stack that contains the running Keepup container.
            # Check by container ID first; fall back to name when HOSTNAME is overridden.
            id_match = self_id and any(
                (c.get("Id", "") or "")[:12] == self_id for c in stack_containers
            )
            name_match = self_name and any(
                self_name in [n.lstrip("/") for n in c.get("Names", [])]
                for c in stack_containers
            )
            if id_match or name_match:
                endpoint_name = endpoint_map.get(endpoint_id, f"env-{endpoint_id}")
                log.info(
                    "Portainer: excluding self-stack %s on %s from discovery",
                    stack_name, endpoint_name,
                )
                continue

            # Check each unique image in this stack
            seen_images: set[str] = set()
            image_statuses = []

            async def _check(container: dict) -> dict:
                img_name = container.get("Image", "")
                img_id = container.get("ImageID", "")
                if not img_name or img_name in seen_images:
                    return None
                seen_images.add(img_name)

                try:
                    img_info = await self._get_image_info(endpoint_id, img_id)
                    repo_digests = img_info.get("RepoDigests", [])
                    repo_tags = img_info.get("RepoTags", [])
                    local_digest = extract_local_digest(repo_digests, img_name)
                    resolved = resolve_image_ref(img_name, repo_tags, repo_digests)
                    check = await check_image_update(
                        resolved, local_digest, dockerhub_creds
                    )
                except Exception as exc:
                    log.warning(
                        "Portainer: image check failed for %s — %s", img_name, exc
                    )
                    check = ImageCheck("unknown", REASON_REGISTRY_ERROR)

                return {
                    "name": img_name,
                    "status": check.status,
                    "reason": check.reason if check.status == "unknown" else None,
                }

            tasks = [_check(c) for c in stack_containers]
            checked = await asyncio.gather(*tasks)
            image_statuses = [r for r in checked if r is not None]

            # Roll up to a single status for the stack
            statuses = {r["status"] for r in image_statuses}
            if not image_statuses:
                # Nothing was checked at all. Say which of the two very
                # different causes it was rather than showing a bare "Unknown".
                rollup = "unknown"
                unknown_reason = (
                    REASON_ENDPOINT_UNREACHABLE
                    if endpoint_id in unreachable_endpoints
                    else REASON_NO_CONTAINERS
                )
            else:
                if "update_available" in statuses and len(statuses) == 1:
                    rollup = "update_available"
                elif "update_available" in statuses:
                    rollup = "mixed"
                elif statuses == {"up_to_date"}:
                    rollup = "up_to_date"
                else:
                    rollup = "unknown"

                unknown_reason = (
                    _rollup_unknown_reason(image_statuses)
                    if rollup == "unknown"
                    else None
                )

            if rollup in ("update_available", "mixed"):
                endpoint_name = endpoint_map.get(endpoint_id, f"env-{endpoint_id}")
                log.info(
                    "Portainer: %s on %s — update available", stack_name, endpoint_name
                )

            results.append(
                {
                    "id": stack_id,
                    "name": stack_name,
                    "endpoint_id": endpoint_id,
                    "endpoint_name": endpoint_map.get(
                        endpoint_id, f"env-{endpoint_id}"
                    ),
                    "update_status": rollup,
                    "unknown_reason": unknown_reason,
                    "images": image_statuses,
                }
            )

        return sorted(results, key=lambda s: (s["endpoint_name"], s["name"]))
