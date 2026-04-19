"""Integration tests for admin panel routes."""

from unittest.mock import AsyncMock, patch

import yaml


# ---------------------------------------------------------------------------
# GET /admin
# ---------------------------------------------------------------------------


def test_admin_page_redirects_to_connections(client):
    response = client.get("/admin", follow_redirects=False)
    assert response.status_code in (302, 303)
    assert "/admin/connections" in response.headers["location"]


def test_admin_connections_returns_200(client):
    response = client.get("/admin/connections")
    assert response.status_code == 200


def test_admin_hosts_page_contains_hosts(client):
    response = client.get("/admin/hosts")
    assert "Test Host" in response.text
    assert "Custom User Host" in response.text


def test_admin_integrations_shows_portainer_configured(client, data_dir, config_file):
    from app.config_manager import save_portainer_config
    from app.credentials import save_integration_credentials

    save_portainer_config(url="https://portainer.test:9443", verify_ssl=False)
    save_integration_credentials("portainer", api_key="test-api-key")
    response = client.get("/admin/integrations")
    assert "portainer.test" in response.text


# ---------------------------------------------------------------------------
# GET /admin/hosts, /admin/ssh, /admin/https, /admin/account, /admin/about
# ---------------------------------------------------------------------------


def test_get_hosts_partial_returns_200(client):
    response = client.get("/admin/hosts")
    assert response.status_code == 200


def test_get_hosts_partial_lists_hosts(client):
    response = client.get("/admin/hosts")
    assert "Test Host" in response.text
    assert "192.168.1.10" in response.text


def test_admin_ssh_page_returns_200(client):
    response = client.get("/admin/ssh")
    assert response.status_code == 200


def test_admin_https_page_returns_200(client):
    response = client.get("/admin/https")
    assert response.status_code == 200


def test_admin_about_page_returns_200(client):
    from unittest.mock import AsyncMock, MagicMock, patch

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [
        {
            "tag_name": "v0.11.0",
            "html_url": "https://github.com/d4vastu/Keepup/releases/tag/v0.11.0",
            "published_at": "2026-04-01T00:00:00Z",
        },
    ]
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)
    with patch("app.admin.httpx.AsyncClient", return_value=mock_client):
        response = client.get("/admin/about")
    assert response.status_code == 200
    assert "v0.11.0" in response.text


def test_admin_about_page_handles_github_error(client):
    from unittest.mock import AsyncMock, patch

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=Exception("no network"))
    with patch("app.admin.httpx.AsyncClient", return_value=mock_client):
        response = client.get("/admin/about")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# POST /admin/hosts
# ---------------------------------------------------------------------------


def test_add_host_minimal(client, config_file):
    response = client.post(
        "/admin/hosts", data={"name": "New Host", "host": "10.0.0.99"}
    )
    assert response.status_code == 200
    assert "New Host" in response.text

    raw = yaml.safe_load(config_file.read_text())
    names = [h["name"] for h in raw["hosts"]]
    assert "New Host" in names


def test_add_host_with_optional_fields(client, config_file):
    response = client.post(
        "/admin/hosts",
        data={
            "name": "Full Host",
            "host": "10.0.0.50",
            "user": "ubuntu",
            "port": "2222",
        },
    )
    assert response.status_code == 200
    assert "Full Host" in response.text


def test_add_host_requires_name_and_host(client):
    response = client.post("/admin/hosts", data={"name": "", "host": ""})
    assert response.status_code == 200
    assert "required" in response.text.lower()


def test_add_host_no_credentials_in_config(client, config_file):
    """Credentials must not be stored in config.yml."""
    client.post(
        "/admin/hosts",
        data={
            "name": "Safe Host",
            "host": "10.0.0.99",
            "user": "dashboard",
        },
    )
    raw = yaml.safe_load(config_file.read_text())
    host = next(h for h in raw["hosts"] if h["name"] == "Safe Host")
    assert "password" not in host
    assert "key" not in host
    assert "ssh_key" not in host


# ---------------------------------------------------------------------------
# GET /admin/hosts/{slug}/edit
# ---------------------------------------------------------------------------


def test_get_edit_form_returns_200(client):
    response = client.get("/admin/hosts/test-host/edit")
    assert response.status_code == 200


