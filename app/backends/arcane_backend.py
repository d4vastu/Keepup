"""
Arcane container backend.

Arcane already manages every Compose project on the hosts that run its agent:
it discovers the projects, checks their images, and can pull and redeploy them.
This backend makes Keepup a client of that work instead of a second copy of it.

  - Update status comes from Arcane's own per-project image check.
  - Applying an update asks Arcane to pull, then bring the project up — the same
    ``pull && up -d`` Keepup runs over SSH, but done by the tool that owns the
    project (so Arcane's history and Git backup see it).
  - Each project is filed under the Keepup host whose address matches the
    Arcane environment, and links back to the project in Arcane.

The SSH backend asks :meth:`ArcaneBackend.projects_by_host` which projects
Arcane covers on each host and leaves exactly those alone, so nothing is
checked or updated twice.
"""

import asyncio
import ipaddress
import json
import logging
import os
import re
import socket
import time
from pathlib import Path
from urllib.parse import urlparse

from ..activity_log import exc_text
from ..arcane_client import ArcaneClient, ArcaneError
from ..config_manager import get_hosts
from ..registry_client import (
    REASON_AUTH_FAILED,
    REASON_ENDPOINT_UNREACHABLE,
    REASON_NO_CONTAINERS,
    REASON_NO_LOCAL_DIGEST,
    REASON_NOT_FOUND,
    REASON_RATE_LIMITED,
    REASON_REGISTRY_ERROR,
)
from ..self_identity import get_self_container_id
from .protocol import StackUpdateError

log = logging.getLogger(__name__)

# The dashboard check and the SSH backend's "which projects are Arcane's?" both
# need the same listing within the same second; one fetch serves both.
_SNAPSHOT_TTL_SECONDS = 30

CONNECTION_BADGE = "Arcane"

# The last known "which projects are Arcane's" answer, kept on disk so an Arcane
# outage right after a Keepup restart does not hand every project to SSH.
_DATA_DIR = Path(os.getenv("DATA_PATH", "/app/data"))
_COVERAGE_PATH = _DATA_DIR / "arcane_coverage.json"

# Whole-word status codes and specific phrases only: a bare "429" also matches
# inside a sha256 digest, and "rate" inside "generate" or "separate".
_RATE_LIMITED = re.compile(r"\b429\b|rate[ -]?limit|too ?many ?requests")
_AUTH_FAILED = re.compile(r"\b40[13]\b|unauthori[sz]ed|forbidden|access denied|denied:")
_NOT_FOUND = re.compile(r"\b404\b|not found|manifest unknown")


def classify_error(error: str) -> str:
    """Map Arcane's free-text registry error onto Keepup's coarse reasons."""
    e = (error or "").lower()
    if _RATE_LIMITED.search(e):
        return REASON_RATE_LIMITED
    if _AUTH_FAILED.search(e):
        return REASON_AUTH_FAILED
    if _NOT_FOUND.search(e):
        return REASON_NOT_FOUND
    return REASON_REGISTRY_ERROR


def _load_coverage() -> dict[str, set[str]]:
    try:
        raw = json.loads(_COVERAGE_PATH.read_text())
        return {str(k): {str(n) for n in v} for k, v in raw.items()}
    except (OSError, ValueError, AttributeError, TypeError):
        return {}


def _save_coverage(coverage: dict[str, set[str]]) -> None:
    try:
        _COVERAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _COVERAGE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps({k: sorted(v) for k, v in coverage.items()}))
        os.replace(tmp, _COVERAGE_PATH)
    except OSError as e:
        log.warning("Arcane: could not save the project list (%s)", exc_text(e))


def image_statuses(update_info: dict | None) -> list[dict]:
    """One ``{name, status, reason, detail}`` per image Arcane knows about."""
    info = update_info or {}
    by_ref = info.get("updateInfoByRef") or {}
    refs = list(dict.fromkeys([*(info.get("imageRefs") or []), *by_ref.keys()]))
    images = []
    for ref in refs:
        u = by_ref.get(ref)
        if not u:
            images.append({
                "name": ref, "status": "unknown", "reason": REASON_NO_LOCAL_DIGEST,
                "detail": "Arcane has not checked this image yet",
            })
        elif u.get("error"):
            images.append({
                "name": ref, "status": "unknown",
                "reason": classify_error(u["error"]), "detail": u["error"],
            })
        elif u.get("hasUpdate"):
            images.append({"name": ref, "status": "update_available", "reason": None, "detail": ""})
        else:
            images.append({"name": ref, "status": "up_to_date", "reason": None, "detail": ""})
    return images


