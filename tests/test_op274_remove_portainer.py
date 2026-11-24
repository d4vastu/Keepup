"""OP#274 — the Portainer integration is removed (Arcane replaced it, OP#273).

An install upgraded from an older Keepup can still carry a ``portainer:`` block
and a stored Portainer API token. Both are dead weight (and the token a dead
secret), so startup deletes them; nothing else may trip over them meanwhile.
"""

from unittest.mock import patch

import pytest
import yaml


def _write_portainer_leftovers(config_file):
    raw = yaml.safe_load(config_file.read_text())
    raw["portainer"] = {"url": "https://portainer.test:9443", "pinned_fingerprint": "AA:BB"}
    config_file.write_text(yaml.dump(raw))
    from app.credentials import save_integration_credentials

    save_integration_credentials("portainer", api_key="old-token")


def test_leftovers_are_removed(config_file, data_dir):
    from app.config_manager import drop_portainer_leftovers, load_config
    from app.credentials import get_integration_credentials

    _write_portainer_leftovers(config_file)
    assert drop_portainer_leftovers() is True
    assert "portainer" not in load_config()
    assert get_integration_credentials("portainer") == {}
    # Hosts and everything else survive.
    assert load_config()["hosts"]


def test_nothing_to_remove(config_file, data_dir):
    from app.config_manager import drop_portainer_leftovers

    assert drop_portainer_leftovers() is False


def test_only_a_stray_token(config_file, data_dir):
    from app.config_manager import drop_portainer_leftovers
    from app.credentials import get_integration_credentials, save_integration_credentials

    save_integration_credentials("portainer", api_key="old-token")
    assert drop_portainer_leftovers() is True
    assert get_integration_credentials("portainer") == {}


@pytest.mark.asyncio
async def test_startup_cleans_up_before_loading_backends(config_file, data_dir):
    import app.main as m
    from app.config_manager import load_config

    _write_portainer_leftovers(config_file)
    with (
        patch.object(m, "_migrate_tofu_certs"),
        patch.object(m, "apply_all_schedules"),
        patch.object(m.scheduler, "start"),
        patch.object(m, "migrate_proxmox_servers", return_value=None),
    ):
        await m._startup()
    assert "portainer" not in load_config()


@pytest.mark.asyncio
async def test_leftover_config_does_not_create_a_backend(config_file, data_dir):
    from app.backend_loader import reload_backends

    _write_portainer_leftovers(config_file)
    assert [b.BACKEND_KEY for b in await reload_backends()] == ["ssh"]


def test_admin_has_no_portainer_card(client):
    html = client.get("/admin/integrations").text
    assert "config-portainer" not in html
    assert "Portainer" not in html


@pytest.mark.parametrize("path", [
    "/admin/integrations/portainer",
    "/admin/integrations/portainer/test",
    "/admin/connections/portainer",
    "/setup/portainer/save",
])
def test_portainer_routes_are_gone(client, path):
    assert client.post(path, data={}).status_code in (404, 405)


def test_setup_summary_lists_arcane(client, config_file, data_dir):
    """The wizard's summary names Arcane where it used to name Portainer."""
    from app.config_manager import save_arcane_config
    from app.credentials import save_integration_credentials

    save_arcane_config("http://arcane.test:3552")
    save_integration_credentials("arcane", api_key="k")
    html = client.get("/setup/summary").text
    assert "Arcane" in html
    assert "Portainer" not in html
