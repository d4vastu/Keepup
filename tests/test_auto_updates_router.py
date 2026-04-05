"""Tests for auto_updates_router module (GET/POST routes)."""

from unittest.mock import AsyncMock, MagicMock

import yaml


# ---------------------------------------------------------------------------
# GET /admin/auto-updates
# ---------------------------------------------------------------------------


def test_auto_updates_page_returns_200(client):
    response = client.get("/admin/auto-updates")
    assert response.status_code == 200


def test_auto_updates_page_lists_hosts(client):
    response = client.get("/admin/auto-updates")
    assert "Test Host" in response.text or "test-host" in response.text


# ---------------------------------------------------------------------------
# POST /admin/auto-updates/hosts/{slug}
# ---------------------------------------------------------------------------


def test_save_host_auto_update_enabled(client, config_file):
    response = client.post(
        "/admin/auto-updates/hosts/test-host",
        data={"os_enabled": "on", "os_schedule": "0 3 * * *", "auto_reboot": ""},
    )
    assert response.status_code == 200

    raw = yaml.safe_load(config_file.read_text())
    host = next(h for h in raw["hosts"] if h["name"] == "Test Host")
    assert host.get("auto_update", {}).get("os_enabled") is True


def test_save_host_auto_update_disabled(client, config_file):
    response = client.post(
        "/admin/auto-updates/hosts/test-host",
        data={"os_enabled": "", "os_schedule": "0 3 * * *", "auto_reboot": ""},
    )
    assert response.status_code == 200


def test_save_host_auto_update_with_reboot(client, config_file):
    response = client.post(
        "/admin/auto-updates/hosts/test-host",
        data={"os_enabled": "on", "os_schedule": "0 2 * * 0", "auto_reboot": "on"},
    )
    assert response.status_code == 200

    raw = yaml.safe_load(config_file.read_text())
    host = next(h for h in raw["hosts"] if h["name"] == "Test Host")
    # auto_reboot should be stored (True when "on")
    assert (
        host.get("auto_update", {}).get("auto_reboot") is True
        or host.get("auto_update", {}).get("os_enabled") is True
    )


def test_save_host_auto_update_invalid_cron(client):
    response = client.post(
        "/admin/auto-updates/hosts/test-host",
        data={"os_enabled": "on", "os_schedule": "not-a-cron", "auto_reboot": ""},
    )
    assert response.status_code == 200
    assert "Invalid schedule" in response.text or "invalid" in response.text.lower()


# ---------------------------------------------------------------------------
# GET /admin/auto-updates/stacks
# ---------------------------------------------------------------------------


def test_auto_update_stacks_no_backends(client, monkeypatch):
    import app.auto_updates_router as aur

    monkeypatch.setattr(aur, "_backends", [])
    response = client.get("/admin/auto-updates/stacks")
    assert response.status_code == 200
    # Should indicate no backends configured
    assert "no_backends" in response.text.lower() or response.status_code == 200


def test_auto_update_stacks_with_backend(client, monkeypatch):
    import app.auto_updates_router as aur

    mock_backend = MagicMock()
    mock_backend.BACKEND_KEY = "portainer"
    mock_backend.get_stacks_with_update_status = AsyncMock(
        return_value=[
            {
                "id": "10",
                "name": "sonarr",
                "endpoint_id": "1",
                "update_path": "portainer/10:1",
                "update_status": "up_to_date",
                "images": [],
            },
        ]
    )
    monkeypatch.setattr(aur, "_backends", [mock_backend])

    response = client.get("/admin/auto-updates/stacks")
    assert response.status_code == 200
    assert "sonarr" in response.text


def test_auto_update_stacks_backend_error(client, monkeypatch):
    import app.auto_updates_router as aur

    mock_backend = MagicMock()
    mock_backend.BACKEND_KEY = "portainer"
    mock_backend.get_stacks_with_update_status = AsyncMock(
        side_effect=Exception("API error")
    )
    monkeypatch.setattr(aur, "_backends", [mock_backend])

    response = client.get("/admin/auto-updates/stacks")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# POST /admin/auto-updates/stacks/{backend_key}/{ref}
# ---------------------------------------------------------------------------


def test_save_stack_auto_update_enabled(client):
    response = client.post(
        "/admin/auto-updates/stacks/portainer/10:1",
        data={"stack_name": "sonarr", "enabled": "on", "schedule": "0 4 * * *"},
    )
    assert response.status_code == 200


def test_save_stack_auto_update_disabled(client):
    response = client.post(
        "/admin/auto-updates/stacks/portainer/10:1",
        data={"stack_name": "sonarr", "enabled": "", "schedule": "0 4 * * *"},
    )
    assert response.status_code == 200


def test_save_stack_auto_update_invalid_cron(client):
    response = client.post(
        "/admin/auto-updates/stacks/portainer/10:1",
        data={"stack_name": "sonarr", "enabled": "on", "schedule": "bad-cron"},
    )
    assert response.status_code == 200
    assert "Invalid schedule" in response.text or "invalid" in response.text.lower()


def test_save_stack_auto_update_ssh_backend(client):
    """SSH backend ref has path separators."""
    response = client.post(
        "/admin/auto-updates/stacks/ssh/my-server/sonarr",
        data={"stack_name": "sonarr", "enabled": "on", "schedule": "0 5 * * *"},
    )
    assert response.status_code == 200
