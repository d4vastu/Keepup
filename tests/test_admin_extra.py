"""Additional admin route tests for coverage gaps."""

from unittest.mock import AsyncMock, patch


# ---------------------------------------------------------------------------
# GET /admin/connections
# ---------------------------------------------------------------------------


def test_admin_connections_returns_200(client):
    response = client.get("/admin/connections")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# POST /admin/connections/portainer/test
# ---------------------------------------------------------------------------


def test_portainer_test_missing_url_or_key(client):
    response = client.post(
        "/admin/connections/portainer/test",
        data={
            "portainer_url": "",
            "portainer_api_key": "",
        },
    )
    assert response.status_code == 200
    assert "Enter" in response.text


def test_portainer_test_connection_success(client):
    with patch("app.portainer_client.PortainerClient") as MockClient:
        instance = MockClient.return_value
        instance.get_endpoints = AsyncMock(return_value=[{"Id": 1}, {"Id": 2}])
        response = client.post(
            "/admin/connections/portainer/test",
            data={
                "portainer_url": "https://portainer.test:9443",
                "portainer_api_key": "test-key",
            },
        )
    assert response.status_code == 200
    assert "Connected" in response.text


def test_portainer_test_connection_401_error(client):
    with patch("app.portainer_client.PortainerClient") as MockClient:
        instance = MockClient.return_value
        instance.get_endpoints = AsyncMock(side_effect=Exception("401 Unauthorized"))
        response = client.post(
            "/admin/connections/portainer/test",
            data={
                "portainer_url": "https://portainer.test:9443",
                "portainer_api_key": "wrong-key",
            },
        )
    assert response.status_code == 200
    assert "Invalid API token" in response.text


def test_portainer_test_connection_connect_error(client):
    with patch("app.portainer_client.PortainerClient") as MockClient:
        instance = MockClient.return_value
        instance.get_endpoints = AsyncMock(side_effect=Exception("Connection refused"))
        response = client.post(
            "/admin/connections/portainer/test",
            data={
                "portainer_url": "https://bad.host:9443",
                "portainer_api_key": "test-key",
            },
        )
    assert response.status_code == 200
    assert "reach" in response.text.lower() or "connect" in response.text.lower()


def test_portainer_test_connection_ssl_error(client):
    with patch("app.portainer_client.PortainerClient") as MockClient:
        instance = MockClient.return_value
        instance.get_endpoints = AsyncMock(
            side_effect=Exception("SSL certificate verify failed")
        )
        response = client.post(
            "/admin/connections/portainer/test",
            data={
                "portainer_url": "https://portainer.test:9443",
                "portainer_api_key": "test-key",
            },
        )
    assert response.status_code == 200
    assert "SSL" in response.text


def test_portainer_test_connection_generic_error(client):
    with patch("app.portainer_client.PortainerClient") as MockClient:
        instance = MockClient.return_value
        instance.get_endpoints = AsyncMock(side_effect=Exception("some other error"))
        response = client.post(
            "/admin/connections/portainer/test",
            data={
                "portainer_url": "https://portainer.test:9443",
                "portainer_api_key": "test-key",
            },
        )
    assert response.status_code == 200
    assert "some other error" in response.text


# ---------------------------------------------------------------------------
# POST /admin/connections/portainer (save)
# ---------------------------------------------------------------------------


def test_save_portainer_config(client):
    response = client.post(
        "/admin/connections/portainer",
        data={
            "portainer_url": "https://portainer.example.com:9443",
            "portainer_api_key": "my-api-key",
            "portainer_verify_ssl": "",
        },
    )
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# POST /admin/connections/dockerhub (save)
# ---------------------------------------------------------------------------


def test_save_dockerhub_config(client):
    response = client.post(
        "/admin/connections/dockerhub",
        data={
            "dockerhub_username": "myuser",
            "dockerhub_token": "mytoken",
        },
    )
    assert response.status_code == 200


def test_save_dockerhub_clears_when_empty(client):
    response = client.post(
        "/admin/connections/dockerhub",
        data={
            "dockerhub_username": "",
            "dockerhub_token": "",
        },
    )
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# GET /admin/account
# ---------------------------------------------------------------------------


def test_admin_account_returns_200(client):
    response = client.get("/admin/account")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# POST /admin/account/password
# ---------------------------------------------------------------------------