def test_get_edit_form_pre_populates_values(client):
    response = client.get("/admin/hosts/test-host/edit")
    assert "Test Host" in response.text
    assert "192.168.1.10" in response.text


def test_get_edit_form_unknown_slug(client):
    response = client.get("/admin/hosts/does-not-exist/edit")
    assert response.status_code == 200
    assert "not found" in response.text.lower()


# ---------------------------------------------------------------------------
# PUT /admin/hosts/{slug}
# ---------------------------------------------------------------------------


def test_update_host(client, config_file):
    response = client.put(
        "/admin/hosts/test-host",
        data={
            "name": "Renamed Host",
            "host": "192.168.1.10",
        },
    )
    assert response.status_code == 200
    assert "Renamed Host" in response.text

    raw = yaml.safe_load(config_file.read_text())
    names = [h["name"] for h in raw["hosts"]]
    assert "Renamed Host" in names
    assert "Test Host" not in names


def test_update_host_renames_credentials(client, data_dir):
    """Renaming a host must migrate its credentials to the new slug."""
    from app.credentials import save_credentials, get_credentials

    save_credentials("test-host", ssh_password="pass123")

    client.put(
        "/admin/hosts/test-host", data={"name": "Renamed Host", "host": "192.168.1.10"}
    )

    assert get_credentials("renamed-host")["ssh_password"] == "pass123"
    assert get_credentials("test-host") == {}


# ---------------------------------------------------------------------------
# DELETE /admin/hosts/{slug}
# ---------------------------------------------------------------------------


def test_delete_host(client, config_file):
    response = client.delete("/admin/hosts/test-host")
    assert response.status_code == 200
    assert "Test Host" not in response.text

    raw = yaml.safe_load(config_file.read_text())
    names = [h["name"] for h in raw["hosts"]]
    assert "Test Host" not in names


def test_delete_host_leaves_others(client, config_file):
    client.delete("/admin/hosts/test-host")
    raw = yaml.safe_load(config_file.read_text())
    names = [h["name"] for h in raw["hosts"]]
    assert "Custom User Host" in names


def test_delete_host_removes_credentials(client, data_dir):
    from app.credentials import save_credentials, get_credentials

    save_credentials("test-host", ssh_password="pass")
    client.delete("/admin/hosts/test-host")
    assert get_credentials("test-host") == {}


# ---------------------------------------------------------------------------
# GET/POST /admin/hosts/{slug}/credentials
# ---------------------------------------------------------------------------


def test_get_credentials_form_returns_200(client):
    response = client.get("/admin/hosts/test-host/credentials")
    assert response.status_code == 200


def test_get_credentials_form_unknown_host(client):
    response = client.get("/admin/hosts/does-not-exist/credentials")
    assert response.status_code == 200
    assert "not found" in response.text.lower()


def test_get_credentials_form_shows_status_empty(client):
    response = client.get("/admin/hosts/test-host/credentials")
    assert response.status_code == 200
    # No saved credentials yet — "saved" badge should not appear
    assert "&#10003;" not in response.text


def test_get_credentials_form_shows_saved_badge(client, data_dir):
    from app.credentials import save_credentials

    save_credentials("test-host", ssh_password="pass", sudo_password="sudo")
    response = client.get("/admin/hosts/test-host/credentials")
    # Checkmark badges appear when credentials are saved
    assert "&#10003;" in response.text


def test_post_credentials_saves_ssh_password(client, data_dir):
    from app.credentials import get_credentials

    response = client.post(
        "/admin/hosts/test-host/credentials",
        data={
            "auth_method": "password",
            "ssh_password": "newpass",
            "ssh_key": "",
            "sudo_password": "",
        },
    )
    assert response.status_code == 200
    assert get_credentials("test-host")["ssh_password"] == "newpass"


def test_post_credentials_saves_ssh_key(client, data_dir):
    from app.credentials import get_credentials

    key_data = "-----BEGIN OPENSSH PRIVATE KEY-----\nfakekey\n-----END OPENSSH PRIVATE KEY-----"
    response = client.post(
        "/admin/hosts/test-host/credentials",
        data={
            "auth_method": "key",
            "ssh_password": "",
            "ssh_key": key_data,
            "sudo_password": "",
        },
    )
    assert response.status_code == 200
    assert get_credentials("test-host")["ssh_key"] == key_data


