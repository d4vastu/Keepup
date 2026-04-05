"""Targeted tests to cover remaining uncovered lines in app/main.py."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Line 41: no admin → redirect to /setup
# ---------------------------------------------------------------------------

def test_protected_route_no_admin_redirects_to_setup(config_file, data_dir, monkeypatch):
    """When no admin account exists, any protected route redirects to /setup."""
    monkeypatch.setenv("PORTAINER_URL", "https://portainer.test:9443")
    monkeypatch.setenv("PORTAINER_API_KEY", "test-api-key")
    monkeypatch.setenv("PORTAINER_VERIFY_SSL", "false")

    from app.main import app
    # No create_admin call — credential store is empty
    tc = TestClient(app, raise_server_exceptions=True)
    response = tc.get("/dashboard", follow_redirects=False)
    assert response.status_code in (302, 303)
    assert "/setup" in response.headers["location"]


# ---------------------------------------------------------------------------
# Lines 72-86: _check_version_notification()
# ---------------------------------------------------------------------------

def test_version_notification_fresh_install(tmp_path, monkeypatch):
    """Fresh install: no existing version file → writes version, no notification."""
    import app.main as m

    version_file = tmp_path / ".app_version"
    monkeypatch.setattr(m, "_VERSION_FILE", version_file)

    with patch("app.notifications.notify") as mock_notify:
        m._check_version_notification()

    mock_notify.assert_not_called()
    assert version_file.read_text().strip() == m.APP_VERSION


def test_version_notification_upgrade(tmp_path, monkeypatch):
    """Upgrade: stored version differs → notification sent and version file updated."""
    import app.main as m

    version_file = tmp_path / ".app_version"
    version_file.write_text("0.0.1")
    monkeypatch.setattr(m, "_VERSION_FILE", version_file)

    with patch("app.notifications.notify") as mock_notify:
        m._check_version_notification()

    mock_notify.assert_called_once()
    call_args = mock_notify.call_args[0]
    assert m.APP_VERSION in call_args[0]  # title mentions new version
    assert version_file.read_text().strip() == m.APP_VERSION


def test_version_notification_same_version(tmp_path, monkeypatch):
    """Same version: no notification, version file unchanged."""
    import app.main as m

    version_file = tmp_path / ".app_version"
    version_file.write_text(m.APP_VERSION)
    monkeypatch.setattr(m, "_VERSION_FILE", version_file)

    with patch("app.notifications.notify") as mock_notify:
        m._check_version_notification()

    mock_notify.assert_not_called()


def test_version_notification_exception_silenced(tmp_path, monkeypatch):
    """Exception in version check is silently swallowed."""
    import app.main as m

    # Point to a file whose parent doesn't exist → write_text will raise
    bad_path = tmp_path / "nonexistent_dir" / ".app_version"
    monkeypatch.setattr(m, "_VERSION_FILE", bad_path)

    # Should not raise
    m._check_version_notification()


# ---------------------------------------------------------------------------
# Lines 91-94: _startup() async handler
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_startup_calls_all_hooks(monkeypatch):
    """_startup() calls _check_version_notification, reload_backends,
    apply_all_schedules, and scheduler.start."""
    import app.main as m

    mock_check = MagicMock()
    mock_reload = AsyncMock()
    mock_apply = MagicMock()
    mock_start = MagicMock()

    monkeypatch.setattr(m, "_check_version_notification", mock_check)
    monkeypatch.setattr(m, "reload_backends", mock_reload)
    monkeypatch.setattr(m, "apply_all_schedules", mock_apply)
    monkeypatch.setattr(m.scheduler, "start", mock_start)

    await m._startup()

    mock_check.assert_called_once()
    mock_reload.assert_awaited_once()
    mock_apply.assert_called_once()
    mock_start.assert_called_once()


# ---------------------------------------------------------------------------
# Lines 311-312: check_and_notify exception swallowed inside docker_check
# ---------------------------------------------------------------------------

def test_docker_check_notify_exception_is_swallowed(client, monkeypatch):
    """If check_and_notify raises, docker_check still returns a 200 response."""
    import app.backend_loader as bl

    ssh_b = MagicMock()
    ssh_b.BACKEND_KEY = "portainer"
    ssh_b.get_stacks_with_update_status = AsyncMock(return_value=[
        {"name": "myapp", "status": "up-to-date", "backend_key": "portainer", "ref": "myapp"}
    ])
    monkeypatch.setattr(bl, "_backends", [ssh_b])

    with patch("app.update_notifier.check_and_notify", side_effect=RuntimeError("notif boom")):
        response = client.get("/api/docker/check")

    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Lines 317-318: outer gather exception → error partial
# ---------------------------------------------------------------------------

def test_docker_check_gather_exception_returns_error_partial(client, monkeypatch):
    """If asyncio.gather itself raises, docker_check returns the error partial."""
    import app.backend_loader as bl
    import app.main as m

    ssh_b = MagicMock()
    ssh_b.BACKEND_KEY = "portainer"
    ssh_b.get_stacks_with_update_status = AsyncMock(return_value=[])
    monkeypatch.setattr(bl, "_backends", [ssh_b])

    async def boom(*args, **kwargs):
        raise RuntimeError("gather exploded")

    monkeypatch.setattr(m.asyncio, "gather", boom)

    response = client.get("/api/docker/check")
    assert response.status_code == 200
    assert "gather exploded" in response.text or "error" in response.text.lower()


# ---------------------------------------------------------------------------
# _newer_version and _fetch_latest_version helpers
# ---------------------------------------------------------------------------

def test_newer_version_returns_true_when_latest_is_higher():
    import app.main as m
    assert m._newer_version("99.0.0") is True


def test_newer_version_returns_false_when_same():
    import app.main as m
    assert m._newer_version(m.APP_VERSION) is False


def test_newer_version_returns_false_on_none():
    import app.main as m
    assert m._newer_version(None) is False


def test_newer_version_returns_false_on_bad_input():
    import app.main as m
    assert m._newer_version("not-a-version") is False


@pytest.mark.asyncio
async def test_fetch_latest_version_success(monkeypatch):
    import app.main as m
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"tag_name": "v1.2.3", "html_url": "https://github.com/example"}
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=resp)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=None)
    with patch("httpx.AsyncClient", return_value=ctx):
        tag, url = await m._fetch_latest_version()
    assert tag == "1.2.3"
    assert "github.com" in url


@pytest.mark.asyncio
async def test_fetch_latest_version_http_error(monkeypatch):
    import app.main as m
    resp = MagicMock()
    resp.status_code = 403
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=resp)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=None)
    with patch("httpx.AsyncClient", return_value=ctx):
        tag, url = await m._fetch_latest_version()
    assert tag is None
    assert url is None


@pytest.mark.asyncio
async def test_fetch_latest_version_exception(monkeypatch):
    import app.main as m
    with patch("httpx.AsyncClient", side_effect=Exception("network down")):
        tag, url = await m._fetch_latest_version()
    assert tag is None
    assert url is None
