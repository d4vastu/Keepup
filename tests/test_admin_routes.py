"""Integration tests for admin panel routes."""
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


def test_admin_page_shows_portainer_configured(client):
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
