"""
SSH-based Docker Compose backend.

Connects to hosts over SSH and uses the Docker CLI to discover and manage
Compose stacks. Requires Docker Compose v2 (docker compose subcommand).
"""
import asyncio
import json
from urllib.parse import quote, unquote

from ..ssh_client import _connect
from ..registry_client import check_image_update, extract_local_digest
from ..credentials import get_credentials
from ..config_manager import get_hosts, get_ssh_config


class SSHDockerBackend:
    BACKEND_KEY = "ssh"

    def _docker_hosts(self) -> list[dict]:
        return [h for h in get_hosts() if h.get("docker_mode")]

    def _make_ref(self, host_slug: str, project_name: str) -> str:
        return f"{host_slug}/{quote(project_name, safe='')}"

    def _parse_ref(self, ref: str) -> tuple[str, str]:
        slug, encoded = ref.split("/", 1)
        return slug, unquote(encoded)

    # ------------------------------------------------------------------
    # Discovery (also used by the admin panel when adding hosts)
    # ------------------------------------------------------------------

    async def discover_stacks(self, host: dict) -> list[dict]:
        """
        SSH into a host and return all Compose projects found.
        Returns a list of {"name": str, "config_file": str} dicts.
        Does not check for image updates — used purely for discovery.
        """
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
        tasks = [
            self._stacks_for_host(h, ssh_cfg, dockerhub_creds)
            for h in hosts
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        all_stacks = []
        for r in results:
            if isinstance(r, list):
                all_stacks.extend(r)
        return sorted(all_stacks, key=lambda s: (s["endpoint_name"], s["name"]))

    async def update_stack(self, ref: str) -> None:
        slug, project_name = self._parse_ref(ref)
        host = next(
            (h for h in self._docker_hosts() if h["slug"] == slug), None
        )
        if host is None:
            raise ValueError(f"No Docker-enabled host with slug {slug!r}")

        ssh_cfg = get_ssh_config()
        creds = get_credentials(slug)
        async with await _connect(host, ssh_cfg, creds) as conn:
            config_file = await self._get_config_file(conn, project_name)
            args = f"-f {config_file}" if config_file else f"-p {project_name}"
            pull = await conn.run(f"docker compose {args} pull 2>&1", check=False)
            if pull.returncode != 0:
                raise RuntimeError(f"docker compose pull failed:\n{pull.stdout}")
            up = await conn.run(f"docker compose {args} up -d 2>&1", check=False)
            if up.returncode != 0:
                raise RuntimeError(f"docker compose up -d failed:\n{up.stdout}")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _stacks_for_host(
        self,
        host: dict,
        ssh_cfg: dict,
        dockerhub_creds: dict | None,
    ) -> list[dict]:
        slug = host["slug"]
        docker_mode = host.get("docker_mode", "all")
        allowed_stacks: set[str] | None = None
        if docker_mode == "selected":
            allowed_stacks = set(host.get("docker_stacks") or [])

        creds = get_credentials(slug)
        stacks = []
        async with await _connect(host, ssh_cfg, creds) as conn:
            rows = _parse_json_output(
                (await conn.run("docker compose ls --all --format json", check=False)).stdout
            )
            for row in rows:
                project_name = row.get("Name", "")
                config_file = row.get("ConfigFiles", "")
                if not project_name:
                    continue
                if allowed_stacks is not None and project_name not in allowed_stacks:
                    continue

                images = await self._check_images(
                    conn, project_name, dockerhub_creds
                )
                rollup = _rollup_status(images)
                ref = self._make_ref(slug, project_name)
                stacks.append({
                    "id": ref,
                    "name": project_name,
                    "endpoint_id": slug,
                    "endpoint_name": host["name"],
                    "update_status": rollup,
                    "images": images,
                    "update_path": f"{self.BACKEND_KEY}/{ref}",
                    "_config_file": config_file,
                })
        return stacks

    async def _check_images(
        self,
        conn,
        project_name: str,
        dockerhub_creds: dict | None,
    ) -> list[dict]:
        ps_result = await conn.run(
            f"docker compose -p {project_name} ps --format json", check=False
        )
        containers = _parse_json_output(ps_result.stdout)

        seen: set[str] = set()
        tasks = []
        for c in containers:
            img = c.get("Image", "")
            if img and img not in seen:
                seen.add(img)
                tasks.append(self._check_one_image(conn, img, dockerhub_creds))

        checked = await asyncio.gather(*tasks, return_exceptions=True)
        return [r for r in checked if isinstance(r, dict)]

    async def _check_one_image(
        self,
        conn,
        image_name: str,
        dockerhub_creds: dict | None,
    ) -> dict:
        try:
            inspect = await conn.run(
                f"docker image inspect {image_name} --format '{{{{json .RepoDigests}}}}'",
                check=False,
            )
            repo_digests: list[str] = []
            if inspect.returncode == 0:
                repo_digests = json.loads(inspect.stdout.strip())
            local_digest = extract_local_digest(repo_digests, image_name)
            status = await check_image_update(image_name, local_digest, dockerhub_creds)
        except Exception:
            status = "unknown"
        return {"name": image_name, "status": status}

    async def _get_config_file(self, conn, project_name: str) -> str:
        result = await conn.run(
            "docker compose ls --all --format json", check=False
        )
        rows = _parse_json_output(result.stdout)
        match = next((r for r in rows if r.get("Name") == project_name), None)
        return match.get("ConfigFiles", "") if match else ""


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
    # NDJSON: one JSON object per line
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


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
