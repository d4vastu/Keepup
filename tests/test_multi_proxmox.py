"""Tests for multi-server Proxmox support (OP#209).

Covers the data model (``proxmox_servers``), the derived server id, the
one-shot migration off the legacy singleton ``proxmox:`` block, and the
compatibility shims that keep the singleton readers working until OP#210
makes every call site server-aware.
"""

import yaml


# ---------------------------------------------------------------------------
# Server id derivation
# ---------------------------------------------------------------------------


def test_server_id_maps_dots_to_hyphens():
    """The design calls for 192-168-1-10, not the digit soup bare slugify gives.

    ``slugify`` deletes every non-[a-z0-9-] char, so it would turn
    192.168.1.10 into 1921685226-style nonsense. The server id needs its own
    derivation.
    """
    from app.config_manager import slugify_server_id

    assert slugify_server_id("https://192.168.1.10:8006") == "192-168-1-10"


def test_server_id_uses_hostname_not_scheme_or_port():
    from app.config_manager import slugify_server_id

    assert slugify_server_id("https://pve1.lan:8006") == "pve1-lan"


def test_server_id_is_css_safe_with_special_chars():
    """Config-derived ids become HTML ids / CSS selectors — must be [a-z0-9-]."""
    import re

    from app.config_manager import slugify_server_id

    sid = slugify_server_id("https://Pve_Node(1).Home_Lab:8006")
    assert re.fullmatch(r"[a-z0-9-]+", sid), sid
    assert "(" not in sid and ")" not in sid and "_" not in sid


def test_server_id_falls_back_when_host_unusable():
    """A URL that slugifies to nothing still yields a usable id."""
    from app.config_manager import slugify_server_id

    assert slugify_server_id("https://___:8006") == "proxmox"


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def test_add_and_get_proxmox_servers(config_file):
    from app.config_manager import (
        add_proxmox_server,
        get_proxmox_server,
        get_proxmox_servers,
    )

    sid = add_proxmox_server("https://192.168.5.225:8006", verify_ssl=False)

    assert sid == "192-168-5-225"
    assert [s["id"] for s in get_proxmox_servers()] == [sid]
    assert get_proxmox_server(sid)["url"] == "https://192.168.5.225:8006"
    assert get_proxmox_server(sid)["verify_ssl"] is False


def test_get_proxmox_server_unknown_id_returns_empty(config_file):
    from app.config_manager import get_proxmox_server

    assert get_proxmox_server("nope") == {}


def test_add_proxmox_server_strips_trailing_slash(config_file):
    from app.config_manager import add_proxmox_server, get_proxmox_server

    sid = add_proxmox_server("https://192.168.5.225:8006/")
    assert get_proxmox_server(sid)["url"] == "https://192.168.5.225:8006"


def test_colliding_server_ids_get_numeric_suffix(config_file):
    """Two URLs deriving the same id must not overwrite one another."""
    from app.config_manager import add_proxmox_server, get_proxmox_servers

    first = add_proxmox_server("https://pve.lan:8006")
    second = add_proxmox_server("https://pve.lan:8007")
    third = add_proxmox_server("https://pve.lan:8008")

    assert first == "pve-lan"
    assert second == "pve-lan-2"
    assert third == "pve-lan-3"
    assert len(get_proxmox_servers()) == 3


def test_update_proxmox_server(config_file):
    from app.config_manager import (
        add_proxmox_server,
        get_proxmox_server,
        update_proxmox_server,
    )

    sid = add_proxmox_server("https://pve.lan:8006")
    update_proxmox_server(sid, url="https://pve.lan:9006", verify_ssl=False)

    server = get_proxmox_server(sid)
    assert server["url"] == "https://pve.lan:9006"
    assert server["verify_ssl"] is False
    # The id is stable across a URL change — hosts reference it.
    assert server["id"] == sid


def test_update_proxmox_server_keeps_pinned_cert_when_omitted(config_file):
    from app.config_manager import (
        add_proxmox_server,
        get_proxmox_server,
        update_proxmox_server,
    )

    sid = add_proxmox_server(
        "https://pve.lan:8006", pinned_cert_pem="PEM", pinned_fingerprint="FP"
    )
    update_proxmox_server(sid, verify_ssl=False)

    assert get_proxmox_server(sid)["pinned_cert_pem"] == "PEM"
    assert get_proxmox_server(sid)["pinned_fingerprint"] == "FP"


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------


