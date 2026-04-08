"""
Proxmox VE API client.

Used during setup to verify credentials and discover VMs and LXC containers.
API token format: user@realm!tokenname=uuid-value
"""

import httpx


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

    async def discover_resources(self) -> list[dict]:
        """Return all VMs and LXC containers across all nodes with IPs, sorted by node + vmid."""
        import asyncio

        async with self._client() as c:
            # cluster/resources?type=vm returns both qemu and lxc in one call
            # and requires only VM.Audit on /vms — no per-node path permissions needed.
            r = await c.get("/api2/json/cluster/resources", params={"type": "vm"})
            r.raise_for_status()

            resources: list[dict] = []
            for item in r.json().get("data", []):
                rtype = item.get("type")
                if rtype not in ("qemu", "lxc"):
                    continue
                vmid = item.get("vmid")
                resources.append(
                    {
                        "type": rtype,
                        "node": item.get("node", ""),
                        "vmid": vmid,
                        "name": item.get("name", f"{rtype}-{vmid}"),
                        "status": item.get("status", "unknown"),
                        "ip": "",
                    }
                )

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

        return sorted(resources, key=lambda r: (r["node"], r["vmid"]))
