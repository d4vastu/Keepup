"""
SSH-based Docker backend.

Monitors every container on a remote host, mirroring Watchtower's model:
one entry per container, regardless of whether it's in a Compose stack.

Update strategy (per container):
  - Belongs to a Compose project → docker compose pull && up -d (whole project)
  - Standalone → docker pull + stop/rm/recreate from docker inspect config
"""

import asyncio
import json
import logging
import re
import shlex
from typing import Callable
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from ..ssh_client import _connect
from ..registry_client import check_image_update, extract_local_digest
from ..credentials import get_credentials, get_integration_credentials
from ..config_manager import get_hosts, get_ssh_config, get_proxmox_config, get_portainer_config, set_docker_monitoring
from ..self_identity import get_self_container_id

log = logging.getLogger(__name__)

# Ref formats (the slug-relative part after "{slug}/"):
#   Compose container:  "{project}:{container_name}"   (colon separator)
#   Standalone:         "~{container_name}"             (tilde prefix)
_STANDALONE_PREFIX = "~"


class SSHDockerBackend:
    BACKEND_KEY = "ssh"

    def _docker_hosts(self) -> list[dict]:
        return [h for h in get_hosts() if h.get("docker_mode")]

    def _connection_badge(self, host: dict) -> str:
        proxmox_node = host.get("proxmox_node")
        proxmox_vmid = host.get("proxmox_vmid")
        proxmox_type = host.get("proxmox_type") or "lxc"
        if proxmox_node and proxmox_vmid is None:
            return "Node · Proxmox API"
        elif proxmox_node and proxmox_type == "lxc":
            return f"LXC {proxmox_vmid} · pct exec"
        elif proxmox_node and proxmox_type == "vm":
            return f"VM {proxmox_vmid} · SSH"
        return "SSH"

    def _is_pct_host(self, host: dict) -> bool:
        """True when this host's docker daemon is reached via `pct exec` on a Proxmox node."""
        if not host.get("proxmox_node"):
            return False
        if host.get("proxmox_vmid") is None:
            return False
        return (host.get("proxmox_type") or "lxc") == "lxc"

    def _ssh_params_for(self, host: dict) -> tuple[dict, dict, Callable[[str], str]]:
        """
        Resolve `(host_entry, ssh_creds, wrap)` for Docker I/O against this host.

        For Proxmox LXCs, SSH targets the Proxmox node and every docker command
        is wrapped as `pct exec {vmid} -- sh -c '<cmd>'`. For everything else,
        the existing direct-SSH parameters are returned and `wrap` is identity.
        """
        if not self._is_pct_host(host):
            return host, get_credentials(host["slug"]), lambda c: c

        vmid = host["proxmox_vmid"]
        px_cfg = get_proxmox_config()
        px_creds = get_integration_credentials("proxmox")
        px_host = urlparse(px_cfg.get("url", "")).hostname or host.get("host", "")
        ssh_user = px_creds.get("ssh_user") or "root"
        ssh_key = px_creds.get("ssh_key") or ""
        ssh_password = px_creds.get("ssh_password") or ""

        host_entry: dict = {"host": px_host, "user": ssh_user, "port": 22}
        if ssh_key:
            if ".." in ssh_key or Path(ssh_key).is_absolute():
                raise ValueError(f"SSH key path escapes keys directory: {ssh_key!r}")
            _keys_dir = Path("/app/keys").resolve()
            _resolved = (_keys_dir / ssh_key).resolve()
            if not _resolved.is_relative_to(_keys_dir):
                raise ValueError(f"SSH key path escapes keys directory: {ssh_key!r}")
            host_entry["key"] = str(_resolved)
        ssh_creds: dict = {}
        if not ssh_key and ssh_password:
            ssh_creds["ssh_password"] = ssh_password

        def wrap(cmd: str) -> str:
            return f"pct exec {vmid} -- sh -c {shlex.quote(cmd)}"

        return host_entry, ssh_creds, wrap

    def _make_compose_ref(self, slug: str, project: str, container: str) -> str:
        return f"{slug}/{quote(project, safe='')}:{quote(container, safe='')}"

    def _make_standalone_ref(self, slug: str, container: str) -> str:
        return f"{slug}/{_STANDALONE_PREFIX}{quote(container, safe='')}"

    # Keep old name so any call sites that use _make_ref still work
    def _make_ref(self, host_slug: str, project_name: str) -> str:
        return f"{host_slug}/{quote(project_name, safe='')}"

    def _parse_ref(self, ref: str) -> tuple[str, str]:
        slug, encoded = ref.split("/", 1)
        return slug, unquote(encoded)

    # ------------------------------------------------------------------
    # Discovery (admin panel)
    # ------------------------------------------------------------------

    async def discover_stacks(self, host: dict) -> list[dict]:
        """Return all Compose projects on the host (for admin discovery only).

        Reads `docker ps -a` labels instead of `docker compose ls`, so
        hosts running legacy `docker-compose` v1 (no v2 plugin) are also
        supported. Also routes through `_ssh_params_for` so Proxmox LXC
        hosts are reached via `pct exec`.
        """
        ssh_cfg = get_ssh_config()
        host_entry, ssh_creds, wrap = self._ssh_params_for(host)
        try:
            async with await _connect(host_entry, ssh_cfg, ssh_creds) as conn:
                result = await conn.run(
                    wrap("docker ps -a --format '{{json .}}'"), check=False
                )
                if result.returncode != 0:
                    return []
                containers = _parse_json_output(result.stdout)
                return _compose_projects_from_ps(containers)
        except Exception:
            return []

    async def discover_containers(self, host: dict) -> list[dict]:
        """Return all containers on the host for admin container selection.

        Returns list of dicts: name, image, status, running (bool),
        compose_project (str | None), css_id (CSS-safe id string).
        Raises on connection failure — caller decides how to handle.
        Also routes through _ssh_params_for so Proxmox LXC hosts are
        reached via pct exec.
        """
        ssh_cfg = get_ssh_config()
        host_entry, ssh_creds, wrap = self._ssh_params_for(host)
        async with await _connect(host_entry, ssh_cfg, ssh_creds) as conn:
            result = await conn.run(
                wrap("docker ps -a --format '{{json .}}'"), check=False
            )
            if result.returncode != 0:
                return []
            raw = _parse_json_output(result.stdout)
            return _containers_for_display(raw)

    # ------------------------------------------------------------------
    # ContainerBackend protocol
    # ------------------------------------------------------------------

    async def get_stacks_with_update_status(
        self, dockerhub_creds: dict | None = None
    ) -> list[dict]:
        hosts = self._docker_hosts()
        ssh_cfg = get_ssh_config()
        tasks = [self._containers_for_host(h, ssh_cfg, dockerhub_creds) for h in hosts]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        all_entries = []
        for host, r in zip(hosts, results):
            if isinstance(r, Exception):
                if self._is_pct_host(host):
                    log.error(
                        "Docker SSH: pct exec failed for %s (node %s vmid %s) — %s",
                        host.get("host", host["slug"]),
                        host.get("proxmox_node"),
                        host.get("proxmox_vmid"),
                        r,
                    )
                else:
                    log.warning(
                        "Docker SSH: skipping %s — %s", host.get("host", host["slug"]), r
                    )
            elif isinstance(r, list):
                all_entries.extend(r)
        return sorted(all_entries, key=lambda s: (s["endpoint_name"], s["name"]))

    async def update_stack(self, ref: str) -> None:
        slug, rest = self._parse_ref(ref)
        host = next((h for h in self._docker_hosts() if h["slug"] == slug), None)
        if host is None:
            raise ValueError(f"No Docker-enabled host with slug {slug!r}")

        if rest.startswith(_STANDALONE_PREFIX):
            container_name = rest[len(_STANDALONE_PREFIX):]
            await self._update_standalone_container(host, container_name)
        elif ":" in rest:
            project_name, _container_name = rest.split(":", 1)
            await self._update_compose_project(host, project_name)
        else:
            # Backward-compat: old refs that were just project names
            await self._update_compose_project(host, rest)

    # ------------------------------------------------------------------
    # Internals — discovery
    # ------------------------------------------------------------------

    async def _containers_for_host(
        self,
        host: dict,
        ssh_cfg: dict,
        dockerhub_creds: dict | None,
    ) -> list[dict]:
        slug = host["slug"]
        h = host.get("host", slug)
        docker_mode = host.get("docker_mode", "all")

        # Resolve which containers are allowed in "selected" mode.
        # migration_stacks: set if we need to expand old docker_stacks → docker_containers.
        allowed_containers: set[str] | None = None
        migration_stacks: set[str] | None = None
        if docker_mode == "selected":
            if "docker_containers" in host:
                allowed_containers = set(host.get("docker_containers") or [])
            elif "docker_stacks" in host:
                migration_stacks = set(host["docker_stacks"])
            else:
                allowed_containers = set()

        host_entry, ssh_creds, wrap = self._ssh_params_for(host)
        entries = []

        async with await _connect(host_entry, ssh_cfg, ssh_creds) as conn:
            ps_result = await conn.run(
                wrap("docker ps -a --format '{{json .}}'"), check=False
            )
            containers = _parse_json_output(ps_result.stdout)
            portainer_projects = _portainer_managed_projects(containers)
            portainer_active = _portainer_integration_active()
            if portainer_projects and portainer_active:
                log.info(
                    "Docker SSH: %s — skipping %d Portainer-managed project(s): %s",
                    h, len(portainer_projects), sorted(portainer_projects),
                )
            elif portainer_projects and not portainer_active:
                log.info(
                    "Docker SSH: %s — Portainer agent detected but no Portainer integration"
                    " configured; including %d project(s) via SSH: %s",
                    h, len(portainer_projects), sorted(portainer_projects),
                )

            # Identify own container and its compose project (if any) so we can
            # exclude both the container itself and all siblings in the same project.
            self_id = get_self_container_id()
            self_projects: set[str] = set()
            if self_id:
                for c in containers:
                    if (c.get("ID", "") or "")[:12] == self_id:
                        labels = _parse_docker_ps_labels(c.get("Labels", "") or "")
                        project = labels.get("com.docker.compose.project", "")
                        name = (c.get("Names", "") or "").split(",")[0].lstrip("/").strip()
                        log.info(
                            "Docker SSH: %s — excluding self-container %s"
                            " (project=%r) from discovery",
                            h, name, project or "(standalone)",
                        )
                        if project:
                            self_projects.add(project)

            # First pass: collect ALL qualifying containers (no mode filter yet,
            # so migration can inspect the full set).
            raw_all: list[tuple[str, str, str]] = []  # (container_name, image, project)
            for c in containers:
                labels = _parse_docker_ps_labels(c.get("Labels", "") or "")
                project = labels.get("com.docker.compose.project", "")
                raw_names = c.get("Names", "")
                container_name = raw_names.split(",")[0].lstrip("/").strip()
                image = c.get("Image", "")

                if not container_name or not image:
                    continue

                # Skip self-container (by ID) and its entire compose project
                if self_id and (c.get("ID", "") or "")[:12] == self_id:
                    continue
                if project and project in self_projects:
                    continue

                # Skip compose projects managed by a Portainer agent — but only
                # when the Portainer integration is active; otherwise those
                # containers would vanish from the dashboard entirely.
                if project and project in portainer_projects and portainer_active:
                    continue

                raw_all.append((container_name, image, project))

            # One-shot migration: docker_stacks → docker_containers.
            # Expand selected project names to their current container names.
            if migration_stacks is not None:
                container_names = [
                    name for name, _, proj in raw_all if proj in migration_stacks
                ]
                set_docker_monitoring(slug, "selected", containers=container_names)
                log.info(
                    "Docker SSH: %s — migrated docker_stacks %s → %d container(s)",
                    h, sorted(migration_stacks), len(container_names),
                )
                allowed_containers = set(container_names)

            # Apply container-level filter for "selected" mode.
            if allowed_containers is not None:
                raw = [(n, img, proj) for n, img, proj in raw_all if n in allowed_containers]
            else:
                raw = raw_all

            # Check all unique images concurrently (deduplicates registry lookups)
            unique_images = list({image for _, image, _ in raw})
            statuses = await asyncio.gather(
                *[self._check_image_status(conn, img, dockerhub_creds, wrap) for img in unique_images],
                return_exceptions=True,
            )
            image_status: dict[str, str] = {
                img: (s if isinstance(s, str) else "unknown")
                for img, s in zip(unique_images, statuses)
            }

            for container_name, image, project in raw:
                status = image_status.get(image, "unknown")
                if project:
                    ref = self._make_compose_ref(slug, project, container_name)
                else:
                    ref = self._make_standalone_ref(slug, container_name)

                entries.append(
                    {
                        "id": ref,
                        "name": container_name,
                        "endpoint_id": slug,
                        "endpoint_name": host["name"],
                        "connection_badge": self._connection_badge(host),
                        "update_status": status,
                        "images": [{"name": image, "status": status}],
                        "update_path": f"{self.BACKEND_KEY}/{ref}",
                        "_compose_project": project,
                    }
                )

        n_compose = sum(1 for e in entries if e.get("_compose_project"))
        n_standalone = len(entries) - n_compose
        log.info(
            "Docker SSH: %s — %d compose container(s), %d standalone container(s)",
            h, n_compose, n_standalone,
        )
        return entries

    async def _check_image_status(
        self,
        conn,
        image_name: str,
        dockerhub_creds: dict | None,
        wrap: Callable[[str], str] | None = None,
    ) -> str:
        wrap = wrap or (lambda c: c)
        try:
            inspect = await conn.run(
                wrap(
                    f"docker image inspect {shlex.quote(image_name)} --format '{{{{json .RepoDigests}}}}'"
                ),
                check=False,
            )
            repo_digests: list[str] = []
            if inspect.returncode == 0:
                repo_digests = json.loads(inspect.stdout.strip())
            local_digest = extract_local_digest(repo_digests, image_name)
            return await check_image_update(image_name, local_digest, dockerhub_creds)
        except Exception as exc:
            log.warning("Docker SSH: image check failed for %s — %s", image_name, exc)
            return "unknown"

    # Kept for backward compatibility (used by discover_stacks path)
    async def _check_images(
        self,
        conn,
        project_name: str,
        dockerhub_creds: dict | None,
    ) -> list[dict]:
        ps_result = await conn.run(
            f"docker compose -p {shlex.quote(project_name)} ps --format json", check=False
        )
        containers = _parse_json_output(ps_result.stdout)
        seen: set[str] = set()
        tasks = []
        for c in containers:
            img = c.get("Image", "")
            if img and img not in seen:
                seen.add(img)
                tasks.append(self._check_image_status(conn, img, dockerhub_creds))
        checked = await asyncio.gather(*tasks, return_exceptions=True)
        return [{"name": img, "status": r} for img, r in zip(seen, checked) if isinstance(r, str)]

    async def _get_config_file(
        self,
        conn,
        project_name: str,
        wrap: Callable[[str], str] | None = None,
    ) -> str:
        """Look up the compose config file for a project via container labels.

        Version-agnostic: reads `com.docker.compose.project.config_files`
        from `docker ps -a` labels, which both compose v1 and v2 write.
        Relative paths (common on v1) are normalized against the
        `working_dir` label by `_compose_projects_from_ps`.
        """
        wrap = wrap or (lambda c: c)
        result = await conn.run(
            wrap("docker ps -a --format '{{json .}}'"), check=False
        )
        projects = _compose_projects_from_ps(_parse_json_output(result.stdout))
        match = next((p for p in projects if p["name"] == project_name), None)
        return match["config_file"] if match else ""

    async def _detect_compose_binary(
        self,
        conn,
        wrap: Callable[[str], str],
        host_label: str,
    ) -> str:
        """Return the compose binary command (`docker compose` or `docker-compose`).

        Raises RuntimeError if neither is available on the host.
        """
        result = await conn.run(wrap(_COMPOSE_PROBE), check=False)
        lines = (result.stdout or "").strip().splitlines()
        flavour = lines[-1].strip() if lines else ""
        if flavour == "v2":
            return "docker compose"
        if flavour == "v1":
            return "docker-compose"
        raise RuntimeError(
            f"Neither 'docker compose' (v2) nor 'docker-compose' (v1) "
            f"is available on {host_label}"
        )

    # ------------------------------------------------------------------
    # Internals — updates
    # ------------------------------------------------------------------

    async def _update_compose_project(self, host: dict, project_name: str) -> None:
        slug = host["slug"]
        h = host.get("host", slug)
        log.info("Docker SSH: updating compose project %s on %s", project_name, h)
        ssh_cfg = get_ssh_config()
        host_entry, ssh_creds, wrap = self._ssh_params_for(host)
        async with await _connect(host_entry, ssh_cfg, ssh_creds) as conn:
            # Safety net: refuse to update a project that contains the self-container.
            self_id = get_self_container_id()
            if self_id:
                ps = await conn.run(wrap("docker ps -a --format '{{json .}}'"), check=False)
                for c in _parse_json_output(ps.stdout):
                    if (c.get("ID", "") or "")[:12] == self_id:
                        labels = _parse_docker_ps_labels(c.get("Labels", "") or "")
                        if labels.get("com.docker.compose.project", "") == project_name:
                            raise ValueError(
                                f"Self-update refused: Keepup is part of"
                                f" compose project {project_name!r} on {h}"
                            )
            binary = await self._detect_compose_binary(conn, wrap, h)
            config_file = await self._get_config_file(conn, project_name, wrap)
            args = f"-f {config_file}" if config_file else f"-p {shlex.quote(project_name)}"
            pull = await conn.run(wrap(f"{binary} {args} pull 2>&1"), check=False)
            if pull.returncode != 0:
                log.error("Docker SSH: pull failed for %s on %s", project_name, h)
                raise RuntimeError(f"{binary} pull failed:\n{pull.stdout}")
            up = await conn.run(wrap(f"{binary} {args} up -d 2>&1"), check=False)
            if up.returncode != 0:
                log.error("Docker SSH: up -d failed for %s on %s", project_name, h)
                raise RuntimeError(f"{binary} up -d failed:\n{up.stdout}")
        log.info("Docker SSH: %s on %s — compose update complete", project_name, h)

    async def _update_standalone_container(self, host: dict, container_name: str) -> None:
        slug = host["slug"]
        h = host.get("host", slug)
        log.info("Docker SSH: updating standalone container %s on %s", container_name, h)
        ssh_cfg = get_ssh_config()
        host_entry, ssh_creds, wrap = self._ssh_params_for(host)
        async with await _connect(host_entry, ssh_cfg, ssh_creds) as conn:
            inspect_result = await conn.run(
                wrap(f"docker inspect {shlex.quote(container_name)}"), check=False
            )
            if inspect_result.returncode != 0 or not inspect_result.stdout.strip():
                raise RuntimeError(f"Container {container_name!r} not found")
            inspect_data = json.loads(inspect_result.stdout)[0]

            # Safety net: refuse to recreate the self-container.
            self_id = get_self_container_id()
            if self_id and (inspect_data.get("Id", "") or "")[:12] == self_id:
                raise ValueError(
                    f"Self-update refused: Keepup is standalone container"
                    f" {container_name!r} on {h}"
                )

            image = inspect_data["Config"]["Image"]
            log.info("Docker SSH: pulling %s for container %s", image, container_name)
            pull = await conn.run(wrap(f"docker pull {shlex.quote(image)} 2>&1"), check=False)
            if pull.returncode != 0:
                log.error("Docker SSH: pull failed for %s on %s", container_name, h)
                raise RuntimeError(f"docker pull failed:\n{pull.stdout}")

            log.info("Docker SSH: stopping and removing %s", container_name)
            await conn.run(wrap(f"docker stop {shlex.quote(container_name)}"), check=False)
            await conn.run(wrap(f"docker rm {shlex.quote(container_name)}"), check=False)

            run_cmd = _build_docker_run_cmd(inspect_data)
            log.info("Docker SSH: recreating %s", container_name)
            run = await conn.run(wrap(f"{run_cmd} 2>&1"), check=False)
            if run.returncode != 0:
                log.error("Docker SSH: recreate failed for %s on %s", container_name, h)
                raise RuntimeError(f"docker run failed:\n{run.stdout}")

        log.info("Docker SSH: %s on %s — standalone container updated", container_name, h)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


