"""Smoke tests for main dashboard routes."""

from unittest.mock import AsyncMock, patch, MagicMock


# ---------------------------------------------------------------------------
# / (home) and /dashboard
# ---------------------------------------------------------------------------


def test_home_unauthenticated_returns_landing(anon_client):
    """Unauthenticated / shows the public landing page."""
    response = anon_client.get("/", follow_redirects=False)
    # Either renders home.html (200) or let follow_redirects handle it
    assert response.status_code == 200
    assert "keepup" in response.text.lower()


def test_home_authenticated_redirects_to_home(client):
    """Authenticated / redirects to /home."""
    response = client.get("/", follow_redirects=False)
    assert response.status_code in (302, 303)
    assert response.headers["location"] == "/home"


def test_home_returns_200(client):
    response = client.get("/home")
    assert response.status_code == 200


def test_home_lists_hosts(client):
    response = client.get("/home")
    assert "Test Host" in response.text
    assert "Custom User Host" in response.text


def test_home_has_admin_link(client):
    response = client.get("/home")
    assert "/admin" in response.text


def test_dashboard_redirects_to_home(client):
    """/dashboard now 301-redirects to /home."""
    response = client.get("/dashboard", follow_redirects=False)
    assert response.status_code == 301
    assert response.headers["location"] == "/home"


def test_unauthenticated_protected_route_redirects_to_login(anon_client):
    """Unauthenticated access to a protected route redirects to /login."""
    response = anon_client.get("/home", follow_redirects=False)
    assert response.status_code in (302, 303)
    assert "/login" in response.headers["location"]


# ---------------------------------------------------------------------------
# /api/host/{slug}/check
# ---------------------------------------------------------------------------


def test_host_check_unknown_slug_returns_error(client):
    response = client.get("/api/host/does-not-exist/check")
    assert response.status_code == 200
    # Returns an error partial, not a 404
    assert "does-not-exist" in response.text or "error" in response.text.lower()


def test_host_check_calls_ssh(client):
    mock_result = {"packages": [], "reboot_required": False}
    with patch("app.main.check_host_updates", new=AsyncMock(return_value=mock_result)):
        response = client.get("/api/host/test-host/check")
    assert response.status_code == 200
    assert "up to date" in response.text.lower()


def test_host_check_shows_pending_updates(client):
    mock_result = {
        "packages": [{"name": "curl", "current": "7.0", "available": "8.0"}],
        "reboot_required": False,
    }
    with patch("app.main.check_host_updates", new=AsyncMock(return_value=mock_result)):
        response = client.get("/api/host/test-host/check")
    assert "curl" in response.text
    assert "1 update" in response.text


def test_host_check_shows_reboot_required(client):
    mock_result = {"packages": [], "reboot_required": True}
    with patch("app.main.check_host_updates", new=AsyncMock(return_value=mock_result)):
        response = client.get("/api/host/test-host/check")
    assert "reboot" in response.text.lower()


# ---------------------------------------------------------------------------
# /api/docker/check
# ---------------------------------------------------------------------------


def test_docker_check_without_backends_returns_error(client, monkeypatch):
    import app.backend_loader as bl

    # SSH backend present but no hosts with docker_mode → treated as unconfigured
    from unittest.mock import AsyncMock

    ssh_b = MagicMock()
    ssh_b.BACKEND_KEY = "ssh"
    ssh_b.get_stacks_with_update_status = AsyncMock(return_value=[])
    monkeypatch.setattr(bl, "_backends", [ssh_b])
    response = client.get("/api/docker/check")
    assert response.status_code == 200
    assert "not configured" in response.text.lower() or "No container" in response.text


# ---------------------------------------------------------------------------
# /api/jobs/{job_id}
# ---------------------------------------------------------------------------


def test_job_status_unknown_id(client):
    response = client.get("/api/jobs/doesnotexist")
    assert response.status_code == 200
    assert "not found" in response.text.lower()


# ---------------------------------------------------------------------------
# Sign out button
# ---------------------------------------------------------------------------


def test_dashboard_has_sign_out_link(client):
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert "/logout" in response.text
    assert "Sign out" in response.text


# ---------------------------------------------------------------------------
# Version update notice
# ---------------------------------------------------------------------------


def test_dashboard_shows_update_notice_when_newer_version(client):
    with patch(
        "app.main._get_latest_version",
        new=AsyncMock(
            return_value=("99.0.0", "https://github.com/example/releases/tag/v99.0.0")
        ),
    ):
        response = client.get("/dashboard")
    assert response.status_code == 200
    assert "99.0.0" in response.text
    assert "available" in response.text


def test_dashboard_no_update_notice_when_up_to_date(client):
    with patch(
        "app.main._get_latest_version",
        new=AsyncMock(return_value=("0.0.1", "https://example.com")),
    ):
        response = client.get("/dashboard")
    assert response.status_code == 200
    assert "available ↑" not in response.text


def test_dashboard_no_update_notice_when_version_check_fails(client):
    with patch(
        "app.main._get_latest_version", new=AsyncMock(return_value=(None, None))
    ):
        response = client.get("/dashboard")
    assert response.status_code == 200
    assert "available ↑" not in response.text


# ---------------------------------------------------------------------------
# _group_hosts unit tests
# ---------------------------------------------------------------------------


