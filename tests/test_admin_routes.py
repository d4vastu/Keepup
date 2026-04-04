"""Integration tests for admin panel routes."""
from unittest.mock import AsyncMock, patch

import yaml


# ---------------------------------------------------------------------------
# GET /admin
# ---------------------------------------------------------------------------

def test_admin_page_returns_200(client):
    response = client.get("/admin")
    assert response.status_code == 200


def test_admin_page_contains_hosts(client):
    response = client.get("/admin")
    assert "Test Host" in response.text
    assert "Custom User Host" in response.text


def test_admin_page_shows_portainer_configured(client, data_dir, config_file):
    from app.config_manager import save_portainer_config
    from app.credentials import save_integration_credentials
    save_portainer_config(url="https://portainer.test:9443", verify_ssl=False)
    save_integration_credentials("portainer", api_key="test-api-key")
    response = client.get("/admin")
    assert "portainer.test" in response.text


# ---------------------------------------------------------------------------
# GET /admin/hosts
# ---------------------------------------------------------------------------

def test_get_hosts_partial_returns_200(client):
    response = client.get("/admin/hosts")
    assert response.status_code == 200


def test_get_hosts_partial_lists_hosts(client):
    response = client.get("/admin/hosts")
    assert "Test Host" in response.text
    assert "192.168.1.10" in response.text


# ---------------------------------------------------------------------------
# POST /admin/hosts
# ---------------------------------------------------------------------------

def test_add_host_minimal(client, config_file):
    response = client.post("/admin/hosts", data={"name": "New Host", "host": "10.0.0.99"})
    assert response.status_code == 200
    assert "New Host" in response.text

    raw = yaml.safe_load(config_file.read_text())
    names = [h["name"] for h in raw["hosts"]]
    assert "New Host" in names


def test_add_host_with_optional_fields(client, config_file):
    response = client.post("/admin/hosts", data={
        "name": "Full Host",
        "host": "10.0.0.50",
        "user": "ubuntu",
        "port": "2222",
    })
    assert response.status_code == 200
    assert "Full Host" in response.text


def test_add_host_requires_name_and_host(client):
    response = client.post("/admin/hosts", data={"name": "", "host": ""})
    assert response.status_code == 200
    assert "required" in response.text.lower()


def test_add_host_no_credentials_in_config(client, config_file):
    """Credentials must not be stored in config.yml."""
    client.post("/admin/hosts", data={
        "name": "Safe Host",
        "host": "10.0.0.99",
        "user": "dashboard",
    })
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
    response = client.put("/admin/hosts/test-host", data={
        "name": "Renamed Host",
        "host": "192.168.1.10",
    })
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

    client.put("/admin/hosts/test-host", data={"name": "Renamed Host", "host": "192.168.1.10"})

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
    response = client.post("/admin/hosts/test-host/credentials", data={
        "auth_method": "password",
        "ssh_password": "newpass",
        "ssh_key": "",
        "sudo_password": "",
    })
    assert response.status_code == 200
    assert get_credentials("test-host")["ssh_password"] == "newpass"


def test_post_credentials_saves_ssh_key(client, data_dir):
    from app.credentials import get_credentials
    key_data = "-----BEGIN OPENSSH PRIVATE KEY-----\nfakekey\n-----END OPENSSH PRIVATE KEY-----"
    response = client.post("/admin/hosts/test-host/credentials", data={
        "auth_method": "key",
        "ssh_password": "",
        "ssh_key": key_data,
        "sudo_password": "",
    })
    assert response.status_code == 200
    assert get_credentials("test-host")["ssh_key"] == key_data


def test_post_credentials_saves_sudo_password(client, data_dir):
    from app.credentials import get_credentials
    client.post("/admin/hosts/test-host/credentials", data={
        "auth_method": "key",
        "ssh_password": "",
        "ssh_key": "",
        "sudo_password": "mysudopass",
    })
    assert get_credentials("test-host")["sudo_password"] == "mysudopass"


def test_post_credentials_password_method_clears_key(client, data_dir):
    """Selecting 'password' auth method should pass empty string for ssh_key, clearing it."""
    from app.credentials import save_credentials, get_credentials
    save_credentials("test-host", ssh_key="-----BEGIN...")
    client.post("/admin/hosts/test-host/credentials", data={
        "auth_method": "password",
        "ssh_password": "newpass",
        "ssh_key": "",
        "sudo_password": "",
    })
    creds = get_credentials("test-host")
    assert "ssh_key" not in creds
    assert creds["ssh_password"] == "newpass"


# ---------------------------------------------------------------------------
# PUT /admin/ssh
# ---------------------------------------------------------------------------

def test_update_ssh_settings(client, config_file):
    response = client.put("/admin/ssh", data={
        "default_user": "ubuntu",
        "default_port": "2222",
        "default_key": "/app/keys/new_key",
        "connect_timeout": "30",
        "command_timeout": "300",
    })
    assert response.status_code == 200
    assert "saved" in response.text.lower()

    raw = yaml.safe_load(config_file.read_text())
    assert raw["ssh"]["default_user"] == "ubuntu"
    assert raw["ssh"]["default_port"] == 2222


def test_update_ssh_invalid_port(client):
    response = client.put("/admin/ssh", data={
        "default_user": "root",
        "default_port": "not-a-number",
        "default_key": "/app/keys/id_ed25519",
        "connect_timeout": "15",
        "command_timeout": "600",
    })
    assert response.status_code == 200
    assert "error" in response.text.lower() or "invalid" in response.text.lower()


def test_delete_host_error_shows_message(client, monkeypatch):
    import app.admin as a
    monkeypatch.setattr(a, "delete_host", lambda slug: (_ for _ in ()).throw(Exception("disk full")))
    response = client.delete("/admin/hosts/test-host")
    assert response.status_code == 200
    assert "disk full" in response.text


def test_update_host_error_shows_message(client, monkeypatch):
    import app.admin as a
    monkeypatch.setattr(a, "update_host", lambda **kw: (_ for _ in ()).throw(Exception("write error")))
    response = client.put("/admin/hosts/test-host", data={"name": "X", "host": "1.2.3.4"})
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