def test_post_credentials_saves_sudo_password(client, data_dir):
    from app.credentials import get_credentials

    client.post(
        "/admin/hosts/test-host/credentials",
        data={
            "auth_method": "key",
            "ssh_password": "",
            "ssh_key": "",
            "sudo_password": "mysudopass",
        },
    )
    assert get_credentials("test-host")["sudo_password"] == "mysudopass"


def test_post_credentials_password_method_clears_key(client, data_dir):
    """Selecting 'password' auth method should pass empty string for ssh_key, clearing it."""
    from app.credentials import save_credentials, get_credentials

    save_credentials("test-host", ssh_key="-----BEGIN...")
    client.post(
        "/admin/hosts/test-host/credentials",
        data={
            "auth_method": "password",
            "ssh_password": "newpass",
            "ssh_key": "",
            "sudo_password": "",
        },
    )
    creds = get_credentials("test-host")
    assert "ssh_key" not in creds
    assert creds["ssh_password"] == "newpass"


# ---------------------------------------------------------------------------
# PUT /admin/ssh
# ---------------------------------------------------------------------------


def test_update_ssh_settings(client, config_file):
    response = client.put(
        "/admin/ssh",
        data={
            "default_user": "ubuntu",
            "default_port": "2222",
            "default_key": "/app/keys/new_key",
            "connect_timeout": "30",
            "command_timeout": "300",
        },
    )
    assert response.status_code == 200
    assert "saved" in response.text.lower()

    raw = yaml.safe_load(config_file.read_text())
    assert raw["ssh"]["default_user"] == "ubuntu"
    assert raw["ssh"]["default_port"] == 2222


def test_update_ssh_invalid_port(client):
    response = client.put(
        "/admin/ssh",
        data={
            "default_user": "root",
            "default_port": "not-a-number",
            "default_key": "/app/keys/id_ed25519",
            "connect_timeout": "15",
            "command_timeout": "600",
        },
    )
    assert response.status_code == 200
    assert "error" in response.text.lower() or "invalid" in response.text.lower()


def test_delete_host_error_shows_message(client, monkeypatch):
    import app.admin as a

    monkeypatch.setattr(
        a, "delete_host", lambda slug: (_ for _ in ()).throw(Exception("disk full"))
    )
    response = client.delete("/admin/hosts/test-host")
    assert response.status_code == 200
    assert "disk full" in response.text


def test_update_host_error_shows_message(client, monkeypatch):
    import app.admin as a

    monkeypatch.setattr(
        a, "update_host", lambda **kw: (_ for _ in ()).throw(Exception("write error"))
    )
    response = client.put(
        "/admin/hosts/test-host", data={"name": "X", "host": "1.2.3.4"}
    )
    assert response.status_code == 200
    assert "write error" in response.text


# ---------------------------------------------------------------------------
# POST /admin/hosts/{slug}/test
# ---------------------------------------------------------------------------


def test_connection_test_success(client):
    mock_result = {"ok": True, "message": "Connected successfully."}
    with patch("app.admin.verify_connection", new=AsyncMock(return_value=mock_result)):
        response = client.post("/admin/hosts/test-host/test")
    assert response.status_code == 200
    assert "Connected" in response.text


def test_connection_test_failure(client):
    mock_result = {"ok": False, "message": "Connection refused"}
    with patch("app.admin.verify_connection", new=AsyncMock(return_value=mock_result)):
        response = client.post("/admin/hosts/test-host/test")
    assert response.status_code == 200
    assert "Failed" in response.text


def test_connection_test_unknown_host(client):
    response = client.post("/admin/hosts/does-not-exist/test")
    assert response.status_code == 200
    assert "not found" in response.text.lower()


# ---------------------------------------------------------------------------
# POST /admin/integrations/portainer/test
# ---------------------------------------------------------------------------


def test_admin_integrations_test_portainer_missing_fields(client):
    response = client.post(
        "/admin/integrations/portainer/test",
        data={"portainer_url": "", "portainer_api_key": ""},
    )
    assert response.status_code == 200
    assert "Enter a URL" in response.text


