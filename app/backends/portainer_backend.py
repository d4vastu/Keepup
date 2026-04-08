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

    async def update_stack(self, ref: str) -> None:
        log.info("Portainer backend: triggering update for ref %s", ref)
        stack_id_str, endpoint_id_str = ref.split(":", 1)
        await self._client.update_stack(int(stack_id_str), int(endpoint_id_str))
