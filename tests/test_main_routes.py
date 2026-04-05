"""Smoke tests for main dashboard routes."""
from unittest.mock import AsyncMock, patch


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
    from unittest.mock import MagicMock, AsyncMock
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