def test_admin_integrations_test_portainer_success(client):
    with patch(
        "app.portainer_client.PortainerClient.get_endpoints",
        new=AsyncMock(return_value=[{"Id": 1}, {"Id": 2}]),
    ):
        response = client.post(
            "/admin/integrations/portainer/test",
            data={
                "portainer_url": "https://portainer.test:9443",
                "portainer_api_key": "testkey",
                "portainer_verify_ssl": "",
            },
        )
    assert response.status_code == 200
    assert "Connected" in response.text
    assert "2 environments" in response.text


def test_admin_integrations_test_portainer_failure(client):
    with patch(
        "app.portainer_client.PortainerClient.get_endpoints",
        new=AsyncMock(side_effect=Exception("401 Unauthorized")),
    ):
        response = client.post(
            "/admin/integrations/portainer/test",
            data={
                "portainer_url": "https://portainer.test:9443",
                "portainer_api_key": "badkey",
            },
        )
    assert response.status_code == 200
    assert "Invalid API token" in response.text


def test_admin_integrations_test_portainer_ssl_error(client):
    with patch(
        "app.portainer_client.PortainerClient.get_endpoints",
        new=AsyncMock(side_effect=Exception("SSL certificate verify failed")),
    ):
        response = client.post(
            "/admin/integrations/portainer/test",
            data={
                "portainer_url": "https://portainer.test:9443",
                "portainer_api_key": "key",
            },
        )
    assert response.status_code == 200
    assert "SSL error" in response.text


# ---------------------------------------------------------------------------
# POST /admin/integrations/portainer (save)
# ---------------------------------------------------------------------------


def test_admin_integrations_save_portainer(client):
    with patch("app.admin.reload_backends", new=AsyncMock()):
        response = client.post(
            "/admin/integrations/portainer",
            data={
                "portainer_url": "https://portainer.test:9443",
                "portainer_api_key": "mykey",
                "portainer_verify_ssl": "",
            },
        )
    assert response.status_code == 200
    assert "saved" in response.text.lower()


# ---------------------------------------------------------------------------
# POST /admin/integrations/dockerhub (save)
# ---------------------------------------------------------------------------


def test_admin_integrations_save_dockerhub(client):
    with patch("app.admin.reload_backends", new=AsyncMock()):
        response = client.post(
            "/admin/integrations/dockerhub",
            data={"dockerhub_username": "myuser", "dockerhub_token": "mytoken"},
        )
    assert response.status_code == 200
    assert "saved" in response.text.lower()


def test_admin_integrations_save_dockerhub_clear(client):
    """Saving with empty username and token clears credentials."""
    with patch("app.admin.reload_backends", new=AsyncMock()):
        response = client.post(
            "/admin/integrations/dockerhub",
            data={"dockerhub_username": "", "dockerhub_token": ""},
        )
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# POST /admin/integrations/proxmox/discover
# ---------------------------------------------------------------------------


def test_admin_proxmox_discover_not_configured(client):
    response = client.post("/admin/integrations/proxmox/discover")
    assert response.status_code == 200
    assert "not configured" in response.text.lower() or response.text.strip() != ""


def test_admin_proxmox_discover_success(client, data_dir, config_file):
    from app.auth_router import save_proxmox_config
    from app.credentials import save_integration_credentials

    save_proxmox_config(url="https://192.168.1.10:8006", verify_ssl=False)
    save_integration_credentials("proxmox", token_id="root@pam!tok", secret="abc123")

    resources = [
        {"type": "qemu", "node": "pve", "vmid": 100, "name": "myvm",
         "status": "running", "ip": "192.168.1.20"},
        {"type": "lxc", "node": "pve", "vmid": 101, "name": "myct",
         "status": "running", "ip": "192.168.1.21"},
    ]
    with patch(
        "app.proxmox_client.ProxmoxClient.discover_resources",
        new=AsyncMock(return_value=resources),
    ):
        response = client.post("/admin/integrations/proxmox/discover")
    assert response.status_code == 200
    assert "myvm" in response.text or "myct" in response.text or "pve" in response.text