# One-shot probe that prints exactly one line: v2 | v1 | none
_COMPOSE_PROBE = (
    "docker compose version >/dev/null 2>&1 && echo v2 "
    "|| { docker-compose --version >/dev/null 2>&1 && echo v1 || echo none; }"
)


def _compose_projects_from_ps(containers: list[dict]) -> list[dict]:
    """
    Group `docker ps -a --format json` output by compose project.

    Returns `[{'name': project, 'config_file': path_or_empty}, ...]` using
    the `com.docker.compose.project` and `com.docker.compose.project.config_files`
    labels. Both compose v1 and v2 write these labels, so the result is
    the same whether the host runs the legacy standalone or the v2 plugin.

    v1 stores `config_files` as the literal value originally passed to the
    CLI — often a bare filename like `docker-compose.yaml`. When relative,
    it's joined with `com.docker.compose.project.working_dir` so the
    returned path is always absolute (v2 already normalizes this).
    """
    by_project: dict[str, str] = {}
    for c in containers:
        labels = _parse_docker_ps_labels(c.get("Labels", "") or "")
        name = labels.get("com.docker.compose.project", "")
        if not name:
            continue
        cf = labels.get(
            "com.docker.compose.project.config_files", ""
        ).split(",")[0].strip()
        if cf and not cf.startswith("/"):
            wd = labels.get(
                "com.docker.compose.project.working_dir", ""
            ).strip().rstrip("/")
            cf = f"{wd}/{cf}" if wd else ""
        if name not in by_project or (cf and not by_project[name]):
            by_project[name] = cf
    return [{"name": n, "config_file": f} for n, f in by_project.items()]