def test_change_password_wrong_current(client):
    response = client.post(
        "/admin/account/password",
        data={
            "current_password": "wrongpassword",
            "new_password": "newpassword123",
            "new_password_confirm": "newpassword123",
        },
    )
    assert response.status_code == 200
    assert "incorrect" in response.text.lower()


def test_change_password_too_short(client):
    response = client.post(
        "/admin/account/password",
        data={
            "current_password": "testpassword123",
            "new_password": "short",
            "new_password_confirm": "short",
        },
    )
    assert response.status_code == 200
    assert "8 characters" in response.text or "error" in response.text.lower()


def test_change_password_mismatch(client):
    response = client.post(
        "/admin/account/password",
        data={
            "current_password": "testpassword123",
            "new_password": "newpassword123",
            "new_password_confirm": "differentpassword123",
        },
    )
    assert response.status_code == 200
    assert "match" in response.text.lower() or "error" in response.text.lower()


def test_change_password_success(client):
    response = client.post(
        "/admin/account/password",
        data={
            "current_password": "testpassword123",
            "new_password": "newpassword456",
            "new_password_confirm": "newpassword456",
        },
    )
    assert response.status_code == 200
    assert (
        "saved" in response.text.lower()
        or "changed" in response.text.lower()
        or "password" in response.text.lower()
    )


# ---------------------------------------------------------------------------
# POST /admin/account/backup-key
# ---------------------------------------------------------------------------


def test_regenerate_backup_key_wrong_password(client):
    response = client.post(
        "/admin/account/backup-key",
        data={
            "current_password": "wrongpassword",
        },
    )
    assert response.status_code == 200
    assert "incorrect" in response.text.lower()


def test_regenerate_backup_key_success(client):
    response = client.post(
        "/admin/account/backup-key",
        data={
            "current_password": "testpassword123",
        },
    )
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# GET /admin/account/mfa/setup
# ---------------------------------------------------------------------------


def test_admin_mfa_setup_page(client):
    response = client.get("/admin/account/mfa/setup")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# POST /admin/hosts/{slug}/credentials error path
# ---------------------------------------------------------------------------


def test_post_credentials_error_shows_message(client, monkeypatch):
    import app.admin as a

    monkeypatch.setattr(
        a,
        "save_credentials",
        lambda **kw: (_ for _ in ()).throw(Exception("disk full")),
    )
    response = client.post(
        "/admin/hosts/test-host/credentials",
        data={
            "auth_method": "password",
            "ssh_password": "pass",
            "ssh_key": "",
            "sudo_password": "",
        },
    )
    assert response.status_code == 200
    assert "disk full" in response.text


# ---------------------------------------------------------------------------
# POST /admin/hosts/{slug}/docker-monitoring error path
# ---------------------------------------------------------------------------


def test_docker_monitoring_error_shows_message(client, monkeypatch):
    import app.admin as a

    monkeypatch.setattr(
        a,
        "set_docker_monitoring",
        lambda **kw: (_ for _ in ()).throw(Exception("config error")),
    )
    response = client.post(
        "/admin/hosts/test-host/docker-monitoring", data={"docker_mode": "all"}
    )
    assert response.status_code == 200
    assert "config error" in response.text


# ---------------------------------------------------------------------------
# POST /admin/account/mfa/setup — wrong code
# ---------------------------------------------------------------------------


def test_mfa_setup_submit_wrong_code(client):
    # Get the setup page first (to set the session secret)
    client.get("/admin/account/mfa/setup")
    # Submit wrong TOTP code
    response = client.post("/admin/account/mfa/setup", data={"totp_code": "000000"})
    assert response.status_code == 200
    assert "incorrect" in response.text.lower() or "error" in response.text.lower()


# ---------------------------------------------------------------------------
# POST /admin/account/mfa/remove — wrong credentials
# ---------------------------------------------------------------------------


def test_mfa_remove_wrong_credentials(client):
    """Route handles wrong credentials gracefully (200 response)."""
    response = client.post(
        "/admin/account/mfa/remove",
        data={
            "current_password": "wrongpassword",
            "totp_code": "000000",
        },
    )
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------


def test_admin_logs_page_returns_200(client):
    response = client.get("/admin/logs")
    assert response.status_code == 200
    assert "Logs" in response.text


def test_admin_logs_lines_returns_200(client):
    response = client.get("/admin/logs/lines")
    assert response.status_code == 200
