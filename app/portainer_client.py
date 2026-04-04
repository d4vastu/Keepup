"""
Portainer API client.

Handles:
  - Listing endpoints (Docker hosts)
  - Listing stacks and their containers
  - Checking Docker image update status via registry digest comparison
  - Triggering stack pull + redeploy
"""
import asyncio
import httpx
from .registry_client import extract_local_digest, check_image_update


class PortainerClient:
    def __init__(self, url: str, api_key: str, verify_ssl: bool = False):
        self.base = url.rstrip("/")
        self.headers = {"X-API-Key": api_key}
        self.verify_ssl = verify_ssl

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base,
            headers=self.headers,
            verify=self.verify_ssl,
            timeout=30,
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
        # Filter to Docker environments only (type 1 = local Docker, 2 = agent)
        return [e for e in data if e.get("Type") in (1, 2)]

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
        # Fetch current stack definition
        stack = await self.get(f"/api/stacks/{stack_id}")
        compose_content = await self.get_stack_file(stack_id)

        payload = {
            "stackFileContent": compose_content,
            "env": stack.get("Env", []),
            "prune": False,
            "pullImage": True,
        }
        return await self.put(
            f"/api/stacks/{stack_id}?endpointId={endpoint_id}", json=payload
        )

    # ------------------------------------------------------------------
    # Image update checking
    # ------------------------------------------------------------------

    async def _get_containers(self, endpoint_id: int) -> list[dict]:
        data = await self.get(
            f"/api/endpoints/{endpoint_id}/docker/containers/json?all=false"
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
          "images": [{"name": str, "status": str}, ...]
        """
        endpoints = await self.get_endpoints()
        endpoint_map = {e["Id"]: e["Name"] for e in endpoints}

        stacks = await self.get_stacks()

        # Build endpoint -> containers mapping (one API call per endpoint)
        endpoint_containers: dict[int, list[dict]] = {}
        for ep in endpoints:
            try:
                endpoint_containers[ep["Id"]] = await self._get_containers(ep["Id"])
            except Exception:
                endpoint_containers[ep["Id"]] = []

        results = []
        for stack in stacks:
            stack_id = stack["Id"]
            endpoint_id = stack.get("EndpointId", 0)
            stack_name = stack.get("Name", "unknown")

            containers = endpoint_containers.get(endpoint_id, [])
            # Containers belonging to this stack via Compose label
            stack_containers = [
                c for c in containers
                if c.get("Labels", {}).get("com.docker.compose.project") == stack_name
            ]

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
                    local_digest = extract_local_digest(repo_digests, img_name)
                    status = await check_image_update(
                        img_name, local_digest, dockerhub_creds
                    )
                except Exception:
                    status = "unknown"

                return {"name": img_name, "status": status}

            tasks = [_check(c) for c in stack_containers]
            checked = await asyncio.gather(*tasks)
            image_statuses = [r for r in checked if r is not None]

            # Roll up to a single status for the stack
            statuses = {r["status"] for r in image_statuses}
            if "update_available" in statuses and len(statuses) == 1:
                rollup = "update_available"
            elif "update_available" in statuses:
                rollup = "mixed"
            elif statuses == {"up_to_date"}:
                rollup = "up_to_date"
            else:
                rollup = "unknown"

            results.append(
                {
                    "id": stack_id,
                    "name": stack_name,
                    "endpoint_id": endpoint_id,
                    "endpoint_name": endpoint_map.get(endpoint_id, f"env-{endpoint_id}"),
                    "update_status": rollup,
                    "images": image_statuses,
                }
            )

        return sorted(results, key=lambda s: (s["endpoint_name"], s["name"]))