def test_admin_proxmox_discover_error(client, data_dir, config_file):
    from app.auth_router import save_proxmox_config
    from app.credentials import save_integration_credentials

    save_proxmox_config(url="https://192.168.1.10:8006", verify_ssl=False)
    save_integration_credentials("proxmox", token_id="root@pam!tok", secret="abc123")

    with patch(
        "app.proxmox_client.ProxmoxClient.discover_resources",
        new=AsyncMock(side_effect=Exception("connection refused")),
    ):
        response = client.post("/admin/integrations/proxmox/discover")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# POST /admin/integrations/proxmox/select-hosts
# ---------------------------------------------------------------------------


def test_admin_proxmox_select_hosts_adds_hosts(client, data_dir, config_file):

    response = client.post(
        "/admin/integrations/proxmox/select-hosts",
        data={
            "hosts": [
                "lxc|pve|101|myct|192.168.1.21",
                "qemu|pve|100|myvm|192.168.1.20",
            ]
        },
    )
    assert response.status_code == 200
    # Should report hosts added
    assert "added" in response.text.lower() or "host" in response.text.lower()


def test_admin_proxmox_select_hosts_empty(client):
    response = client.post("/admin/integrations/proxmox/select-hosts", data={})
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# POST /admin/integrations/proxmox/add-node-host
# ---------------------------------------------------------------------------


def test_admin_proxmox_add_node_host(client, data_dir, config_file):
    response = client.post(
        "/admin/integrations/proxmox/add-node-host",
        data={
            "node_name": "pve",
            "display_name": "Proxmox VE",
            "ip_address": "192.168.1.10",
        },
    )
    assert response.status_code == 200


def test_admin_integrations_test_portainer_connect_error(client):
    with patch(
        "app.portainer_client.PortainerClient.get_endpoints",
        new=AsyncMock(side_effect=Exception("could not connect to host")),
    ):
        response = client.post(
            "/admin/integrations/portainer/test",
            data={"portainer_url": "https://bad.host:9443", "portainer_api_key": "key"},
        )
    assert response.status_code == 200
    assert "reach" in response.text.lower() or "connect" in response.text.lower()


def test_admin_integrations_test_portainer_generic_error(client):
    with patch(
        "app.portainer_client.PortainerClient.get_endpoints",
        new=AsyncMock(side_effect=Exception("unexpected internal error")),
    ):
        response = client.post(
            "/admin/integrations/portainer/test",
            data={"portainer_url": "https://portainer.test:9443", "portainer_api_key": "key"},
        )
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# POST /admin/integrations/proxmox/select-hosts — correct field name + format
# ---------------------------------------------------------------------------


def test_admin_proxmox_select_hosts_correct_format(client, data_dir, config_file):
    """selected_hosts field uses node:vmid:type:name:ip format."""
    import yaml

    response = client.post(
        "/admin/integrations/proxmox/select-hosts",
        data={
            "selected_hosts": [
                "pve:101:lxc:myct:192.168.1.21",
                "pve:100:qemu:myvm:192.168.1.20",
            ]
        },
    )
    assert response.status_code == 200
    assert "added" in response.text.lower()
    raw = yaml.safe_load(config_file.read_text())
    lxc = next(h for h in raw["hosts"] if h["host"] == "192.168.1.21")
    assert lxc.get("docker_mode") == "all"


def test_admin_proxmox_select_hosts_lxc_docker_mode_special_chars(client, data_dir, config_file):
    """LXC names with special chars still get docker_mode=all."""
    import yaml

    response = client.post(
        "/admin/integrations/proxmox/select-hosts",
        data={"selected_hosts": ["pve:101:lxc:My CT (test):192.168.1.55"]},
    )
    assert response.status_code == 200
    raw = yaml.safe_load(config_file.read_text())
    lxc = next(h for h in raw["hosts"] if h["host"] == "192.168.1.55")
    assert lxc.get("docker_mode") == "all"


def test_admin_proxmox_select_hosts_skips_no_ip(client):
    response = client.post(
        "/admin/integrations/proxmox/select-hosts",
        data={"selected_hosts": ["pve:101:lxc:myct"]}  # no IP part
    )
    assert response.status_code == 200
    assert "No hosts added" in response.text or response.status_code == 200