def _write_hosts(config_file, hosts, servers):
    config_file.write_text(
        yaml.dump({"hosts": hosts, "proxmox_servers": servers})
    )


def test_delete_proxmox_server_removes_node_and_lxc_keeps_vm(config_file, data_dir):
    """A VM has its own kernel and SSH identity, so it survives as a plain host.

    A node and an LXC are unusable without the API, so they go.
    """
    from app.config_manager import delete_proxmox_server, get_hosts

    _write_hosts(
        config_file,
        [
            {"name": "Node", "host": "10.0.0.1", "proxmox_node": "pve",
             "proxmox_server": "s1"},
            {"name": "Ct", "host": "10.0.0.2", "proxmox_node": "pve",
             "proxmox_vmid": 101, "proxmox_type": "lxc", "proxmox_server": "s1"},
            {"name": "Vm", "host": "10.0.0.3", "user": "root",
             "proxmox_node": "pve", "proxmox_vmid": 200, "proxmox_type": "vm",
             "proxmox_server": "s1"},
            {"name": "Other", "host": "10.0.0.4", "proxmox_node": "pve2",
             "proxmox_server": "s2"},
            {"name": "Plain", "host": "10.0.0.5", "user": "root"},
        ],
        [{"id": "s1", "url": "https://a:8006"}, {"id": "s2", "url": "https://b:8006"}],
    )

    delete_proxmox_server("s1")

    by_name = {h["name"]: h for h in get_hosts()}
    assert "Node" not in by_name
    assert "Ct" not in by_name
    # The VM stays, stripped of every Proxmox field.
    vm = by_name["Vm"]
    assert vm["host"] == "10.0.0.3" and vm["user"] == "root"
    assert not any(k.startswith("proxmox_") for k in vm)
    # Other servers and plain hosts are untouched.
    assert by_name["Other"]["proxmox_server"] == "s2"
    assert by_name["Plain"]["host"] == "10.0.0.5"


def test_delete_proxmox_server_removes_server_entry(config_file, data_dir):
    from app.config_manager import delete_proxmox_server, get_proxmox_servers

    _write_hosts(
        config_file, [],
        [{"id": "s1", "url": "https://a:8006"}, {"id": "s2", "url": "https://b:8006"}],
    )
    delete_proxmox_server("s1")

    assert [s["id"] for s in get_proxmox_servers()] == ["s2"]


def test_delete_proxmox_server_purges_credentials(config_file, data_dir):
    from app.config_manager import delete_proxmox_server
    from app.credentials import (
        get_integration_credentials,
        save_integration_credentials,
    )

    _write_hosts(config_file, [], [{"id": "s1", "url": "https://a:8006"}])
    save_integration_credentials("proxmox_s1", token_id="t", secret="s")
    assert get_integration_credentials("proxmox_s1")["token_id"] == "t"

    delete_proxmox_server("s1")

    assert get_integration_credentials("proxmox_s1") == {}


def test_delete_unknown_proxmox_server_is_noop(config_file, data_dir):
    from app.config_manager import delete_proxmox_server, get_proxmox_servers

    _write_hosts(config_file, [], [{"id": "s1", "url": "https://a:8006"}])
    delete_proxmox_server("nope")

    assert [s["id"] for s in get_proxmox_servers()] == ["s1"]


# ---------------------------------------------------------------------------
# One-shot migration
# ---------------------------------------------------------------------------


def _write_legacy(config_file):
    config_file.write_text(
        yaml.dump(
            {
                "proxmox": {
                    "url": "https://192.168.5.226:8006",
                    "verify_ssl": False,
                    "pinned_cert_pem": "PEM",
                    "pinned_fingerprint": "FP",
                },
                "hosts": [
                    {"name": "Proxmox VE (pve)", "host": "192.168.5.226",
                     "proxmox_node": "pve"},
                    {"name": "Ct", "host": "192.168.5.235", "proxmox_node": "pve",
                     "proxmox_vmid": 100},
                    {"name": "Plain", "host": "192.168.5.228", "user": "root"},
                ],
            }
        )
    )


