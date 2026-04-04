"""Tests for main.py background job runners and remaining routes."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Background job runners
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_job_run_host_update_success(config_file):
    import app.main as m

    job_id = "testjob1"
    m._jobs[job_id] = {"done": False, "status": "running", "error": None, "lines": []}
    host = {"name": "Test", "host": "10.0.0.1", "slug": "test"}

    with patch("app.main.run_host_update_buffered", new=AsyncMock(return_value=["line1", "line2"])):
        await m._job_run_host_update(job_id, host)

    assert m._jobs[job_id]["done"] is True
    assert m._jobs[job_id]["status"] == "done"
    assert m._jobs[job_id]["lines"] == ["line1", "line2"]


@pytest.mark.asyncio
async def test_job_run_host_update_failure(config_file):
    import app.main as m

    job_id = "testjob2"
    m._jobs[job_id] = {"done": False, "status": "running", "error": None, "lines": []}
    host = {"name": "Test", "host": "10.0.0.1", "slug": "test"}

    with patch("app.main.run_host_update_buffered", new=AsyncMock(side_effect=Exception("SSH failed"))):
        await m._job_run_host_update(job_id, host)

    assert m._jobs[job_id]["done"] is True
    assert m._jobs[job_id]["status"] == "error"
    assert "SSH failed" in m._jobs[job_id]["error"]


@pytest.mark.asyncio
async def test_job_run_host_restart_success(config_file):
    import app.main as m

    job_id = "testjob3"
    m._jobs[job_id] = {"done": False, "status": "running", "error": None, "lines": []}
    host = {"name": "Test", "host": "10.0.0.1", "slug": "test"}

    with patch("app.main.reboot_host", new=AsyncMock(return_value=["Reboot initiated"])):
        await m._job_run_host_restart(job_id, host)

    assert m._jobs[job_id]["done"] is True
    assert m._jobs[job_id]["status"] == "done"


@pytest.mark.asyncio
async def test_job_run_host_restart_failure(config_file):
    import app.main as m

    job_id = "testjob4"
    m._jobs[job_id] = {"done": False, "status": "running", "error": None, "lines": []}
    host = {"name": "Test", "host": "10.0.0.1", "slug": "test"}

    with patch("app.main.reboot_host", new=AsyncMock(side_effect=Exception("Connection refused"))):
        await m._job_run_host_restart(job_id, host)

    assert m._jobs[job_id]["status"] == "error"
    assert "Connection refused" in m._jobs[job_id]["error"]


@pytest.mark.asyncio
async def test_job_run_stack_update_success(config_file, monkeypatch):
    import app.main as m

    mock_portainer = AsyncMock()
    mock_portainer.update_stack = AsyncMock(return_value={})
    monkeypatch.setattr(m, "portainer", mock_portainer)

    job_id = "testjob5"
    m._jobs[job_id] = {"done": False, "status": "running", "error": None, "lines": []}

    await m._job_run_stack_update(job_id, stack_id=10, endpoint_id=1)

    assert m._jobs[job_id]["done"] is True
    assert m._jobs[job_id]["status"] == "done"
    assert "Stack updated" in m._jobs[job_id]["lines"][0]


@pytest.mark.asyncio
async def test_job_run_stack_update_failure(config_file, monkeypatch):
    import app.main as m

    mock_portainer = AsyncMock()
    mock_portainer.update_stack = AsyncMock(side_effect=Exception("Portainer error"))
    monkeypatch.setattr(m, "portainer", mock_portainer)

    job_id = "testjob6"
    m._jobs[job_id] = {"done": False, "status": "running", "error": None, "lines": []}

    await m._job_run_stack_update(job_id, stack_id=10, endpoint_id=1)

    assert m._jobs[job_id]["status"] == "error"
    assert "Portainer error" in m._jobs[job_id]["error"]


# ---------------------------------------------------------------------------
# POST /api/host/{slug}/update
# ---------------------------------------------------------------------------

def test_host_update_triggers_job(client):
    with patch("app.main.run_host_update_buffered", new=AsyncMock(return_value=[])):
        response = client.post("/api/host/test-host/update")
    assert response.status_code == 200
    assert "Running" in response.text or "job" in response.text.lower()


def test_host_update_unknown_slug(client):
    response = client.post("/api/host/does-not-exist/update")
    assert response.status_code == 200
    assert "does-not-exist" in response.text or "error" in response.text.lower()


# ---------------------------------------------------------------------------
# POST /api/host/{slug}/restart
# ---------------------------------------------------------------------------

def test_host_restart_triggers_job(client):
    with patch("app.main.reboot_host", new=AsyncMock(return_value=[])):
        response = client.post("/api/host/test-host/restart")
    assert response.status_code == 200


def test_host_restart_unknown_slug(client):
    response = client.post("/api/host/does-not-exist/restart")
    assert response.status_code == 200
    assert "does-not-exist" in response.text or "error" in response.text.lower()


# ---------------------------------------------------------------------------
# GET /api/docker/check
# ---------------------------------------------------------------------------

def test_docker_check_with_portainer(client, monkeypatch):
    import app.main as m

    mock_portainer = MagicMock()
    mock_portainer.get_stacks_with_update_status = AsyncMock(return_value=[
        {"id": 1, "name": "sonarr", "endpoint_id": 1, "endpoint_name": "primary",
         "update_status": "up_to_date", "images": []}
    ])
    monkeypatch.setattr(m, "portainer", mock_portainer)

    response = client.get("/api/docker/check")
    assert response.status_code == 200
    assert "sonarr" in response.text or "up to date" in response.text.lower()


def test_docker_check_portainer_error(client, monkeypatch):
    import app.main as m

    mock_portainer = MagicMock()
    mock_portainer.get_stacks_with_update_status = AsyncMock(side_effect=Exception("API error"))
    monkeypatch.setattr(m, "portainer", mock_portainer)

    response = client.get("/api/docker/check")
    assert response.status_code == 200
    assert "API error" in response.text


# ---------------------------------------------------------------------------
# POST /api/docker/stack/{stack_id}/update
# ---------------------------------------------------------------------------

def test_stack_update_triggers_job(client, monkeypatch):
    import app.main as m

    mock_portainer = MagicMock()
    mock_portainer.update_stack = AsyncMock(return_value={})
    monkeypatch.setattr(m, "portainer", mock_portainer)

    response = client.post("/api/docker/stack/10/update?endpoint_id=1")
    assert response.status_code == 200


def test_stack_update_without_portainer(client, monkeypatch):
    import app.main as m
    monkeypatch.setattr(m, "portainer", None)

    response = client.post("/api/docker/stack/10/update?endpoint_id=1")
    assert response.status_code == 200
    assert "not configured" in response.text.lower() or "portainer" in response.text.lower()


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
