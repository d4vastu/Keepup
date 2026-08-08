import logging

from ..portainer_client import PortainerClient

log = logging.getLogger(__name__)


class PortainerBackend:
    BACKEND_KEY = "portainer"

    def __init__(self, client: PortainerClient):
        self._client = client

    def _make_ref(self, stack_id: int, endpoint_id: int) -> str:
        return f"{stack_id}:{endpoint_id}"

    async def get_stacks_with_update_status(
        self, dockerhub_creds: dict | None = None
    ) -> list[dict]:
        raw = await self._client.get_stacks_with_update_status(dockerhub_creds)
        enriched = []
        for s in raw:
            s = dict(s)
            ref = self._make_ref(s["id"], s["endpoint_id"])
            s["id"] = str(s["id"])
            s["endpoint_id"] = str(s["endpoint_id"])
            s["update_path"] = f"{self.BACKEND_KEY}/{ref}"
            enriched.append(s)
        updates_found = sum(
            1 for s in enriched if s.get("update_status") in ("update_available", "mixed")
        )
        if updates_found:
            log.warning(
                "Portainer backend: %d stack(s) with updates found", updates_found
            )
        return enriched

    async def describe_ref(self, ref: str) -> str:
        """Resolve ``{stack_id}:{endpoint_id}`` to the stack's name."""
        stack_id = ref.split(":", 1)[0]
        stack = await self._client.get(f"/api/stacks/{stack_id}")
        return (stack or {}).get("Name", "") or ref

    async def update_stack(self, ref: str) -> list[str]:
        log.info("Portainer backend: triggering update for ref %s", ref)
        stack_id_str, endpoint_id_str = ref.split(":", 1)
        stack_id, endpoint_id = int(stack_id_str), int(endpoint_id_str)
        lines = [f"Redeploying Portainer stack {stack_id} on endpoint {endpoint_id}…"]
        result = await self._client.update_stack(stack_id, endpoint_id)
        status = (result or {}).get("Status", "unknown")
        lines.append(
            f"Portainer accepted the redeploy for stack {stack_id} (status {status})."
        )
        # Said out loud so a reader comparing this record against an SSH one
        # knows the thinness is the API's, not a bug in the recording.
        lines.append(
            "Portainer does not return per-container pull output;"
            " check the stack's container logs for detail."
        )
        return lines
