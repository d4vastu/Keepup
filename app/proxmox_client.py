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

    async def discover_resources(self) -> list[dict]:
        """Return all VMs and LXC containers across all nodes, sorted by node + vmid."""
        async with self._client() as c:
            nodes_resp = await c.get("/api2/json/nodes")
            nodes_resp.raise_for_status()
            nodes = nodes_resp.json()["data"]

            resources: list[dict] = []

            for node in nodes:
                node_name = node["node"]

                try:
                    r = await c.get(f"/api2/json/nodes/{node_name}/qemu")
                    r.raise_for_status()
                    for vm in r.json()["data"]:
                        resources.append(
                            {
                                "type": "vm",
                                "node": node_name,
                                "vmid": vm["vmid"],
                                "name": vm.get("name", f"vm-{vm['vmid']}"),
                                "status": vm.get("status", "unknown"),
                            }
                        )
                except Exception:
                    pass

                try:
                    r = await c.get(f"/api2/json/nodes/{node_name}/lxc")
                    r.raise_for_status()
                    for ct in r.json()["data"]:
                        resources.append(
                            {
                                "type": "lxc",
                                "node": node_name,
                                "vmid": ct["vmid"],
                                "name": ct.get("name", f"ct-{ct['vmid']}"),
                                "status": ct.get("status", "unknown"),
                            }
                        )
                except Exception:
                    pass

        return sorted(resources, key=lambda r: (r["node"], r["vmid"]))
