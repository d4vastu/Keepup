"""
Proxmox VE API client.

Used during setup to verify credentials and discover VMs and LXC containers.
API token format: user@realm!tokenname=uuid-value
"""

import logging

import httpx

log = logging.getLogger(__name__)


class ProxmoxClient:
    def __init__(self, url: str, api_token: str, verify_ssl: bool = False):
        self.base = url.rstrip("/")
        self.headers = {"Authorization": f"PVEAPIToken={api_token}"}
        self.verify_ssl = verify_ssl

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base,
            headers=self.headers,
            verify=self.verify_ssl,
            timeout=15,
        )

    async def get_version(self) -> dict:
        log.info("Proxmox: testing API at %s", self.base)
        async with self._client() as c:
            resp = await c.get("/api2/json/version")
            resp.raise_for_status()
            return resp.json()["data"]

    async def _get_lxc_ip(self, c: httpx.AsyncClient, node: str, vmid: int) -> str:
        """Fetch primary IP for an LXC container. Returns '' on failure."""
        try:
            r = await c.get(f"/api2/json/nodes/{node}/lxc/{vmid}/interfaces")
            r.raise_for_status()
            for iface in r.json().get("data", []):
                name = iface.get("name", "")
                if name == "lo":
                    continue
                for addr in iface.get("inet", "").split(","):
                    ip = addr.strip().split("/")[0]
                    if ip and not ip.startswith("127."):
                        return ip
        except Exception:
            pass
        return ""

    async def _get_vm_ip(self, c: httpx.AsyncClient, node: str, vmid: int) -> str:
        """Fetch primary IP for a VM via QEMU guest agent. Returns '' on failure."""
        try:
            r = await c.get(
                f"/api2/json/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces"
            )
            r.raise_for_status()
            for iface in r.json().get("data", {}).get("result", []):
                name = iface.get("name", "")
                if name == "lo":
                    continue
                for addr_info in iface.get("ip-addresses", []):
                    if addr_info.get("ip-address-type") != "ipv4":
                        continue
                    ip = addr_info.get("ip-address", "")
                    if ip and not ip.startswith("127."):
                        return ip
        except Exception:
            pass
        return ""

    async def get_lxc_updates(
        self, node: str, vmid: int, ssh_host: str, ssh_cfg: dict, ssh_creds: dict
    ) -> list[dict]:
        """
        Return available apt packages for an LXC container by running
        `pct exec {vmid} -- apt list -qq --upgradable` via SSH on the Proxmox host.

        ssh_host: IP/hostname of the Proxmox node
        ssh_cfg / ssh_creds: same format as ssh_client helpers
        """
        from .ssh_client import _connect, _run

        log.info("Proxmox: pct exec on %s/%s via %s", node, vmid, ssh_host)

        if not ssh_creds.get("key_path") and not ssh_creds.get("ssh_password"):
            raise RuntimeError(
                "No SSH credentials configured for Proxmox host. "
                "Set SSH user and key/password in Admin → Integrations → Proxmox VE."
            )

        host_entry = {
            "host": ssh_host,
            "user": ssh_creds.get("user", "root"),
            "port": ssh_creds.get("port", 22),
        }
        cmd = (
            f"pct exec {vmid} -- sh -c "
            f"'apt-get update -qq 2>/dev/null; apt list -qq --upgradable 2>/dev/null'"
        )
        async with await _connect(host_entry, ssh_cfg, ssh_creds) as conn:
            result = await _run(conn, cmd, sudo_password=None, needs_sudo=False, timeout=90)

        packages = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line or line.startswith("Listing"):
                continue
            # Format: package/suite version arch [upgradable from: oldversion]
            parts = line.split()
            if len(parts) >= 2:
                pkg_name = parts[0].split("/")[0]
                new_ver = parts[1]
                old_ver = ""
                if "upgradable from:" in line:
                    old_ver = line.split("upgradable from:")[-1].strip().rstrip("]")
                packages.append({"name": pkg_name, "current": old_ver, "available": new_ver})

        n = len(packages)
        if n:
            log.info("Proxmox: %s/%s — %d update(s) available", node, vmid, n)
        else:
            log.info("Proxmox: %s/%s — up to date", node, vmid)
        return packages

    async def get_node_updates(self, node: str) -> list[dict]:
        """Return available apt packages for a Proxmox node via the apt API."""
        log.info("Proxmox: checking node %s for apt updates", node)
        async with self._client() as c:
            r = await c.get(f"/api2/json/nodes/{node}/apt/update")
            r.raise_for_status()
            packages = [
                {
                    "name": item.get("Package", ""),
                    "current": item.get("OldVersion", ""),
                    "available": item.get("Version", ""),
                }
                for item in r.json().get("data", [])
            ]
        n = len(packages)
        if n:
            log.info("Proxmox: node %s — %d update(s)", node, n)
        else:
            log.info("Proxmox: node %s — up to date", node)
        return packages

    async def upgrade_lxc(
        self, node: str, vmid: int, ssh_host: str, ssh_cfg: dict, ssh_creds: dict
    ) -> list[str]:
        """Run apt-get upgrade inside an LXC container via pct exec over SSH."""
        from .ssh_client import _connect, _run

        log.info("Proxmox: pct exec upgrade on %s/%s via %s", node, vmid, ssh_host)

        if not ssh_creds.get("key_path") and not ssh_creds.get("ssh_password"):
            raise RuntimeError(
                "No SSH credentials configured for Proxmox host. "
                "Set SSH user and key/password in Admin → Integrations → Proxmox VE."
            )

        host_entry = {
            "host": ssh_host,
            "user": ssh_creds.get("user", "root"),
            "port": ssh_creds.get("port", 22),
        }
        cmd = f"pct exec {vmid} -- apt-get upgrade -y 2>&1"
        async with await _connect(host_entry, ssh_cfg, ssh_creds) as conn:
            result = await _run(conn, cmd, sudo_password=None, needs_sudo=False, timeout=300)

        lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
        log.info("Proxmox: upgrade complete on %s/%s — %d line(s)", node, vmid, len(lines))
        return lines

    async def upgrade_node(self, node: str) -> list[str]:
        """Run apt upgrade on a Proxmox node via the API. Returns log lines."""
        import asyncio

        log.info("Proxmox: starting apt upgrade on node %s", node)
        async with self._client() as c:
            r = await c.post(f"/api2/json/nodes/{node}/apt/upgrade")
            r.raise_for_status()
            upid = r.json().get("data", "")
            if not upid:
                raise RuntimeError("Proxmox API did not return a task ID for upgrade.")
            log.info("Proxmox: upgrade task %s started on node %s", upid, node)

            for _ in range(120):
                await asyncio.sleep(3)
                status_r = await c.get(f"/api2/json/nodes/{node}/tasks/{upid}/status")
                status_r.raise_for_status()
                status = status_r.json().get("data", {})
                if status.get("status") == "stopped":
                    exit_status = status.get("exitstatus", "")
                    if exit_status != "OK":
                        log.warning("Proxmox: upgrade task exited with %s", exit_status)
                    break

            log_r = await c.get(f"/api2/json/nodes/{node}/tasks/{upid}/log", params={"limit": 500})
            log_r.raise_for_status()
            lines = [entry.get("t", "") for entry in log_r.json().get("data", [])]

        log.info("Proxmox: upgrade complete on node %s", node)
        return lines

    async def get_running_guests(self, node: str) -> list[dict]:
        """Return all running VMs and LXCs on a node."""
        import asyncio

        async with self._client() as c:
            results = await asyncio.gather(
                c.get(f"/api2/json/nodes/{node}/qemu"),
                c.get(f"/api2/json/nodes/{node}/lxc"),
                return_exceptions=True,
            )
        guests = []
        for guest_type, result in zip(("qemu", "lxc"), results):
            if isinstance(result, Exception):
                log.warning("Proxmox: failed to list %s on %s: %s", guest_type, node, result)
                continue
            result.raise_for_status()
            for item in result.json().get("data", []):
                if item.get("status") == "running":
                    guests.append({
                        "vmid": item["vmid"],
                        "name": item.get("name", f"{guest_type}-{item['vmid']}"),
                        "type": guest_type,
                    })
        log.info("Proxmox: %d running guest(s) on node %s", len(guests), node)
        return guests

    async def stop_guest(
        self, node: str, vmid: int, guest_type: str, timeout: int = 60
    ) -> str:
        """Gracefully shut down a guest. Returns 'stopped' or 'timed_out'."""
        import asyncio

        log.info(
            "Proxmox: shutting down %s %s/%s (timeout=%ds)", guest_type, node, vmid, timeout
        )
        async with self._client() as c:
            r = await c.post(
                f"/api2/json/nodes/{node}/{guest_type}/{vmid}/status/shutdown"
            )
            r.raise_for_status()
            elapsed = 0
            while elapsed < timeout:
                await asyncio.sleep(2)
                elapsed += 2
                status_r = await c.get(
                    f"/api2/json/nodes/{node}/{guest_type}/{vmid}/status/current"
                )
                status_r.raise_for_status()
                if status_r.json().get("data", {}).get("status") == "stopped":
                    log.info("Proxmox: %s %s/%s stopped", guest_type, node, vmid)
                    return "stopped"
        log.warning(
            "Proxmox: %s %s/%s timed out after %ds", guest_type, node, vmid, timeout
        )
        return "timed_out"

    async def force_stop_guest(self, node: str, vmid: int, guest_type: str) -> None:
        """Immediately poweroff a guest (no graceful shutdown)."""
        log.info("Proxmox: force-stopping %s %s/%s", guest_type, node, vmid)
        async with self._client() as c:
            r = await c.post(
                f"/api2/json/nodes/{node}/{guest_type}/{vmid}/status/stop"
            )
            r.raise_for_status()

    @staticmethod
    def _parse_kversion(kversion: str) -> str:
        """Extract kernel release from kversion string.

        kversion: 'Linux 6.17.13-3-pve #1 SMP ...' → '6.17.13-3-pve'
        """
        try:
            return kversion.split()[1]
        except IndexError:
            return ""

    @staticmethod
    def _kernel_ver_tuple(release: str) -> tuple:
        """'6.17.13-3-pve' → (6, 17, 13, 3)"""
        try:
            release = release.replace("-pve", "")
            main, *rest = release.split("-", 1)
            nums = tuple(int(x) for x in main.split("."))
            build = (int(rest[0]),) if rest else (0,)
            return nums + build
        except (ValueError, AttributeError):
            return (0,)

    async def get_node_reboot_required(self, node: str) -> bool:
        """Return True if a newer Proxmox kernel is installed than what's running.

        Uses kversion from node/status and proxmox-kernel-*-pve-signed packages
        from apt/versions (PVE 9+ naming).
        """
        import asyncio

        async with self._client() as c:
            results = await asyncio.gather(
                c.get(f"/api2/json/nodes/{node}/status"),
                c.get(f"/api2/json/nodes/{node}/apt/versions"),
                return_exceptions=True,
            )

        if any(isinstance(r, Exception) for r in results):
            return False
        status_r, versions_r = results
        try:
            status_r.raise_for_status()
            versions_r.raise_for_status()
        except Exception:
            return False

        kversion = status_r.json().get("data", {}).get("kversion", "")
        running = self._parse_kversion(kversion)
        if not running:
            return False

        packages = versions_r.json().get("data", [])
        # Package names: "proxmox-kernel-6.17.13-3-pve-signed"
        installed_kernels = [
            pkg["Package"][len("proxmox-kernel-"):-len("-signed")]
            for pkg in packages
            if pkg.get("Package", "").startswith("proxmox-kernel-")
            and pkg.get("Package", "").endswith("-pve-signed")
        ]
        if not installed_kernels:
            return False

        running_ver = self._kernel_ver_tuple(running)
        return any(self._kernel_ver_tuple(k) > running_ver for k in installed_kernels)

    async def reboot_node(self, node: str) -> None:
        """Issue a node reboot via the Proxmox API (bulk-stops guests, then reboots)."""
        log.info("Proxmox: issuing reboot for node %s", node)
        async with self._client() as c:
            r = await c.post(
                f"/api2/json/nodes/{node}/status",
                json={"command": "reboot"},
            )
            r.raise_for_status()

    async def get_node_kernel(self, node: str) -> str:
        """Return the running kernel release string for a node."""
        async with self._client() as c:
            r = await c.get(f"/api2/json/nodes/{node}/status")
            r.raise_for_status()
            kversion = r.json().get("data", {}).get("kversion", "")
            return self._parse_kversion(kversion) or "unknown"

    async def wait_for_node(self, node: str, timeout: int = 600) -> bool:
        """Poll until the node API responds or timeout expires. Returns True if up."""
        import asyncio

        log.info("Proxmox: waiting for node %s (timeout=%ds)", node, timeout)
        elapsed = 0
        while elapsed < timeout:
            await asyncio.sleep(5)
            elapsed += 5
            try:
                async with httpx.AsyncClient(
                    base_url=self.base,
                    headers=self.headers,
                    verify=self.verify_ssl,
                    timeout=5,
                ) as c:
                    r = await c.get(f"/api2/json/nodes/{node}/status")
                    if r.status_code == 200:
                        log.info("Proxmox: node %s responded after %ds", node, elapsed)
                        return True
            except Exception:
                pass
        log.warning("Proxmox: node %s did not return within %ds", node, timeout)
        return False

    async def get_nodes(self) -> list[str]:
        """Return names of all online nodes."""
        async with self._client() as c:
            r = await c.get("/api2/json/nodes")
            r.raise_for_status()
            return [
                n["node"]
                for n in r.json().get("data", [])
                if n.get("status") == "online"
            ]

    async def discover_resources(self) -> list[dict]:
        """Return all VMs and LXC containers across all nodes with IPs, sorted by node + vmid."""
        import asyncio

        async with self._client() as c:
            # Use a dict keyed by (type, node, vmid) to deduplicate across sources.
            seen: dict[tuple, dict] = {}

            # Phase 1: cluster/resources — single call, but token permissions may
            # exclude LXC containers depending on how the token was scoped.
            try:
                r = await c.get("/api2/json/cluster/resources")
                r.raise_for_status()
                for item in r.json().get("data", []):
                    rtype = item.get("type")
                    if rtype not in ("qemu", "lxc"):
                        continue
                    vmid = item.get("vmid")
                    node = item.get("node", "")
                    seen[(rtype, node, vmid)] = {
                        "type": rtype,
                        "node": node,
                        "vmid": vmid,
                        "name": item.get("name", f"{rtype}-{vmid}"),
                        "status": item.get("status", "unknown"),
                        "ip": "",
                    }
            except Exception:
                pass

            # Phase 2: per-node LXC endpoint — supplements phase 1 when the token
            # has node-level but not cluster-level LXC visibility.
            try:
                nodes_r = await c.get("/api2/json/nodes")
                nodes_r.raise_for_status()
                for node in nodes_r.json().get("data", []):
                    node_name = node["node"]
                    try:
                        lxc_r = await c.get(f"/api2/json/nodes/{node_name}/lxc")
                        lxc_r.raise_for_status()
                        for ct in lxc_r.json().get("data", []):
                            vmid = ct["vmid"]
                            key = ("lxc", node_name, vmid)
                            if key not in seen:
                                seen[key] = {
                                    "type": "lxc",
                                    "node": node_name,
                                    "vmid": vmid,
                                    "name": ct.get("name", f"lxc-{vmid}"),
                                    "status": ct.get("status", "unknown"),
                                    "ip": "",
                                }
                    except Exception:
                        pass
            except Exception:
                pass

            resources = list(seen.values())

            # Fetch IPs concurrently
            async def _fill_ip(resource: dict) -> None:
                if resource["type"] == "lxc":
                    resource["ip"] = await self._get_lxc_ip(
                        c, resource["node"], resource["vmid"]
                    )
                else:
                    resource["ip"] = await self._get_vm_ip(
                        c, resource["node"], resource["vmid"]
                    )

            await asyncio.gather(*[_fill_ip(r) for r in resources])

        n_vms = sum(1 for r in resources if r["type"] == "qemu")
        n_lxcs = sum(1 for r in resources if r["type"] == "lxc")
        log.info("Proxmox: discovered %d VMs and %d LXCs", n_vms, n_lxcs)
        return sorted(resources, key=lambda r: (r["node"], r["vmid"]))
