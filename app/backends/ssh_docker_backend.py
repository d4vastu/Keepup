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
import shlex
from urllib.parse import quote, unquote

log = logging.getLogger(__name__)

from ..ssh_client import _connect
from ..registry_client import check_image_update, extract_local_digest
from ..credentials import get_credentials
from ..config_manager import get_hosts, get_ssh_config

# Ref formats (the slug-relative part after "{slug}/"):
#   Compose container:  "{project}:{container_name}"   (colon separator)
#   Standalone:         "~{container_name}"             (tilde prefix)
_STANDALONE_PREFIX = "~"


class SSHDockerBackend:
    BACKEND_KEY = "ssh"

    def _docker_hosts(self) -> list[dict]:
        return [h for h in get_hosts() if h.get("docker_mode")]

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
        """Return all Compose projects on the host (for admin discovery only)."""
        ssh_cfg = get_ssh_config()
        creds = get_credentials(host["slug"])
        try:
            async with await _connect(host, ssh_cfg, creds) as conn:
                result = await conn.run(
                    "docker compose ls --all --format json", check=False
                )
                if result.returncode != 0 or not result.stdout.strip():
                    return []
                rows = _parse_json_output(result.stdout)
                return [
                    {"name": r["Name"], "config_file": r.get("ConfigFiles", "")}
                    for r in rows
                    if r.get("Name")
                ]
        except Exception:
            return []

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
        allowed_projects: set[str] | None = None
        if docker_mode == "selected":
            allowed_projects = set(host.get("docker_stacks") or [])

        creds = get_credentials(slug)
        entries = []

        async with await _connect(host, ssh_cfg, creds) as conn:
            ps_result = await conn.run(
                "docker ps -a --format '{{json .}}'", check=False
            )
            containers = _parse_json_output(ps_result.stdout)

            for c in containers:
                labels = _parse_docker_ps_labels(c.get("Labels", "") or "")
                project = labels.get("com.docker.compose.project", "")
                raw_names = c.get("Names", "")
                container_name = raw_names.split(",")[0].lstrip("/").strip()
                image = c.get("Image", "")

                if not container_name or not image:
                    continue

                # In "selected" mode only include containers from chosen projects
                if allowed_projects is not None:
                    if not project or project not in allowed_projects:
                        continue

                status = await self._check_image_status(conn, image, dockerhub_creds)
                images = [{"name": image, "status": status}]

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
                        "update_status": status,
                        "images": images,
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
    ) -> str:
        try:
            inspect = await conn.run(
                f"docker image inspect {shlex.quote(image_name)} --format '{{{{json .RepoDigests}}}}'",
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

    async def _get_config_file(self, conn, project_name: str) -> str:
        result = await conn.run("docker compose ls --all --format json", check=False)
        rows = _parse_json_output(result.stdout)
        match = next((r for r in rows if r.get("Name") == project_name), None)
        return match.get("ConfigFiles", "") if match else ""

    # ------------------------------------------------------------------
    # Internals — updates
    # ------------------------------------------------------------------

    async def _update_compose_project(self, host: dict, project_name: str) -> None:
        slug = host["slug"]
        h = host.get("host", slug)
        log.info("Docker SSH: updating compose project %s on %s", project_name, h)
        ssh_cfg = get_ssh_config()
        creds = get_credentials(slug)
        async with await _connect(host, ssh_cfg, creds) as conn:
            config_file = await self._get_config_file(conn, project_name)
            args = f"-f {config_file}" if config_file else f"-p {shlex.quote(project_name)}"
            pull = await conn.run(f"docker compose {args} pull 2>&1", check=False)
            if pull.returncode != 0:
                log.error("Docker SSH: pull failed for %s on %s", project_name, h)
                raise RuntimeError(f"docker compose pull failed:\n{pull.stdout}")
            up = await conn.run(f"docker compose {args} up -d 2>&1", check=False)
            if up.returncode != 0:
                log.error("Docker SSH: up -d failed for %s on %s", project_name, h)
                raise RuntimeError(f"docker compose up -d failed:\n{up.stdout}")
        log.info("Docker SSH: %s on %s — compose update complete", project_name, h)

    async def _update_standalone_container(self, host: dict, container_name: str) -> None:
        slug = host["slug"]
        h = host.get("host", slug)
        log.info("Docker SSH: updating standalone container %s on %s", container_name, h)
        ssh_cfg = get_ssh_config()
        creds = get_credentials(slug)
        async with await _connect(host, ssh_cfg, creds) as conn:
            inspect_result = await conn.run(
                f"docker inspect {shlex.quote(container_name)}", check=False
            )
            if inspect_result.returncode != 0 or not inspect_result.stdout.strip():
                raise RuntimeError(f"Container {container_name!r} not found")
            inspect_data = json.loads(inspect_result.stdout)[0]

            image = inspect_data["Config"]["Image"]
            log.info("Docker SSH: pulling %s for container %s", image, container_name)
            pull = await conn.run(f"docker pull {shlex.quote(image)} 2>&1", check=False)
            if pull.returncode != 0:
                log.error("Docker SSH: pull failed for %s on %s", container_name, h)
                raise RuntimeError(f"docker pull failed:\n{pull.stdout}")

            log.info("Docker SSH: stopping and removing %s", container_name)
            await conn.run(f"docker stop {shlex.quote(container_name)}", check=False)
            await conn.run(f"docker rm {shlex.quote(container_name)}", check=False)

            run_cmd = _build_docker_run_cmd(inspect_data)
            log.info("Docker SSH: recreating %s", container_name)
            run = await conn.run(f"{run_cmd} 2>&1", check=False)
            if run.returncode != 0:
                log.error("Docker SSH: recreate failed for %s on %s", container_name, h)
                raise RuntimeError(f"docker run failed:\n{run.stdout}")

        log.info("Docker SSH: %s on %s — standalone container updated", container_name, h)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


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
