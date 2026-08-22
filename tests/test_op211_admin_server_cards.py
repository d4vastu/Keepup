"""Tests for the multi-server Proxmox admin UI (OP#211).

Covers the per-server card list in Admin → Integrations, the CSS-safe per-card
HTML ids, and making the shared ``/setup/connect/proxmox/*`` routes and the
admin discover/add-node routes operate on the named server rather than
whichever one happens to be first.

The id tests matter more than they look: every id in the pre-OP#211 card was
global and singular, and ``hx-include`` selects them by ``#id``. With two cards
htmx silently submits the *first* card's values — a wrong-server write with no
error anywhere.
"""

import re

import yaml


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _two_servers(config_file, data_dir):
    """Configure two Proxmox servers with distinct credentials."""
    from app.config_manager import add_proxmox_server
    from app.credentials import save_integration_credentials

    a = add_proxmox_server("https://192.168.5.225:8006")
    b = add_proxmox_server("https://192.168.5.226:8006")
    save_integration_credentials(
        f"proxmox_{a}", token_id="root@pam!A", secret="secret-a", api_user="root@pam"
    )
    save_integration_credentials(
        f"proxmox_{b}", token_id="root@pam!B", secret="secret-b", api_user="root@pam"
    )
    return a, b


# ---------------------------------------------------------------------------
# add_host() must be able to stamp the owning server
# ---------------------------------------------------------------------------


def test_add_host_persists_proxmox_server(config_file):
    """Without this, admin-discovered guests fall back to the first server.

    ``server_context()`` treats a host with no ``proxmox_server`` as belonging
    to the singleton shim's server, so a guest discovered on server B would be
    checked and upgraded via server A.
    """
    from app.config_manager import add_host

    add_host(
        name="Guest B",
        host="192.168.5.60",
        user=None,
        port=None,
        proxmox_node="pve2",
        proxmox_vmid=140,
        proxmox_type="lxc",
        proxmox_server="192-168-5-226",
    )

    cfg = yaml.safe_load(config_file.read_text())
    entry = next(h for h in cfg["hosts"] if h["name"] == "Guest B")
    assert entry["proxmox_server"] == "192-168-5-226"


def test_add_host_omits_proxmox_server_when_not_given(config_file):
    """A plain host must not grow an empty proxmox_server key."""
    from app.config_manager import add_host

    add_host(name="Plain", host="192.168.1.99", user="root", port=None)

    cfg = yaml.safe_load(config_file.read_text())
    entry = next(h for h in cfg["hosts"] if h["name"] == "Plain")
    assert "proxmox_server" not in entry


# ---------------------------------------------------------------------------
# Admin context — per-server cards
# ---------------------------------------------------------------------------


def test_integration_status_lists_every_server(config_file, data_dir):
    from app.admin import _integration_status

    a, b = _two_servers(config_file, data_dir)
    cards = _integration_status()["proxmox_servers"]

    assert [c["id"] for c in cards] == [a, b]
    assert cards[0]["url"] == "https://192.168.5.225:8006"
    assert cards[1]["url"] == "https://192.168.5.226:8006"


def test_integration_status_reads_per_server_credentials(config_file, data_dir):
    """Card B must show B's token, not A's — the shim would return A's."""
    from app.admin import _integration_status

    _two_servers(config_file, data_dir)
    cards = _integration_status()["proxmox_servers"]

    assert cards[0]["token_id"] == "root@pam!A"
    assert cards[1]["token_id"] == "root@pam!B"
    assert all(c["secret_set"] for c in cards)


def test_integration_status_empty_without_servers(config_file, data_dir):
    from app.admin import _integration_status

    assert _integration_status()["proxmox_servers"] == []


def test_card_ssh_state_reflects_stored_auth(config_file, data_dir):
    """API and SSH status are independent — a valid token with no SSH is real.

    That is pve1's actual state: reachable API, no SSH credentials at all.
    """
    from app.admin import _integration_status
    from app.config_manager import add_proxmox_server
    from app.credentials import save_integration_credentials

    sid = add_proxmox_server("https://192.168.5.225:8006")
    save_integration_credentials(f"proxmox_{sid}", token_id="root@pam!A", secret="s")
    assert _integration_status()["proxmox_servers"][0]["ssh_state"] == "none"

    save_integration_credentials(f"proxmox_{sid}", ssh_user="root", ssh_key="id_ed25519")
    assert _integration_status()["proxmox_servers"][0]["ssh_state"] == "key"

    save_integration_credentials(
        f"proxmox_{sid}", ssh_password="hunter2", ssh_key=None
    )
    assert _integration_status()["proxmox_servers"][0]["ssh_state"] == "password"