def _portainer_integration_active() -> bool:
    """Return True if both a Portainer URL and API key are configured."""
    cfg = get_portainer_config()
    creds = get_integration_credentials("portainer")
    return bool(cfg.get("url") and creds.get("api_key"))


def _portainer_managed_projects(containers: list[dict]) -> set[str]:
    """
    Return compose project names that are managed by a Portainer agent on this host.

    When a portainer/agent container is present, Portainer stores its compose
    files inside its own data volume (mounted at /data/compose/ by default),
    not on the host filesystem. Those projects cannot be updated via SSH and
    should be left to the Portainer backend.
    """
    has_agent = any(
        "portainer/agent" in (c.get("Image") or "").lower()
        for c in containers
    )
    if not has_agent:
        return set()

    excluded: set[str] = set()
    for c in containers:
        labels = _parse_docker_ps_labels(c.get("Labels", "") or "")
        project = labels.get("com.docker.compose.project", "")
        config_files = labels.get("com.docker.compose.project.config_files", "")
        if project and config_files.startswith("/data/compose/"):
            excluded.add(project)
    return excluded


def _parse_json_output(text: str) -> list[dict]:
    """Parse JSON from docker CLI — handles both array and NDJSON output."""
    text = text.strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            return [parsed]
    except json.JSONDecodeError:
        pass
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _parse_docker_ps_labels(labels_str: str) -> dict[str, str]:
    """Parse the comma-separated key=value Labels field from `docker ps --format json`."""
    result: dict[str, str] = {}
    for part in (labels_str or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            result[k.strip()] = v.strip()
    return result


def _css_safe_id(name: str) -> str:
    """Convert a string to a CSS-safe id: lowercase, only [a-z0-9-]."""
    s = name.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-") or "container"


def _containers_for_display(containers: list[dict]) -> list[dict]:
    """Parse docker ps -a NDJSON output into display-ready container dicts."""
    result = []
    for c in containers:
        labels = _parse_docker_ps_labels(c.get("Labels", "") or "")
        project = labels.get("com.docker.compose.project", "") or None
        name = (c.get("Names", "") or "").split(",")[0].lstrip("/").strip()
        image = c.get("Image", "")
        status = c.get("Status", "")
        if not name:
            continue
        result.append(
            {
                "name": name,
                "image": image,
                "status": status,
                "running": (status or "").lower().startswith("up"),
                "compose_project": project,
                "css_id": _css_safe_id(name),
            }
        )
    return result


def _rollup_status(images: list[dict]) -> str:
    statuses = {img["status"] for img in images}
    if not statuses:
        return "unknown"
    if statuses == {"update_available"}:
        return "update_available"
    if "update_available" in statuses:
        return "mixed"
    if statuses == {"up_to_date"}:
        return "up_to_date"
    return "unknown"


def _build_docker_run_cmd(inspect: dict) -> str:
    """
    Reconstruct a `docker run` command from `docker inspect` output.

    Mirrors Watchtower's GetCreateConfig / GetCreateHostConfig approach:
    reads HostConfig directly and maps fields to CLI flags.
    """
    parts = ["docker", "run", "-d"]

    name = inspect.get("Name", "").lstrip("/")
    if name:
        parts += ["--name", name]

    config = inspect.get("Config", {})
    host_config = inspect.get("HostConfig", {})
    network_settings = inspect.get("NetworkSettings", {})

    # Restart policy
    restart = host_config.get("RestartPolicy") or {}
    restart_name = restart.get("Name", "")
    if restart_name and restart_name != "no":
        max_retry = restart.get("MaximumRetryCount", 0)
        if restart_name == "on-failure" and max_retry:
            parts += ["--restart", f"on-failure:{max_retry}"]
        else:
            parts += ["--restart", restart_name]

    # Network mode
    network_mode = host_config.get("NetworkMode", "")
    if network_mode and network_mode not in ("default", "bridge", ""):
        parts += ["--network", network_mode]
    else:
        for net_name in (network_settings.get("Networks") or {}):
            if net_name not in ("bridge", "host", "none"):
                parts += ["--network", net_name]

    # Hostname
    hostname = config.get("Hostname", "")
    short_id = inspect.get("Id", "")[:12]
    if hostname and hostname != short_id and hostname != name:
        parts += ["--hostname", hostname]

    # Privileged
    if host_config.get("Privileged"):
        parts.append("--privileged")

    # Capabilities
    for cap in host_config.get("CapAdd") or []:
        parts += ["--cap-add", cap]
    for cap in host_config.get("CapDrop") or []:
        parts += ["--cap-drop", cap]

    # PID / IPC mode
    pid_mode = host_config.get("PidMode", "")
    if pid_mode:
        parts += ["--pid", pid_mode]
    ipc_mode = host_config.get("IpcMode", "")
    if ipc_mode and ipc_mode not in ("", "shareable", "private"):
        parts += ["--ipc", ipc_mode]

    # Environment variables
    for env in config.get("Env") or []:
        parts += ["-e", env]

    # Volume mounts
    for bind in host_config.get("Binds") or []:
        parts += ["-v", bind]

    # Tmpfs mounts
    for tmpfs_path in (host_config.get("Tmpfs") or {}):
        parts += ["--tmpfs", tmpfs_path]

    # Port bindings
    port_bindings = host_config.get("PortBindings") or {}
    for container_port, host_bindings in port_bindings.items():
        for hb in host_bindings or []:
            host_ip = hb.get("HostIp", "")
            host_port = hb.get("HostPort", "")
            if host_ip and host_ip not in ("0.0.0.0", "::"):
                parts += ["-p", f"{host_ip}:{host_port}:{container_port}"]
            elif host_port:
                parts += ["-p", f"{host_port}:{container_port}"]
            else:
                parts += ["-p", container_port]

    # Devices
    for device in host_config.get("Devices") or []:
        path_on_host = device.get("PathOnHost", "")
        path_in_container = device.get("PathInContainer", "")
        perms = device.get("CgroupPermissions", "rwm")
        if path_on_host and path_in_container:
            parts += ["--device", f"{path_on_host}:{path_in_container}:{perms}"]

    # Labels (skip internal Docker/Compose labels)
    _skip_prefixes = ("com.docker.compose.", "org.opencontainers.", "desktop.docker.")
    for key, val in (config.get("Labels") or {}).items():
        if not any(key.startswith(p) for p in _skip_prefixes):
            parts += ["-l", f"{key}={val}"]

    # DNS
    for dns in host_config.get("Dns") or []:
        parts += ["--dns", dns]

    # Extra hosts
    for host_entry in host_config.get("ExtraHosts") or []:
        parts += ["--add-host", host_entry]

    # Log driver
    log_config = host_config.get("LogConfig") or {}
    log_driver = log_config.get("Type", "")
    if log_driver and log_driver != "json-file":
        parts += ["--log-driver", log_driver]
        for lk, lv in (log_config.get("Config") or {}).items():
            parts += ["--log-opt", f"{lk}={lv}"]

    # Image
    parts.append(config.get("Image", ""))

    # Entrypoint + command overrides
    entrypoint = config.get("Entrypoint") or []
    cmd = config.get("Cmd") or []
    if entrypoint:
        parts += ["--entrypoint", entrypoint[0]]
        parts.extend(entrypoint[1:])
    parts.extend(cmd)

    return " ".join(shlex.quote(p) for p in parts)
