"""Tests for the setup wizard's "add another Proxmox server" flow (OP#212).

The wizard's first-server save writes the legacy ``proxmox:`` block, which the
startup migration later converts into ``proxmox_servers[0]``. That is fine
until a *second* server is added in the same session: the config would then
carry both shapes at once, and ``migrate_proxmox_servers()`` resolves that by
dropping the legacy block as stale. The first server would vanish, and its
hosts — unstamped — would fall through ``server_context()`` onto the second
server. Adding a server would silently re-point the first one's guests.

So "add another" migrates before it appends. Most of this file exists to hold
that behaviour down.
"""

import yaml


def _first_server_the_old_way(client):
    """Complete the wizard's first-server save, exactly as today's flow does."""
    return client.post(
        "/setup/connect/proxmox/save",
        data={
            "proxmox_url": "https://192.168.5.226:8006",
            "proxmox_token_id": "root@pam!A",
            "proxmox_secret": "secret-a",
        },
    )


# ---------------------------------------------------------------------------
# The done step
# ---------------------------------------------------------------------------


def test_done_step_lists_configured_servers(client, config_file, data_dir):
    from app.config_manager import add_proxmox_server

    add_proxmox_server("https://192.168.5.225:8006")
    add_proxmox_server("https://192.168.5.226:8006")

    body = client.post("/setup/connect/proxmox/skip-vms").text

    assert "192.168.5.225:8006" in body
    assert "192.168.5.226:8006" in body


def test_done_step_offers_add_another(client, config_file, data_dir):
    from app.config_manager import add_proxmox_server

    add_proxmox_server("https://192.168.5.226:8006")

    body = client.post("/setup/connect/proxmox/skip-vms").text

    assert "Add another Proxmox server" in body
    assert "/setup/connect/proxmox/add-another" in body


def test_done_step_lists_a_legacy_singleton_too(client, config_file, data_dir):
    """Before migration the first server lives in the legacy block."""
    _first_server_the_old_way(client)

    body = client.post("/setup/connect/proxmox/skip-vms").text

    assert "192.168.5.226:8006" in body


# ---------------------------------------------------------------------------
# Add another — the migration guard
# ---------------------------------------------------------------------------


def test_add_another_returns_a_blank_credentials_step(client, config_file, data_dir):
    from app.config_manager import add_proxmox_server

    add_proxmox_server("https://192.168.5.226:8006")

    body = client.post("/setup/connect/proxmox/add-another").text

    assert "Proxmox URL" in body
    assert 'value="__new__"' in body


def test_add_another_migrates_a_legacy_singleton_first(client, config_file, data_dir):
    """Without this the first server is dropped the next time config is read.

    A config carrying both a legacy ``proxmox:`` block and a ``proxmox_servers``
    list is resolved by treating the list as authoritative and discarding the
    block — so appending a second server would delete the first.
    """
    _first_server_the_old_way(client)
    cfg = yaml.safe_load(config_file.read_text())
    assert "proxmox" in cfg, "precondition: first save wrote the legacy block"

    client.post("/setup/connect/proxmox/add-another")

    from app.config_manager import get_proxmox_servers

    servers = get_proxmox_servers()
    assert [s["url"] for s in servers] == ["https://192.168.5.226:8006"]
    cfg = yaml.safe_load(config_file.read_text())
    assert "proxmox" not in cfg, "legacy block must be gone, not left alongside"


def test_add_another_moves_the_first_servers_credentials(
    client, config_file, data_dir
):
    """Migration renames proxmox -> proxmox_{id}; the token must survive it."""
    _first_server_the_old_way(client)

    client.post("/setup/connect/proxmox/add-another")

    from app.config_manager import get_proxmox_servers
    from app.credentials import get_integration_credentials

    sid = get_proxmox_servers()[0]["id"]
    assert get_integration_credentials(f"proxmox_{sid}")["secret"] == "secret-a"


def test_add_another_is_idempotent_when_already_migrated(
    client, config_file, data_dir
):
    from app.config_manager import add_proxmox_server, get_proxmox_servers

    add_proxmox_server("https://192.168.5.226:8006")
    client.post("/setup/connect/proxmox/add-another")

    assert len(get_proxmox_servers()) == 1


def test_saving_after_add_another_appends_a_second_server(
    client, config_file, data_dir
):
    """The whole point: two servers configured entirely through the wizard."""
    _first_server_the_old_way(client)
    client.post("/setup/connect/proxmox/add-another")

    client.post(
        "/setup/connect/proxmox/save",
        data={
            "server_id": "__new__",
            "proxmox_url": "https://192.168.5.225:8006",
            "proxmox_token_id": "root@pam!B",
            "proxmox_secret": "secret-b",
        },
    )

    from app.config_manager import get_proxmox_servers
    from app.credentials import get_integration_credentials

    urls = [s["url"] for s in get_proxmox_servers()]
    assert urls == ["https://192.168.5.226:8006", "https://192.168.5.225:8006"]
    assert get_integration_credentials("proxmox_192-168-5-225")["secret"] == "secret-b"


# ---------------------------------------------------------------------------
# Guests discovered on the second server carry its id
# ---------------------------------------------------------------------------


def test_lxcs_saved_for_a_server_are_stamped_with_it(client, config_file, data_dir):
    from app.config_manager import add_proxmox_server

    sid = add_proxmox_server("https://192.168.5.225:8006")

    client.post(
        "/setup/connect/proxmox/save-lxcs",
        data={
            "server_id": sid,
            "selected_lxcs": "pve1:140:paperless:192.168.5.60",
            "proxmox_ssh_user": "root",
            "proxmox_ssh_auth": "key",
            "proxmox_ssh_key": "id_ed25519",
        },
    )

    cfg = yaml.safe_load(config_file.read_text())
    guest = next(h for h in cfg["hosts"] if h["name"] == "paperless")
    assert guest["proxmox_server"] == sid