# ---------------------------------------------------------------------------
# Rendering — per-card ids
# ---------------------------------------------------------------------------


def test_admin_page_renders_a_card_per_server(client, config_file, data_dir):
    _two_servers(config_file, data_dir)
    body = client.get("/admin/integrations").text

    assert "192.168.5.225:8006" in body
    assert "192.168.5.226:8006" in body


def test_per_card_ids_are_namespaced_by_server(client, config_file, data_dir):
    """The core defect this story fixes: two cards must not share ids."""
    a, b = _two_servers(config_file, data_dir)
    body = client.get("/admin/integrations").text

    for sid in (a, b):
        assert f'id="px-{sid}-url"' in body
        assert f'id="px-{sid}-secret"' in body
        assert f'id="px-{sid}-result"' in body

    # The old global ids must be gone, or hx-include picks the wrong card.
    assert 'id="proxmox-url"' not in body
    assert 'id="proxmox-secret"' not in body


def test_hx_include_targets_the_cards_own_fields(client, config_file, data_dir):
    a, b = _two_servers(config_file, data_dir)
    body = client.get("/admin/integrations").text

    assert f"#px-{a}-url" in body
    assert f"#px-{b}-url" in body


def test_card_carries_its_server_id_in_the_form(client, config_file, data_dir):
    a, b = _two_servers(config_file, data_dir)
    body = client.get("/admin/integrations").text

    assert f'name="server_id" value="{a}"' in body
    assert f'name="server_id" value="{b}"' in body


def test_card_ids_are_css_safe_for_a_special_char_host(
    client, config_file, data_dir
):
    """Required by CLAUDE.md: config-derived HTML ids must be [a-z0-9-] only.

    A host with parentheses and underscores must not leak them into an id,
    where they would break the CSS/htmx selector.
    """
    from app.config_manager import add_proxmox_server

    sid = add_proxmox_server("https://Pve_Node(1).Home_Lab:8006")
    assert re.fullmatch(r"[a-z0-9-]+", sid), sid

    body = client.get("/admin/integrations").text
    assert f'id="px-{sid}-url"' in body
    assert "(" not in f"px-{sid}-url"

    for rendered in re.findall(r'id="(px-[^"]+)"', body):
        assert re.fullmatch(r"[a-z0-9-]+", rendered), rendered


def test_ssh_toggle_is_scoped_per_card(client, config_file, data_dir):
    """One global togglePxSshAuth() would always toggle card #1's rows."""
    a, b = _two_servers(config_file, data_dir)
    body = client.get("/admin/integrations").text

    assert f"togglePxSshAuth(this.value, '{a}')" in body
    assert f"togglePxSshAuth(this.value, '{b}')" in body


# ---------------------------------------------------------------------------
# Single-server and first-run views stay as they are today
# ---------------------------------------------------------------------------


def test_single_server_view_has_no_remove_action(client, config_file, data_dir):
    from app.config_manager import add_proxmox_server
    from app.credentials import save_integration_credentials

    sid = add_proxmox_server("https://192.168.5.226:8006")
    save_integration_credentials(f"proxmox_{sid}", token_id="root@pam!A", secret="s")

    body = client.get("/admin/integrations").text
    assert "remove-server" not in body
    assert "Add Proxmox server" in body


def test_two_servers_offer_remove(client, config_file, data_dir):
    _two_servers(config_file, data_dir)
    body = client.get("/admin/integrations").text
    assert "remove-server" in body


def test_first_run_renders_an_empty_card(client, config_file, data_dir):
    """Zero servers shows today's empty form, not a bare Add button."""
    body = client.get("/admin/integrations").text
    assert 'id="px-new-url"' in body
    assert "PROXMOX URL" in body


# ---------------------------------------------------------------------------
# Routes operate on the named server
# ---------------------------------------------------------------------------


