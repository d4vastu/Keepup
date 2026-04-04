"""Tests for backend_loader module."""
import pytest


@pytest.fixture(autouse=True)
def _setup(config_file, data_dir):
    """Each test gets isolated config and data dirs."""


@pytest.mark.asyncio
async def test_reload_backends_no_portainer_adds_ssh():
    """Without Portainer config, only SSH backend is added."""
    import app.backend_loader as bl
    backends = await bl.reload_backends()
    keys = [b.BACKEND_KEY for b in backends]
    assert "ssh" in keys
    assert "portainer" not in keys


@pytest.mark.asyncio
async def test_reload_backends_with_portainer_ui_config():
    """With Portainer configured via UI, Portainer backend is added."""
    from app.config_manager import save_portainer_config
    from app.credentials import save_integration_credentials
    save_portainer_config(url="https://portainer.test:9443", verify_ssl=False)
    save_integration_credentials("portainer", api_key="test-api-key")

    import app.backend_loader as bl
    backends = await bl.reload_backends()
    keys = [b.BACKEND_KEY for b in backends]
    assert "portainer" in keys
    assert "ssh" in keys


@pytest.mark.asyncio
async def test_get_backends_returns_current_list():
    import app.backend_loader as bl
    await bl.reload_backends()
    backends = bl.get_backends()
    assert isinstance(backends, list)
    assert len(backends) >= 1


@pytest.mark.asyncio
async def test_get_dockerhub_creds_none_without_config():
    import app.backend_loader as bl
    creds = bl.get_dockerhub_creds()
    assert creds is None


@pytest.mark.asyncio
async def test_get_dockerhub_creds_from_ui_config():
    from app.config_manager import save_dockerhub_config
    from app.credentials import save_integration_credentials
    save_dockerhub_config(username="myuser")
    save_integration_credentials("dockerhub", token="mytoken")

    import app.backend_loader as bl
    creds = bl.get_dockerhub_creds()
    assert creds is not None
    assert creds["username"] == "myuser"
    assert creds["token"] == "mytoken"


@pytest.mark.asyncio
async def test_reload_backends_propagates_to_scheduler(monkeypatch):
    """reload_backends calls set_backends on scheduler and auto_updates_router."""
    import app.backend_loader as bl

    set_scheduler_called = []
    set_auto_updates_called = []

    monkeypatch.setattr("app.auto_update_scheduler.set_backends",
                        lambda b: set_scheduler_called.append(b))
    monkeypatch.setattr("app.auto_updates_router.set_backends",
                        lambda b: set_auto_updates_called.append(b))

    await bl.reload_backends()

    assert len(set_scheduler_called) == 1
    assert len(set_auto_updates_called) == 1