def test_lxc_ssh_credentials_go_to_the_named_server(client, config_file, data_dir):
    """Each server needs its own SSH credentials — _lxc_ssh_context reads per server."""
    from app.config_manager import add_proxmox_server

    a = add_proxmox_server("https://192.168.5.225:8006")
    b = add_proxmox_server("https://192.168.5.226:8006")

    client.post(
        "/setup/connect/proxmox/save-lxcs",
        data={
            "server_id": b,
            "selected_lxcs": "pve2:141:immich:192.168.5.61",
            "proxmox_ssh_user": "keepup",
            "proxmox_ssh_auth": "key",
            "proxmox_ssh_key": "id_ed25519",
        },
    )

    from app.credentials import get_integration_credentials

    assert get_integration_credentials(f"proxmox_{b}")["ssh_user"] == "keepup"
    assert not get_integration_credentials(f"proxmox_{a}").get("ssh_user")


def test_first_server_lxcs_keep_working_without_a_server_id(
    client, config_file, data_dir
):
    """The first-server flow posts no server_id and must behave as today."""
    _first_server_the_old_way(client)

    client.post(
        "/setup/connect/proxmox/save-lxcs",
        data={
            "selected_lxcs": "pve2:140:paperless:192.168.5.60",
            "proxmox_ssh_user": "root",
            "proxmox_ssh_auth": "key",
            "proxmox_ssh_key": "id_ed25519",
        },
    )

    from app.credentials import get_integration_credentials

    cfg = yaml.safe_load(config_file.read_text())
    guest = next(h for h in cfg["hosts"] if h["name"] == "paperless")
    assert "proxmox_server" not in guest
    assert get_integration_credentials("proxmox")["ssh_user"] == "root"


def test_discover_stamps_the_node_host_with_its_server(
    client, config_file, data_dir
):
    from unittest.mock import AsyncMock, patch

    from app.config_manager import add_proxmox_server
    from app.credentials import save_integration_credentials

    sid = add_proxmox_server("https://192.168.5.225:8006")
    save_integration_credentials(f"proxmox_{sid}", token_id="root@pam!B", secret="s")

    with patch("app.auth_router.ProxmoxClient") as mock_cls:
        mock_cls.return_value.get_nodes = AsyncMock(return_value=["pve1"])
        mock_cls.return_value.discover_resources = AsyncMock(return_value=[])
        client.post("/setup/connect/proxmox/discover", data={"server_id": sid})

    cfg = yaml.safe_load(config_file.read_text())
    node = next(h for h in cfg["hosts"] if h["host"] == "192.168.5.225")
    assert node["proxmox_server"] == sid


def test_discover_uses_the_named_servers_url(client, config_file, data_dir):
    from unittest.mock import AsyncMock, patch

    from app.config_manager import add_proxmox_server
    from app.credentials import save_integration_credentials

    a = add_proxmox_server("https://192.168.5.225:8006")
    b = add_proxmox_server("https://192.168.5.226:8006")
    save_integration_credentials(f"proxmox_{a}", token_id="root@pam!A", secret="sa")
    save_integration_credentials(f"proxmox_{b}", token_id="root@pam!B", secret="sb")

    with patch("app.auth_router.ProxmoxClient") as mock_cls:
        mock_cls.return_value.get_nodes = AsyncMock(return_value=[])
        mock_cls.return_value.discover_resources = AsyncMock(return_value=[])
        client.post("/setup/connect/proxmox/discover", data={"server_id": b})

    assert mock_cls.call_args.kwargs["url"] == "https://192.168.5.226:8006"


# ---------------------------------------------------------------------------
# Section state
# ---------------------------------------------------------------------------


def test_section_carries_the_server_id_through_the_steps(
    client, config_file, data_dir
):
    """Each step re-posts the server it belongs to."""
    from app.config_manager import add_proxmox_server

    sid = add_proxmox_server("https://192.168.5.225:8006")

    body = client.post(
        "/setup/connect/proxmox/skip-lxcs", data={"server_id": sid}
    ).text

    assert f'name="server_id" value="{sid}"' in body


def test_first_server_section_carries_an_empty_server_id(
    client, config_file, data_dir
):
    body = client.post("/setup/connect/proxmox/skip-vms").text
    assert 'name="server_id" value=""' in body


def test_new_sentinel_is_resolved_before_the_next_step(client, config_file, data_dir):
    """The step after the save must carry the derived id, not "__new__".

    Carrying the sentinel forward would make save-lxcs write credentials under
    "proxmox___new__" and stamp guests with a server that does not exist.
    """
    from unittest.mock import AsyncMock, patch

    _first_server_the_old_way(client)
    client.post("/setup/connect/proxmox/add-another")

    with patch("app.auth_router.ProxmoxClient") as mock_cls:
        mock_cls.return_value.get_nodes = AsyncMock(return_value=["pve1"])
        mock_cls.return_value.discover_resources = AsyncMock(
            return_value=[
                {
                    "type": "lxc",
                    "node": "pve1",
                    "vmid": 140,
                    "name": "paperless",
                    "ip": "192.168.5.60",
                    "status": "running",
                }
            ]
        )
        body = client.post(
            "/setup/connect/proxmox/save",
            data={
                "server_id": "__new__",
                "proxmox_url": "https://192.168.5.225:8006",
                "proxmox_token_id": "root@pam!B",
                "proxmox_secret": "secret-b",
            },
        ).text

    assert 'name="server_id" value="192-168-5-225"' in body
    assert "__new__" not in body