def test_discover_uses_the_named_servers_url(client, config_file, data_dir):
    from unittest.mock import AsyncMock, patch

    a, b = _two_servers(config_file, data_dir)

    with patch("app.admin.ProxmoxClient") as mock_cls:
        mock_cls.return_value.discover_resources = AsyncMock(return_value=[])
        client.post("/admin/integrations/proxmox/discover", data={"server_id": b})

    assert mock_cls.call_args.kwargs["url"] == "https://192.168.5.226:8006"


def test_discover_unknown_server_reports_rather_than_raising(
    client, config_file, data_dir
):
    _two_servers(config_file, data_dir)
    resp = client.post(
        "/admin/integrations/proxmox/discover", data={"server_id": "nope"}
    )
    assert resp.status_code == 200
    assert "not configured" in resp.text.lower()


def test_add_node_host_stamps_the_owning_server(client, config_file, data_dir):
    from unittest.mock import AsyncMock, patch

    a, b = _two_servers(config_file, data_dir)

    with patch("app.admin.ProxmoxClient") as mock_cls:
        mock_cls.return_value.get_nodes = AsyncMock(return_value=["pve2"])
        client.post(
            "/admin/integrations/proxmox/add-node-host", data={"server_id": b}
        )

    cfg = yaml.safe_load(config_file.read_text())
    node = next(h for h in cfg["hosts"] if h["host"] == "192.168.5.226")
    assert node["proxmox_server"] == b
    assert node["proxmox_node"] == "pve2"


def test_select_hosts_stamps_the_owning_server(client, config_file, data_dir):
    a, b = _two_servers(config_file, data_dir)

    client.post(
        "/admin/integrations/proxmox/select-hosts",
        data={"server_id": b, "selected_hosts": "pve2:140:lxc:paperless:192.168.5.60"},
    )

    cfg = yaml.safe_load(config_file.read_text())
    guest = next(h for h in cfg["hosts"] if h["name"] == "paperless")
    assert guest["proxmox_server"] == b


# ---------------------------------------------------------------------------
# Save / add / remove
# ---------------------------------------------------------------------------


def test_save_updates_the_named_server_not_the_first(client, config_file, data_dir):
    a, b = _two_servers(config_file, data_dir)

    client.post(
        "/setup/connect/proxmox/save",
        data={
            "server_id": b,
            "proxmox_url": "https://192.168.5.240:8006",
            "proxmox_token_id": "root@pam!B2",
            "proxmox_secret": "new-secret",
        },
    )

    from app.config_manager import get_proxmox_server

    assert get_proxmox_server(a)["url"] == "https://192.168.5.225:8006"
    assert get_proxmox_server(b)["url"] == "https://192.168.5.240:8006"


def test_save_writes_credentials_under_the_named_server(
    client, config_file, data_dir
):
    a, b = _two_servers(config_file, data_dir)

    client.post(
        "/setup/connect/proxmox/save",
        data={
            "server_id": b,
            "proxmox_url": "https://192.168.5.226:8006",
            "proxmox_token_id": "root@pam!B2",
            "proxmox_secret": "rotated",
        },
    )

    from app.credentials import get_integration_credentials

    assert get_integration_credentials(f"proxmox_{b}")["secret"] == "rotated"
    assert get_integration_credentials(f"proxmox_{a}")["secret"] == "secret-a"


def test_save_with_new_sentinel_appends_a_server(client, config_file, data_dir):
    a, b = _two_servers(config_file, data_dir)

    client.post(
        "/setup/connect/proxmox/save",
        data={
            "server_id": "__new__",
            "proxmox_url": "https://192.168.5.250:8006",
            "proxmox_token_id": "root@pam!C",
            "proxmox_secret": "secret-c",
        },
    )

    from app.config_manager import get_proxmox_servers

    ids = [s["id"] for s in get_proxmox_servers()]
    assert ids == [a, b, "192-168-5-250"]


def test_save_without_server_id_keeps_singleton_behaviour(
    client, config_file, data_dir
):
    """The wizard's first-server flow posts no server_id and must still work.

    On a config that has never been migrated this writes the legacy ``proxmox:``
    block and stores credentials under the bare ``proxmox`` key — exactly what
    it did before OP#211. The startup migration converts it to a server entry.
    """
    client.post(
        "/setup/connect/proxmox/save",
        data={
            "proxmox_url": "https://192.168.5.226:8006",
            "proxmox_token_id": "root@pam!A",
            "proxmox_secret": "s",
        },
    )

    from app.config_manager import get_proxmox_config
    from app.credentials import get_integration_credentials

    assert get_proxmox_config()["url"] == "https://192.168.5.226:8006"
    assert get_integration_credentials("proxmox")["secret"] == "s"