def test_migration_moves_config_block(config_file, data_dir):
    from app.config_manager import (
        get_proxmox_servers,
        load_config,
        migrate_proxmox_servers,
    )

    _write_legacy(config_file)
    sid = migrate_proxmox_servers()

    assert sid == "192-168-5-226"
    servers = get_proxmox_servers()
    assert len(servers) == 1
    assert servers[0] == {
        "id": "192-168-5-226",
        "url": "https://192.168.5.226:8006",
        "verify_ssl": False,
        "pinned_cert_pem": "PEM",
        "pinned_fingerprint": "FP",
    }
    # Legacy key gone — this is what makes the migration idempotent.
    assert "proxmox" not in load_config()


def test_migration_stamps_proxmox_hosts_only(config_file, data_dir):
    from app.config_manager import get_hosts, migrate_proxmox_servers

    _write_legacy(config_file)
    migrate_proxmox_servers()

    by_name = {h["name"]: h for h in get_hosts()}
    assert by_name["Proxmox VE (pve)"]["proxmox_server"] == "192-168-5-226"
    assert by_name["Ct"]["proxmox_server"] == "192-168-5-226"
    # A plain host is not a Proxmox guest and must not be stamped.
    assert "proxmox_server" not in by_name["Plain"]


def test_migration_renames_credential_entry(config_file, data_dir):
    from app.config_manager import migrate_proxmox_servers
    from app.credentials import (
        get_integration_credentials,
        save_integration_credentials,
    )

    _write_legacy(config_file)
    save_integration_credentials(
        "proxmox", token_id="root@pam!keepup", secret="sekrit", ssh_user="root"
    )

    sid = migrate_proxmox_servers()

    moved = get_integration_credentials(f"proxmox_{sid}")
    assert moved["token_id"] == "root@pam!keepup"
    assert moved["secret"] == "sekrit"
    assert moved["ssh_user"] == "root"


def test_migration_is_idempotent(config_file, data_dir):
    """Re-running must not create a second server or re-stamp anything."""
    from app.config_manager import (
        get_hosts,
        get_proxmox_servers,
        migrate_proxmox_servers,
    )

    _write_legacy(config_file)
    first = migrate_proxmox_servers()
    before = get_hosts()

    second = migrate_proxmox_servers()

    assert first == "192-168-5-226"
    assert second == ""
    assert len(get_proxmox_servers()) == 1
    assert get_hosts() == before


def test_migration_noop_without_legacy_block(config_file, data_dir):
    from app.config_manager import get_proxmox_servers, migrate_proxmox_servers

    assert migrate_proxmox_servers() == ""
    assert get_proxmox_servers() == []


def test_migration_drops_legacy_block_when_servers_already_exist(
    config_file, data_dir
):
    """Hand-edited config with both shapes: the new list wins, legacy is dropped."""
    from app.config_manager import (
        get_proxmox_servers,
        load_config,
        migrate_proxmox_servers,
    )

    config_file.write_text(
        yaml.dump(
            {
                "proxmox": {"url": "https://legacy:8006"},
                "proxmox_servers": [{"id": "kept", "url": "https://kept:8006"}],
                "hosts": [],
            }
        )
    )

    assert migrate_proxmox_servers() == ""
    assert [s["id"] for s in get_proxmox_servers()] == ["kept"]
    assert "proxmox" not in load_config()


def test_migration_without_pinned_cert_omits_the_fields(config_file, data_dir):
    from app.config_manager import get_proxmox_servers, migrate_proxmox_servers

    config_file.write_text(
        yaml.dump({"proxmox": {"url": "https://pve.lan:8006"}, "hosts": []})
    )
    migrate_proxmox_servers()

    server = get_proxmox_servers()[0]
    assert "pinned_cert_pem" not in server
    assert server["verify_ssl"] is True


# ---------------------------------------------------------------------------
# Compatibility shims (removed in OP#210)
# ---------------------------------------------------------------------------


def test_get_proxmox_config_shim_reads_first_server(config_file):
    """Singleton readers keep working after migration, until OP#210."""
    from app.config_manager import get_proxmox_config

    _write_hosts(
        config_file, [],
        [
            {"id": "s1", "url": "https://first:8006", "verify_ssl": False},
            {"id": "s2", "url": "https://second:8006"},
        ],
    )

    cfg = get_proxmox_config()
    assert cfg["url"] == "https://first:8006"
    assert cfg["verify_ssl"] is False
    # The shim mimics the old block shape, which had no id.
    assert "id" not in cfg