def rollup(images: list[dict]) -> tuple[str, str | None, str]:
    """(status, reason, detail) for a whole project from its images."""
    if not images:
        return "unknown", REASON_NO_CONTAINERS, "Arcane lists no images for this project"
    statuses = {i["status"] for i in images}
    if statuses == {"update_available"}:
        return "update_available", None, ""
    if "update_available" in statuses:
        return "mixed", None, ""
    if statuses == {"up_to_date"}:
        return "up_to_date", None, ""
    unknown = [i for i in images if i["status"] == "unknown"]
    reasons = {i["reason"] for i in unknown}
    reason = reasons.pop() if len(reasons) == 1 else None
    detail = "; ".join(f"{i['name']}: {i['detail']}" for i in unknown if i.get("detail"))
    return "unknown", reason, detail


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


async def _resolve(addr: str) -> str:
    """The address as an IP, so a hostname and an IP for one host compare equal."""
    if not addr or _is_ip(addr):
        return addr
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(addr, None, family=socket.AF_INET)
        return infos[0][4][0] if infos else addr
    except OSError:
        return addr


class ArcaneBackend:
    BACKEND_KEY = "arcane"

    def __init__(self, client: ArcaneClient):
        self._client = client
        self._snapshot: tuple[float, list] | None = None
        self._lock = asyncio.Lock()
        # Last good answer to "which projects are Arcane's?", kept through an
        # Arcane outage — and across restarts, via the data dir — so the SSH
        # backend does not suddenly start checking (and notifying about) every
        # Arcane project for the length of it.
        self._last_coverage: dict[str, set[str]] = _load_coverage()

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    async def _environments(self) -> list[tuple[dict, list[dict] | Exception]]:
        """[(environment, projects-or-the-error-listing-them)], briefly cached."""
        async with self._lock:
            now = time.monotonic()
            if self._snapshot and now - self._snapshot[0] < _SNAPSHOT_TTL_SECONDS:
                return self._snapshot[1]
            envs = await self._client.list_environments()
            projects = await asyncio.gather(
                *[self._client.list_projects(str(e["id"])) for e in envs],
                return_exceptions=True,
            )
            snapshot = list(zip(envs, projects))
            self._snapshot = (now, snapshot)
            return snapshot

    async def _env_address(self, env: dict) -> str:
        """The IP of the machine an environment's Docker runs on."""
        if str(env.get("id")) == "0":
            # The manager's own Docker: the machine Keepup reaches Arcane on.
            return await _resolve(urlparse(self._client.base).hostname or "")
        host = env.get("agentHost") or {}
        return await _resolve(host.get("publicIp") or "")

    async def _hosts_by_address(self) -> dict[str, dict]:
        hosts = get_hosts()
        addrs = await asyncio.gather(*[_resolve(h.get("host", "")) for h in hosts])
        return {a: h for a, h in zip(addrs, hosts) if a}

    def _app_url(self, snapshot) -> str:
        """Arcane's browser address, for links; the API URL if it has none."""
        for env, _ in snapshot:
            if str(env.get("id")) == "0":
                url = env.get("apiUrl") or ""
                if url.startswith(("http://", "https://")):
                    return url.rstrip("/")
        return self._client.base

    async def projects_by_host(self) -> dict[str, set[str]]:
        """{host IP: lowercased names of the Compose projects Arcane manages there}.

        Lowercased because Compose lowercases project names in its labels,
        while Arcane shows a project's name as written.
        """
        try:
            snapshot = await self._environments()
        except Exception as e:
            log.warning(
                "Arcane: could not list projects (%s); keeping the last known"
                " project list so the SSH backend does not take them over",
                exc_text(e),
            )
            return self._last_coverage
        coverage: dict[str, set[str]] = {}
        for env, projects in snapshot:
            if isinstance(projects, Exception):
                addr = await self._env_address(env)
                if addr in self._last_coverage:
                    coverage[addr] = self._last_coverage[addr]
                continue
            addr = await self._env_address(env)
            if addr:
                coverage.setdefault(addr, set()).update(
                    (p.get("name") or "").lower() for p in projects
                )
        if coverage != self._last_coverage:
            _save_coverage(coverage)
        self._last_coverage = coverage
        return coverage

    async def find_project_ref(self, host: dict, project_name: str) -> str | None:
        """The Arcane ref (``env:project``) for a Compose project on a Keepup host."""
        addr = await _resolve(host.get("host", ""))
        if not addr:
            return None
        for env, projects in await self._environments():
            if isinstance(projects, Exception) or await self._env_address(env) != addr:
                continue
            for p in projects:
                if (p.get("name") or "").lower() == project_name.lower():
                    return f"{env['id']}:{p['id']}"
        return None

    async def get_stacks_with_update_status(
        self, dockerhub_creds: dict | None = None
    ) -> list[dict]:
        snapshot = await self._environments()
        app_url = self._app_url(snapshot)
        hosts = await self._hosts_by_address()
        self_id = get_self_container_id()

        entries: list[dict] = []
        checks = []
        for env, projects in snapshot:
            env_id = str(env["id"])
            host = hosts.get(await self._env_address(env))
            endpoint_name = host["name"] if host else env.get("name", env_id)
            if isinstance(projects, Exception):
                entries.append(self._unreachable_entry(env, endpoint_name, projects))
                continue
            for p in projects:
                if _is_self_project(p, self_id):
                    continue
                checks.append((env_id, endpoint_name, p))

        results = await asyncio.gather(
            *[self._fresh_update_info(env_id, p) for env_id, _, p in checks]
        )
        for (env_id, endpoint_name, p), info in zip(checks, results):
            images = image_statuses(info)
            status, reason, detail = rollup(images)
            ref = f"{env_id}:{p['id']}"
            entries.append({
                "id": ref,
                "name": p.get("name", p["id"]),
                "endpoint_id": env_id,
                "endpoint_name": endpoint_name,
                "connection_badge": CONNECTION_BADGE,
                "update_status": status,
                "unknown_reason": reason,
                "unknown_detail": detail,
                "images": images,
                "update_path": f"{self.BACKEND_KEY}/{ref}",
                "arcane_url": f"{app_url}/projects/{p['id']}?env={env_id}",
            })

        updates = sum(1 for e in entries if e["update_status"] in ("update_available", "mixed"))
        log.info("Arcane: %d project(s), %d with updates", len(entries), updates)
        return sorted(entries, key=lambda s: (s["endpoint_name"], s["name"]))

    async def _fresh_update_info(self, env_id: str, project: dict) -> dict:
        """Arcane's live check, falling back to its last scheduled one."""
        try:
            return await self._client.check_project_updates(env_id, project["id"])
        except Exception as e:
            log.warning(
                "Arcane: live update check failed for %s (%s); using its last result",
                project.get("name", project["id"]), exc_text(e),
            )
            return project.get("updateInfo") or {}

    def _unreachable_entry(self, env: dict, endpoint_name: str, error: Exception) -> dict:
        """A visible row for an environment Arcane could not list (OP#216)."""
        env_id = str(env["id"])
        return {
            "id": f"{env_id}:*",
            "name": f"{env.get('name', env_id)} (Arcane environment)",
            "endpoint_id": env_id,
            "endpoint_name": endpoint_name,
            "connection_badge": CONNECTION_BADGE,
            "update_status": "unknown",
            "unknown_reason": REASON_ENDPOINT_UNREACHABLE,
            "unknown_detail": exc_text(error),
            "images": [],
            "update_path": f"{self.BACKEND_KEY}/{env_id}:*",
        }

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    async def describe_ref(self, ref: str) -> str:
        env_id, _, project_id = ref.partition(":")
        try:
            for p in await self._client.list_projects(env_id):
                if p.get("id") == project_id:
                    return p.get("name") or ref
        except Exception as e:
            log.warning("Arcane: could not name %s (%s)", ref, exc_text(e))
        return ref

    async def update_stack(self, ref: str) -> list[str]:
        env_id, _, project_id = ref.partition(":")
        if not env_id or not project_id or project_id == "*":
            raise StackUpdateError(f"Not an Arcane project reference: {ref!r}")
        lines = [f"Asking Arcane to pull the images for project {project_id} (environment {env_id})…"]
        try:
            lines += await self._client.deploy(env_id, project_id, "pull")
            lines.append("Asking Arcane to bring the project up with the new images…")
            lines += await self._client.deploy(env_id, project_id, "up")
        except ArcaneError as e:
            raise StackUpdateError(str(e), lines + e.lines) from e
        except Exception as e:
            raise StackUpdateError(f"Arcane update failed: {exc_text(e)}", lines) from e
        finally:
            self._snapshot = None  # the next check must see the new state
        lines.append("Arcane finished the update.")
        return lines


def _is_self_project(project: dict, self_id: str | None) -> bool:
    """True for the project Keepup itself runs in — never redeploy ourselves."""
    if not self_id:
        return False
    return any(
        (s.get("containerId") or "").startswith(self_id)
        for s in project.get("runtimeServices") or []
    )