def test_group_hosts_all_standalone():
    from app.main import _group_hosts

    hosts = [
        {"slug": "a", "name": "A", "host": "1.1.1.1"},
        {"slug": "b", "name": "B", "host": "1.1.1.2"},
    ]
    groups, standalone = _group_hosts(hosts)
    assert groups == []
    assert len(standalone) == 2


def test_group_hosts_proxmox_node_only():
    from app.main import _group_hosts

    hosts = [
        {"slug": "pve", "name": "Proxmox VE (pve)", "host": "10.0.0.1", "proxmox_node": "pve"},
    ]
    groups, standalone = _group_hosts(hosts)
    assert len(groups) == 1
    assert groups[0]["name"] == "pve"
    assert groups[0]["node_host"]["slug"] == "pve"
    assert groups[0]["lxcs"] == []
    assert groups[0]["vms"] == []
    assert standalone == []


def test_group_hosts_lxc_defaults_to_lxc_type():
    from app.main import _group_hosts

    hosts = [
        {
            "slug": "myct",
            "name": "My CT",
            "host": "10.0.0.2",
            "proxmox_node": "pve",
            "proxmox_vmid": 101,
        },
    ]
    groups, standalone = _group_hosts(hosts)
    assert len(groups[0]["lxcs"]) == 1
    assert groups[0]["vms"] == []


def test_group_hosts_explicit_vm_type():
    from app.main import _group_hosts

    hosts = [
        {
            "slug": "myvm",
            "name": "My VM",
            "host": "10.0.0.3",
            "proxmox_node": "pve",
            "proxmox_vmid": 200,
            "proxmox_type": "vm",
        },
    ]
    groups, standalone = _group_hosts(hosts)
    assert groups[0]["vms"][0]["slug"] == "myvm"
    assert groups[0]["lxcs"] == []


def test_group_hosts_mixed_node_lxc_vm_and_standalone():
    from app.main import _group_hosts

    hosts = [
        {"slug": "standalone", "name": "Standalone", "host": "1.2.3.4"},
        {"slug": "node", "name": "PVE", "host": "10.0.0.1", "proxmox_node": "pve"},
        {"slug": "lxc101", "name": "LXC 101", "host": "10.0.0.2", "proxmox_node": "pve", "proxmox_vmid": 101, "proxmox_type": "lxc"},
        {"slug": "vm200", "name": "VM 200", "host": "10.0.0.3", "proxmox_node": "pve", "proxmox_vmid": 200, "proxmox_type": "vm"},
    ]
    groups, standalone = _group_hosts(hosts)
    assert len(groups) == 1
    assert groups[0]["node_host"]["slug"] == "node"
    assert len(groups[0]["lxcs"]) == 1
    assert len(groups[0]["vms"]) == 1
    assert len(standalone) == 1


def test_group_hosts_multiple_nodes():
    from app.main import _group_hosts

    hosts = [
        {"slug": "n1", "name": "Node1", "host": "10.0.0.1", "proxmox_node": "pve1"},
        {"slug": "n2", "name": "Node2", "host": "10.0.0.2", "proxmox_node": "pve2"},
        {"slug": "ct1", "name": "CT1", "host": "10.0.0.3", "proxmox_node": "pve1", "proxmox_vmid": 101},
    ]
    groups, standalone = _group_hosts(hosts)
    assert len(groups) == 2
    pve1 = next(g for g in groups if g["name"] == "pve1")
    assert pve1["node_host"]["slug"] == "n1"
    assert len(pve1["lxcs"]) == 1


# ---------------------------------------------------------------------------
# Dashboard rendering with grouped hosts
# ---------------------------------------------------------------------------


def test_home_shows_proxmox_group_header(client, config_file):
    import yaml

    cfg = yaml.safe_load(config_file.read_text())
    cfg["hosts"].append({
        "name": "Proxmox VE (pve)",
        "host": "10.0.0.1",
        "proxmox_node": "pve",
    })
    config_file.write_text(yaml.dump(cfg))

    response = client.get("/home")
    assert response.status_code == 200
    assert "Proxmox VE pve" in response.text


def test_home_shows_lxc_badge(client, config_file):
    import yaml

    cfg = yaml.safe_load(config_file.read_text())
    cfg["hosts"].append({
        "name": "My LXC",
        "host": "10.0.0.5",
        "proxmox_node": "pve",
        "proxmox_vmid": 105,
        "proxmox_type": "lxc",
    })
    config_file.write_text(yaml.dump(cfg))

    response = client.get("/home")
    assert "LXC 105" in response.text


def test_home_shows_vm_badge(client, config_file):
    import yaml

    cfg = yaml.safe_load(config_file.read_text())
    cfg["hosts"].append({
        "name": "My VM",
        "host": "10.0.0.6",
        "proxmox_node": "pve",
        "proxmox_vmid": 201,
        "proxmox_type": "vm",
    })
    config_file.write_text(yaml.dump(cfg))

    response = client.get("/home")
    assert "VM 201" in response.text


def test_home_shows_standalone_divider_when_mixed(client, config_file):
    import yaml

    cfg = yaml.safe_load(config_file.read_text())
    cfg["hosts"].append({
        "name": "Proxmox VE (pve)",
        "host": "10.0.0.1",
        "proxmox_node": "pve",
    })
    config_file.write_text(yaml.dump(cfg))

    response = client.get("/home")
    assert "Standalone hosts" in response.text


def test_home_no_standalone_divider_when_only_standalone(client):
    response = client.get("/home")
    assert "Standalone hosts" not in response.text