def test_get_proxmox_config_prefers_legacy_block_before_migration(config_file):
    from app.config_manager import get_proxmox_config

    config_file.write_text(
        yaml.dump(
            {
                "proxmox": {"url": "https://legacy:8006"},
                "proxmox_servers": [{"id": "s1", "url": "https://new:8006"}],
                "hosts": [],
            }
        )
    )
    assert get_proxmox_config()["url"] == "https://legacy:8006"


def test_get_proxmox_config_empty_when_nothing_configured(config_file):
    from app.config_manager import get_proxmox_config

    assert get_proxmox_config() == {}


def test_save_proxmox_config_writes_through_to_first_server(config_file, data_dir):
    """Post-migration there is no legacy block to write, so update server[0]."""
    from app.config_manager import (
        get_proxmox_server,
        get_proxmox_servers,
        load_config,
        save_proxmox_config,
    )

    _write_hosts(
        config_file, [], [{"id": "s1", "url": "https://old:8006", "verify_ssl": True}]
    )
    save_proxmox_config("https://new:8006", verify_ssl=False)

    assert len(get_proxmox_servers()) == 1
    assert get_proxmox_server("s1")["url"] == "https://new:8006"
    assert get_proxmox_server("s1")["verify_ssl"] is False
    # It must not resurrect the legacy block the migration just removed.
    assert "proxmox" not in load_config()


def test_save_proxmox_config_adds_first_server_when_list_empty(config_file, data_dir):
    from app.config_manager import get_proxmox_servers, save_proxmox_config

    _write_hosts(config_file, [], [])
    save_proxmox_config("https://pve.lan:8006")

    servers = get_proxmox_servers()
    assert [s["id"] for s in servers] == ["pve-lan"]


def test_save_proxmox_config_empty_url_removes_the_server(config_file, data_dir):
    """Clearing the integration must not leave a half-configured server behind."""
    from app.config_manager import get_proxmox_servers, save_proxmox_config

    _write_hosts(
        config_file,
        [{"name": "Node", "host": "10.0.0.1", "proxmox_node": "pve",
          "proxmox_server": "s1"}],
        [{"id": "s1", "url": "https://a:8006"}],
    )
    save_proxmox_config("")

    assert get_proxmox_servers() == []


def test_save_proxmox_config_keeps_legacy_shape_before_migration(config_file):
    """Pre-migration behaviour is unchanged — existing call sites still work."""
    from app.config_manager import get_proxmox_config, load_config, save_proxmox_config

    save_proxmox_config("https://pve.lan:8006", verify_ssl=False)

    assert load_config()["proxmox"]["url"] == "https://pve.lan:8006"
    assert get_proxmox_config()["verify_ssl"] is False
    assert "proxmox_servers" not in load_config()


def test_integration_creds_shim_resolves_first_server(config_file, data_dir):
    """`get_integration_credentials("proxmox")` must survive the key rename."""
    from app.credentials import (
        get_integration_credentials,
        save_integration_credentials,
    )

    _write_hosts(config_file, [], [{"id": "s1", "url": "https://a:8006"}])
    save_integration_credentials("proxmox_s1", token_id="t", ssh_user="root")

    creds = get_integration_credentials("proxmox")
    assert creds["token_id"] == "t"
    assert creds["ssh_user"] == "root"


def test_integration_creds_shim_prefers_legacy_entry(config_file, data_dir):
    from app.credentials import (
        get_integration_credentials,
        save_integration_credentials,
    )

    _write_hosts(config_file, [], [{"id": "s1", "url": "https://a:8006"}])
    save_integration_credentials("proxmox", token_id="legacy")
    save_integration_credentials("proxmox_s1", token_id="new")

    assert get_integration_credentials("proxmox")["token_id"] == "legacy"


def test_integration_creds_shim_absent_returns_empty(config_file, data_dir):
    from app.credentials import get_integration_credentials

    assert get_integration_credentials("proxmox") == {}


def test_integration_creds_shim_does_not_affect_other_keys(config_file, data_dir):
    """The fallback is Proxmox-specific — Portainer must not gain one."""
    from app.credentials import get_integration_credentials

    _write_hosts(config_file, [], [{"id": "s1", "url": "https://a:8006"}])
    assert get_integration_credentials("portainer") == {}
