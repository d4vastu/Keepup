"""Tests for main.py background job runners and remaining routes."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Background job runners
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_job_run_host_update_success(config_file, data_dir):
    import app.main as m

    job_id = "testjob1"
    m._jobs[job_id] = {"done": False, "status": "running", "error": None, "lines": []}
    host = {"name": "Test", "host": "10.0.0.1", "slug": "test"}
    creds = {}

    with patch("app.main.run_host_update_buffered", new=AsyncMock(return_value=["line1", "line2"])):
        await m._job_run_host_update(job_id, host, creds)

    assert m._jobs[job_id]["done"] is True
    assert m._jobs[job_id]["status"] == "done"
    assert m._jobs[job_id]["lines"] == ["line1", "line2"]


@pytest.mark.asyncio
async def test_job_run_host_update_failure(config_file, data_dir):
    import app.main as m

    job_id = "testjob2"
    m._jobs[job_id] = {"done": False, "status": "running", "error": None, "lines": []}
    host = {"name": "Test", "host": "10.0.0.1", "slug": "test"}
    creds = {}

    with patch("app.main.run_host_update_buffered", new=AsyncMock(side_effect=Exception("SSH failed"))):
        await m._job_run_host_update(job_id, host, creds)

    assert m._jobs[job_id]["done"] is True
    assert m._jobs[job_id]["status"] == "error"
    assert "SSH failed" in m._jobs[job_id]["error"]


@pytest.mark.asyncio
async def test_job_run_host_restart_success(config_file, data_dir):
    import app.main as m

    job_id = "testjob3"
    m._jobs[job_id] = {"done": False, "status": "running", "error": None, "lines": []}
    host = {"name": "Test", "host": "10.0.0.1", "slug": "test"}
    creds = {}

    with patch("app.main.reboot_host", new=AsyncMock(return_value=["Reboot initiated"])):
        await m._job_run_host_restart(job_id, host, creds)

    assert m._jobs[job_id]["done"] is True
    assert m._jobs[job_id]["status"] == "done"


@pytest.mark.asyncio
async def test_job_run_host_restart_failure(config_file, data_dir):
    import app.main as m

    job_id = "testjob4"
    m._jobs[job_id] = {"done": False, "status": "running", "error": None, "lines": []}
    host = {"name": "Test", "host": "10.0.0.1", "slug": "test"}
    creds = {}

    with patch("app.main.reboot_host", new=AsyncMock(side_effect=Exception("Connection refused"))):
        await m._job_run_host_restart(job_id, host, creds)

    assert m._jobs[job_id]["status"] == "error"
    assert "Connection refused" in m._jobs[job_id]["error"]


@pytest.mark.asyncio
async def test_job_run_stack_update_success(config_file, data_dir, monkeypatch):
    import app.main as m
    import app.backend_loader as bl
    from unittest.mock import MagicMock

    mock_backend = MagicMock()
    mock_backend.BACKEND_KEY = "portainer"
    mock_backend.update_stack = AsyncMock(return_value=None)
    monkeypatch.setattr(bl, "_backends", [mock_backend])

    job_id = "testjob5"
    m._jobs[job_id] = {"done": False, "status": "running", "error": None, "lines": []}

    await m._job_run_stack_update(job_id, backend_key="portainer", ref="10:1")

    assert m._jobs[job_id]["done"] is True
    assert m._jobs[job_id]["status"] == "done"
    assert "Stack updated" in m._jobs[job_id]["lines"][0]
    mock_backend.update_stack.assert_called_once_with("10:1")


@pytest.mark.asyncio
async def test_job_run_stack_update_failure(config_file, data_dir, monkeypatch):
    import app.main as m
    import app.backend_loader as bl
    from unittest.mock import MagicMock

    mock_backend = MagicMock()
    mock_backend.BACKEND_KEY = "portainer"
    mock_backend.update_stack = AsyncMock(side_effect=Exception("Portainer error"))
    monkeypatch.setattr(bl, "_backends", [mock_backend])

    job_id = "testjob6"
    m._jobs[job_id] = {"done": False, "status": "running", "error": None, "lines": []}

    await m._job_run_stack_update(job_id, backend_key="portainer", ref="10:1")

    assert m._jobs[job_id]["status"] == "error"
    assert "Portainer error" in m._jobs[job_id]["error"]


@pytest.mark.asyncio
async def test_job_run_stack_update_unknown_backend(config_file, data_dir, monkeypatch):
    import app.main as m
    import app.backend_loader as bl
    monkeypatch.setattr(bl, "_backends", [])

    job_id = "testjob7"
    m._jobs[job_id] = {"done": False, "status": "running", "error": None, "lines": []}

    await m._job_run_stack_update(job_id, backend_key="ghost", ref="10:1")

    assert m._jobs[job_id]["status"] == "error"
    assert "ghost" in m._jobs[job_id]["error"]


# ---------------------------------------------------------------------------
# POST /api/host/{slug}/update — sudo modal logic
# ---------------------------------------------------------------------------

def test_host_update_root_user_no_modal(client):
    """Root user (SSH default) → no sudo modal, job starts immediately."""
    with patch("app.main.run_host_update_buffered", new=AsyncMock(return_value=[])):
        response = client.post("/api/host/test-host/update")
    assert response.status_code == 200
    # Should get job poll, not a sudo modal
    assert "sudo" not in response.text.lower()


def test_host_update_nonroot_no_sudo_shows_modal(client, data_dir):
    """Non-root user with no saved sudo password → modal returned."""
    with patch("app.main._needs_sudo", return_value=True):
        response = client.post("/api/host/test-host/update")
    assert response.status_code == 200
    assert "sudo" in response.text.lower()
    assert "Use once" in response.text or "Save" in response.text


def test_host_update_nonroot_sudo_provided_once(client, data_dir):
    """Non-root + sudo_password in form → starts job (does not save)."""
    from app.credentials import get_credentials
    with patch("app.main._needs_sudo", return_value=True), \
         patch("app.main.run_host_update_buffered", new=AsyncMock(return_value=[])):
        response = client.post("/api/host/test-host/update", data={
            "sudo_password": "mysudo",
            "save_sudo": "",
        })
    assert response.status_code == 200
    # Job started, not a modal
    assert "Use once" not in response.text
    # Credential NOT saved
    assert "sudo_password" not in get_credentials("test-host")


def test_host_update_nonroot_sudo_saved(client, data_dir):
    """Non-root + sudo_password + save_sudo=save → saves credential and starts job."""
    from app.credentials import get_credentials
    with patch("app.main._needs_sudo", return_value=True), \
         patch("app.main.run_host_update_buffered", new=AsyncMock(return_value=[])):
        response = client.post("/api/host/test-host/update", data={
            "sudo_password": "mysudo",
            "save_sudo": "save",
        })
    assert response.status_code == 200
    assert get_credentials("test-host").get("sudo_password") == "mysudo"


def test_host_update_nonroot_saved_sudo_used_automatically(client, data_dir):
    """If sudo password already saved, no modal — job runs directly."""
    from app.credentials import save_credentials
    save_credentials("test-host", sudo_password="presaved")
    with patch("app.main._needs_sudo", return_value=True), \
         patch("app.main.run_host_update_buffered", new=AsyncMock(return_value=[])):
        response = client.post("/api/host/test-host/update")
    assert response.status_code == 200
    assert "Use once" not in response.text


def test_host_update_unknown_slug(client):
    response = client.post("/api/host/does-not-exist/update")
    assert response.status_code == 200
    assert "does-not-exist" in response.text or "error" in response.text.lower()


# ---------------------------------------------------------------------------
# POST /api/host/{slug}/restart — sudo modal logic
# ---------------------------------------------------------------------------

def test_host_restart_root_user_no_modal(client):
    with patch("app.main.reboot_host", new=AsyncMock(return_value=[])):
        response = client.post("/api/host/test-host/restart")
    assert response.status_code == 200
    assert "sudo" not in response.text.lower()


def test_host_restart_nonroot_no_sudo_shows_modal(client, data_dir):
    with patch("app.main._needs_sudo", return_value=True):
        response = client.post("/api/host/test-host/restart")
    assert response.status_code == 200
    assert "sudo" in response.text.lower()


def test_host_restart_unknown_slug(client):
    response = client.post("/api/host/does-not-exist/restart")
    assert response.status_code == 200
    assert "does-not-exist" in response.text or "error" in response.text.lower()


def test_host_restart_nonroot_sudo_saved_automatically(client, data_dir):
    """Non-root + sudo_password + save_sudo=save → saves credential and starts job."""
    from app.credentials import get_credentials
    with patch("app.main._needs_sudo", return_value=True), \
         patch("app.main.reboot_host", new=AsyncMock(return_value=[])):
        response = client.post("/api/host/test-host/restart", data={
            "sudo_password": "mysudo",
            "save_sudo": "save",
        })
    assert response.status_code == 200
    assert get_credentials("test-host").get("sudo_password") == "mysudo"


def test_docker_check_raises_exception(client, monkeypatch):
    """Exception in gather propagates to error template."""
    import app.backend_loader as bl

    def bad_backend():
        b = MagicMock()
        b.BACKEND_KEY = "portainer"
        b.get_stacks_with_update_status = AsyncMock(side_effect=Exception("boom"))
        return b

    monkeypatch.setattr(bl, "_backends", [bad_backend()])
    response = client.get("/api/docker/check")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# GET /api/docker/check
# ---------------------------------------------------------------------------

def _make_mock_backend(key: str, stacks=None, error=None):
    b = MagicMock()
    b.BACKEND_KEY = key
    if error:
        b.get_stacks_with_update_status = AsyncMock(side_effect=error)
    else:
        b.get_stacks_with_update_status = AsyncMock(return_value=stacks or [])
    b.update_stack = AsyncMock(return_value=None)
    return b


def test_docker_check_with_backend(client, monkeypatch):
    import app.backend_loader as bl
    stacks = [{"id": "p/1:1", "name": "sonarr", "endpoint_id": "1",
               "endpoint_name": "primary", "update_status": "up_to_date",
               "images": [], "update_path": "portainer/1:1"}]
    monkeypatch.setattr(bl, "_backends", [_make_mock_backend("portainer", stacks)])
    response = client.get("/api/docker/check")
    assert response.status_code == 200
    assert "sonarr" in response.text or "up to date" in response.text.lower()


def test_docker_check_no_backends_configured(client, monkeypatch):
    import app.backend_loader as bl
    # SSH backend with no docker_mode hosts → treated as inactive
    ssh_b = _make_mock_backend("ssh")
    monkeypatch.setattr(bl, "_backends", [ssh_b])
    response = client.get("/api/docker/check")
    assert response.status_code == 200
    assert "not configured" in response.text.lower() or "No container" in response.text


def test_docker_check_backend_error(client, monkeypatch):
    import app.backend_loader as bl
    monkeypatch.setattr(bl, "_backends", [_make_mock_backend("portainer", error=Exception("API error"))])
    response = client.get("/api/docker/check")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# POST /api/docker/stack/{backend}/{ref}/update
# ---------------------------------------------------------------------------

def test_stack_update_triggers_job(client, monkeypatch):
    import app.backend_loader as bl
    monkeypatch.setattr(bl, "_backends", [_make_mock_backend("portainer")])
    response = client.post("/api/docker/stack/portainer/10:1/update")
    assert response.status_code == 200


def test_stack_update_unknown_backend(client, monkeypatch):
    import app.backend_loader as bl
    monkeypatch.setattr(bl, "_backends", [])
    response = client.post("/api/docker/stack/ghost/10:1/update")
    assert response.status_code == 200
    assert "not configured" in response.text.lower() or "ghost" in response.text.lower()


def test_stack_update_ssh_backend(client, monkeypatch):
    import app.backend_loader as bl
    monkeypatch.setattr(bl, "_backends", [_make_mock_backend("ssh")])
    response = client.post("/api/docker/stack/ssh/my-server/myapp/update")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# GET /api/jobs/{job_id}
# ---------------------------------------------------------------------------

def test_job_status_running(client):
    import app.main as m
    m._jobs["runjob"] = {"done": False, "status": "running", "error": None, "lines": []}

    response = client.get("/api/jobs/runjob")
    assert response.status_code == 200
    assert "running" in response.text.lower() or "runjob" in response.text


def test_job_status_done(client):
    import app.main as m
    m._jobs["donejob"] = {"done": True, "status": "done", "error": None, "lines": ["Updated."]}

    response = client.get("/api/jobs/donejob")
    assert response.status_code == 200


def test_job_status_error(client):
    import app.main as m
    m._jobs["errjob"] = {"done": True, "status": "error", "error": "SSH failed", "lines": []}

    response = client.get("/api/jobs/errjob")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# _classify_log_line and _format_log_lines
# ---------------------------------------------------------------------------

def test_classify_log_line_error():
    from app.main import _classify_log_line
    assert _classify_log_line("E: some error occurred") == "log-line-red"
    assert _classify_log_line("Failed to fetch") == "log-line-red"
    assert _classify_log_line("dpkg: error processing") == "log-line-red"


def test_classify_log_line_warning():
    from app.main import _classify_log_line
    assert _classify_log_line("WARNING: something odd") == "log-line-yellow"
    assert _classify_log_line("System reboot required") == "log-line-yellow"


def test_classify_log_line_dim():
    from app.main import _classify_log_line
    assert _classify_log_line("Get:1 http://archive.ubuntu.com focal InRelease") == "log-line-dim"
    assert _classify_log_line("Unpacking libssl1.1") == "log-line-dim"
    assert _classify_log_line("Setting up libssl1.1") == "log-line-dim"
    assert _classify_log_line("Processing triggers for man-db") == "log-line-dim"


def test_classify_log_line_ok():
    from app.main import _classify_log_line
    assert _classify_log_line("5 upgraded, 0 newly installed") == "log-line-ok"
    assert _classify_log_line("1 installed") == "log-line-ok"
    assert _classify_log_line("done") == "log-line-ok"


def test_classify_log_line_white():
    from app.main import _classify_log_line
    assert _classify_log_line("Reading package lists...") == "log-line-white"
    assert _classify_log_line("Building dependency tree") == "log-line-white"


def test_format_log_lines_produces_html():
    from app.main import _format_log_lines
    result = _format_log_lines(["5 upgraded", "E: some error", "Get:1 http://example.com"])
    assert '<div class="log-line-ok">' in result
    assert '<div class="log-line-red">' in result
    assert '<div class="log-line-dim">' in result


def test_format_log_lines_escapes_html():
    from app.main import _format_log_lines
    result = _format_log_lines(["<script>alert('xss')</script>"])
    assert "<script>" not in result
    assert "&lt;script&gt;" in result


def test_format_log_lines_empty():
    from app.main import _format_log_lines
    assert _format_log_lines([]) == ""


# ---------------------------------------------------------------------------
# GET /api/jobs/{job_id}/modal
# ---------------------------------------------------------------------------

def test_job_modal_running(client):
    import app.main as m
    m._jobs["modaljob1"] = {
        "done": False, "status": "running", "error": None, "lines": ["Installing..."],
        "type": "os_upgrade", "label": "My Server", "sub": "10.0.0.1",
    }
    response = client.get("/api/jobs/modaljob1/modal")
    assert response.status_code == 200
    assert "My Server" in response.text
    assert "Installing" in response.text


def test_job_modal_done_success(client):
    import app.main as m
    m._jobs["modaljob2"] = {
        "done": True, "status": "done", "error": None, "lines": ["5 upgraded"],
        "type": "os_upgrade", "label": "My Server", "sub": "10.0.0.1",
    }
    response = client.get("/api/jobs/modaljob2/modal")
    assert response.status_code == 200
    assert "My Server" in response.text
    assert "upgraded" in response.text.lower()


def test_job_modal_done_error(client):
    import app.main as m
    m._jobs["modaljob3"] = {
        "done": True, "status": "error", "error": "Connection refused", "lines": [],
        "type": "os_upgrade", "label": "My Server", "sub": "10.0.0.1",
    }
    response = client.get("/api/jobs/modaljob3/modal")
    assert response.status_code == 200
    assert "Connection refused" in response.text


def test_job_modal_container_redeploy(client):
    import app.main as m
    m._jobs["modaljob4"] = {
        "done": False, "status": "running", "error": None, "lines": [],
        "type": "container_redeploy", "label": "sonarr", "sub": "portainer",
    }
    response = client.get("/api/jobs/modaljob4/modal")
    assert response.status_code == 200
    assert "sonarr" in response.text


def test_job_modal_os_restart(client):
    import app.main as m
    m._jobs["modaljob5"] = {
        "done": True, "status": "done", "error": None, "lines": ["Reboot initiated"],
        "type": "os_restart", "label": "My Server", "sub": "10.0.0.1",
    }
    response = client.get("/api/jobs/modaljob5/modal")
    assert response.status_code == 200
    assert "My Server" in response.text


def test_job_modal_not_found(client):
    response = client.get("/api/jobs/nonexistent/modal")
    assert response.status_code == 200
    assert response.text == ""


# ---------------------------------------------------------------------------
# GET /api/jobs/{job_id}/modal-body
# ---------------------------------------------------------------------------

def test_job_modal_body_returns_log_html(client):
    import app.main as m
    m._jobs["bodyjob1"] = {
        "done": False, "status": "running", "error": None, "lines": ["5 upgraded", "E: error"],
        "type": "os_upgrade", "label": "My Server", "sub": "10.0.0.1",
    }
    response = client.get("/api/jobs/bodyjob1/modal-body")
    assert response.status_code == 200
    assert "log-line-ok" in response.text
    assert "log-line-red" in response.text


def test_job_modal_body_empty_lines(client):
    import app.main as m
    m._jobs["bodyjob2"] = {
        "done": False, "status": "running", "error": None, "lines": [],
        "type": "os_upgrade", "label": "My Server", "sub": "10.0.0.1",
    }
    response = client.get("/api/jobs/bodyjob2/modal-body")
    assert response.status_code == 200
    assert response.text == ""


def test_job_modal_body_not_found(client):
    response = client.get("/api/jobs/nonexistent/modal-body")
    assert response.status_code == 200
    assert response.text == ""