def test_add_server_route_returns_a_blank_card(client, config_file, data_dir):
    _two_servers(config_file, data_dir)
    resp = client.post("/admin/integrations/proxmox/add-server")

    assert resp.status_code == 200
    assert 'id="px-new-url"' in resp.text
    assert 'value="__new__"' in resp.text


def test_remove_server_deletes_it(client, config_file, data_dir):
    a, b = _two_servers(config_file, data_dir)

    client.post(
        "/admin/integrations/proxmox/remove-server", data={"server_id": b}
    )

    from app.config_manager import get_proxmox_servers

    assert [s["id"] for s in get_proxmox_servers()] == [a]


def test_remove_server_names_the_url_and_hosts_it_will_drop(
    client, config_file, data_dir
):
    """Two IPs differing by one character — the confirmation must be specific."""
    from app.config_manager import add_host

    a, b = _two_servers(config_file, data_dir)
    add_host(
        name="paperless",
        host="192.168.5.60",
        user=None,
        port=None,
        proxmox_node="pve2",
        proxmox_vmid=140,
        proxmox_type="lxc",
        proxmox_server=b,
    )

    resp = client.post(
        "/admin/integrations/proxmox/remove-server-confirm", data={"server_id": b}
    )

    assert "192.168.5.226:8006" in resp.text
    assert "paperless" in resp.text


def test_new_is_reserved_so_a_blank_card_cannot_collide(config_file):
    """The blank card uses px-new-* ids; a real server must not claim 'new'."""
    from app.config_manager import add_proxmox_server

    assert add_proxmox_server("https://new:8006") != "new"


# ---------------------------------------------------------------------------
# SSL cert-trust retry must re-submit the card it came from
# ---------------------------------------------------------------------------


def test_cert_trust_retry_targets_the_calling_card(client, config_file, data_dir):
    """The trust prompt re-posts the test — from the right card's fields.

    Hardcoded global ids would make the retry submit nothing on a namespaced
    card, so the second attempt fails with no visible reason.
    """
    from unittest.mock import patch

    a, b = _two_servers(config_file, data_dir)

    with patch("app.auth_router.ProxmoxClient") as mock_cls, patch(
        "app.auth_router._is_ssl_cert_error", return_value=True
    ), patch(
        "app.auth_router.fetch_server_cert", return_value="PEM"
    ), patch(
        "app.auth_router.cert_info", return_value={"subject": "pve", "issuer": "self"}
    ):
        mock_cls.return_value.get_version.side_effect = RuntimeError("bad cert")
        resp = client.post(
            "/setup/connect/proxmox/test",
            data={
                "server_id": b,
                "proxmox_url": "https://192.168.5.226:8006",
                "proxmox_token_id": "root@pam!B",
                "proxmox_secret": "secret-b",
            },
        )

    assert f"#px-{b}-test-result" in resp.text
    assert f"#px-{b}-secret" in resp.text
    assert "#proxmox-secret" not in resp.text


def test_cert_trust_retry_uses_name_selectors_for_the_wizard(
    client, config_file, data_dir
):
    """The wizard's inputs carry no ids, so its retry must select by name."""
    import html
    from unittest.mock import patch

    with patch("app.auth_router.ProxmoxClient") as mock_cls, patch(
        "app.auth_router._is_ssl_cert_error", return_value=True
    ), patch(
        "app.auth_router.fetch_server_cert", return_value="PEM"
    ), patch(
        "app.auth_router.cert_info", return_value={"subject": "pve", "issuer": "self"}
    ):
        mock_cls.return_value.get_version.side_effect = RuntimeError("bad cert")
        resp = client.post(
            "/setup/connect/proxmox/test",
            data={
                "proxmox_url": "https://192.168.5.226:8006",
                "proxmox_token_id": "root@pam!A",
                "proxmox_secret": "s",
            },
        )

    # Jinja escapes the quotes; the browser decodes them back into a selector.
    assert "[name='proxmox_url']" in html.unescape(resp.text)
    assert "#proxmox-test-result" in resp.text