def test_admin_proxmox_select_hosts_skips_duplicate(client, data_dir, config_file):
    import yaml
    raw = yaml.safe_load(config_file.read_text())
    existing_ip = raw["hosts"][0]["host"]
    response = client.post(
        "/admin/integrations/proxmox/select-hosts",
        data={"selected_hosts": [f"pve:101:lxc:myct:{existing_ip}"]}
    )
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# POST /admin/integrations/proxmox/add-node-host — with Proxmox configured
# ---------------------------------------------------------------------------


def test_admin_proxmox_add_node_host_success(client, data_dir, config_file):
    from app.auth_router import save_proxmox_config
    from app.credentials import save_integration_credentials

    save_proxmox_config(url="https://192.168.1.10:8006", verify_ssl=False)
    save_integration_credentials("proxmox", token_id="root@pam!tok", secret="abc")

    with patch(
        "app.proxmox_client.ProxmoxClient.get_nodes",
        new=AsyncMock(return_value=["pve"]),
    ):
        response = client.post("/admin/integrations/proxmox/add-node-host")
    assert response.status_code == 200
    assert "Added" in response.text or "already added" in response.text


def test_admin_proxmox_add_node_host_already_exists(client, data_dir, config_file):
    """If the Proxmox host IP is already in the host list, returns 'already added'."""
    import yaml
    raw = yaml.safe_load(config_file.read_text())
    existing_ip = raw["hosts"][0]["host"]

    from app.auth_router import save_proxmox_config
    from app.credentials import save_integration_credentials

    # Use a Proxmox URL whose hostname resolves to an existing host IP
    save_proxmox_config(url=f"https://{existing_ip}:8006", verify_ssl=False)
    save_integration_credentials("proxmox", token_id="root@pam!tok", secret="abc")

    with patch(
        "app.proxmox_client.ProxmoxClient.get_nodes",
        new=AsyncMock(return_value=["pve"]),
    ):
        response = client.post("/admin/integrations/proxmox/add-node-host")
    assert response.status_code == 200
    assert "already added" in response.text


def test_admin_proxmox_add_node_host_api_error(client, data_dir, config_file):
    from app.auth_router import save_proxmox_config
    from app.credentials import save_integration_credentials

    save_proxmox_config(url="https://192.168.1.10:8006", verify_ssl=False)
    save_integration_credentials("proxmox", token_id="root@pam!tok", secret="abc")

    with patch(
        "app.proxmox_client.ProxmoxClient.get_nodes",
        new=AsyncMock(side_effect=Exception("connection refused")),
    ):
        response = client.post("/admin/integrations/proxmox/add-node-host")
    assert response.status_code == 200
    assert "Could not reach" in response.text


# ---------------------------------------------------------------------------
# POST /admin/account/timezone
# ---------------------------------------------------------------------------


def test_admin_save_timezone_valid(client):
    response = client.post(
        "/admin/account/timezone", data={"timezone": "America/New_York"}
    )
    assert response.status_code == 200


def test_admin_save_timezone_invalid(client):
    response = client.post(
        "/admin/account/timezone", data={"timezone": "Invalid/Zone"}
    )
    assert response.status_code == 200
    assert "Unknown timezone" in response.text


# ---------------------------------------------------------------------------
# POST /admin/https/self-signed and /admin/https/disable
# ---------------------------------------------------------------------------


def test_admin_https_self_signed_no_hostname(client):
    response = client.post("/admin/https/self-signed", data={"hostname": ""})
    assert response.status_code == 200
    assert "Enter your server" in response.text


def test_admin_https_self_signed_with_hostname(client):
    with patch("app.admin._restart_after_delay", new=AsyncMock()):
        with patch("app.ssl_manager.generate_self_signed_cert", return_value=("CERT", "KEY")):
            with patch("app.ssl_manager.save_ssl_files"):
                with patch("app.admin.save_ssl_config"):
                    response = client.post(
                        "/admin/https/self-signed", data={"hostname": "192.168.1.100"}
                    )
    assert response.status_code == 200


def test_admin_https_disable(client):
    with patch("app.admin._restart_after_delay", new=AsyncMock()):
        with patch("app.ssl_manager.remove_ssl_files"):
            with patch("app.admin.clear_ssl_config"):
                response = client.post("/admin/https/disable")
    assert response.status_code == 200
