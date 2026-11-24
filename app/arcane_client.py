"""
Arcane API client.

Arcane (https://getarcane.app) manages Docker Compose projects on every host
through agents. Keepup reads update status from it and asks it to apply
updates, rather than duplicating that work over SSH. This module is a thin,
typed wrapper over the handful of endpoints Keepup needs:

  - list environments (the manager's own Docker plus each agent)
  - list projects in an environment
  - check a project's images for updates
  - pull and bring up a project (streamed NDJSON)

Authentication is an Arcane API key sent as ``X-API-Key``.
"""

import json
import logging

import httpx

from .httpx_client import make_client

log = logging.getLogger(__name__)

# Listing and per-project update checks are quick reads, but an update check
# asks the registry about every image in the project; some registries (lscr.io)
# take ~10 s per image. Generous enough not to flake, short enough that a dead
# Arcane does not stall the dashboard.
_READ_TIMEOUT = httpx.Timeout(connect=5, read=60, write=15, pool=30)

# Pull and up are long-running by nature: a big image pull can take minutes.
# OP#228 is what inheriting a short default does to a redeploy — reported as a
# failure while succeeding server-side. The read timeout is the gap allowed
# *between* streamed lines, and Arcane streams pull progress, so 15 minutes of
# silence means something is genuinely stuck.
DEPLOY_TIMEOUT = httpx.Timeout(connect=5, read=900, write=30, pool=30)


class ArcaneError(RuntimeError):
    """Arcane refused or failed an action, with the output captured so far."""

    def __init__(self, message: str, lines: list[str] | None = None):
        super().__init__(message)
        self.lines = list(lines or [])


class ArcaneClient:
    def __init__(self, url: str, api_key: str, verify_ssl: bool = True):
        self.base = url.rstrip("/")
        self._headers = {"X-API-Key": api_key}
        self._verify = verify_ssl

    def _client(self, timeout: httpx.Timeout) -> httpx.AsyncClient:
        return make_client(
            base_url=f"{self.base}/api",
            headers=self._headers,
            verify=self._verify,
            timeout=timeout,
        )

    async def _get_data(self, path: str):
        async with self._client(_READ_TIMEOUT) as c:
            resp = await c.get(path)
            _raise_for_status(resp)
            return resp.json().get("data")

    async def list_environments(self) -> list[dict]:
        """Every environment: id "0" is the manager's own Docker host."""
        return await self._get_data("/agents") or []

    async def list_projects(self, env_id: str) -> list[dict]:
        return await self._get_data(f"/agents/{env_id}/projects") or []

    async def check_project_updates(self, env_id: str, project_id: str) -> dict:
        """Ask Arcane to check the project's images now; returns its updateInfo."""
        async with self._client(_READ_TIMEOUT) as c:
            resp = await c.post(f"/agents/{env_id}/updater/projects/{project_id}/check")
            _raise_for_status(resp)
            return resp.json().get("data") or {}

    async def deploy(self, env_id: str, project_id: str, action: str) -> list[str]:
        """Run a streamed deploy action ("pull" or "up") and return its log lines.

        Arcane answers 200 and reports failure *inside* the stream as an
        ``{"error": ...}`` line, so a clean HTTP status proves nothing; the
        stream has to be read to the end and checked.
        """
        lines: list[str] = []
        done = False
        async with self._client(DEPLOY_TIMEOUT) as c:
            async with c.stream(
                "POST", f"/agents/{env_id}/projects/{project_id}/deploy",
                params={"action": action},
            ) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    _raise_for_status(resp)
                async for raw in resp.aiter_lines():
                    if not raw.strip():
                        continue
                    try:
                        event = json.loads(raw)
                    except ValueError:
                        event = None
                    if not isinstance(event, dict):
                        lines.append(raw)
                        continue
                    if event.get("error"):
                        lines.append(f"Error: {event['error']}")
                        raise ArcaneError(f"Arcane {action} failed: {event['error']}", lines)
                    if event.get("log"):
                        lines.append(str(event["log"]))
                    elif event.get("message"):
                        lines.append(str(event["message"]))
                    if event.get("done"):
                        done = True
        if not done:
            raise ArcaneError(
                f"Arcane {action} stream ended before reporting it was done", lines
            )
        return lines


def _raise_for_status(resp: httpx.Response) -> None:
    """Raise with Arcane's own explanation, not just the status code."""
    if resp.status_code < 400:
        return
    detail = ""
    try:
        body = resp.json()
        detail = body.get("detail") or body.get("title") or body.get("error") or ""
    except ValueError:
        detail = (resp.text or "").strip()[:200]
    raise ArcaneError(f"Arcane returned HTTP {resp.status_code}: {detail or resp.reason_phrase}")
