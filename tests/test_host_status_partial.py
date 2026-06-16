"""Render tests for the host_status partial — held-back/phased states (OP#179)."""

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape


def _render(**ctx):
    templates_dir = Path(__file__).parent.parent / "app" / "templates"
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=select_autoescape(["html"]),
    )
    defaults = {
        "slug": "web1",
        "packages": [],
        "reboot_required": False,
        "is_proxmox_node": False,
        "proxmox_node": None,
    }
    defaults.update(ctx)
    return env.get_template("partials/host_status.html").render(**defaults)


def _pkg(name, held_back):
    return {
        "name": name,
        "current": "1.0",
        "available": "2.0",
        "held_back": held_back,
    }


def test_applicable_only_shows_amber_and_upgrade():
    html = _render(packages=[_pkg("curl", False), _pkg("nginx", False)])
    assert "2 updates" in html
    assert "Upgrade" in html
    assert "held back" not in html


def test_mixed_shows_count_and_held_back_subbadge():
    html = _render(packages=[_pkg("curl", False), _pkg("nginx", True)])
    assert "1 update" in html
    assert "Upgrade" in html
    assert "1 held back" in html


def test_held_back_only_shows_neutral_state_no_upgrade():
    html = _render(packages=[_pkg("nginx", True), _pkg("libc6", True)])
    assert "2 held back" in html
    assert "Upgrade" not in html
    assert "full-upgrade" in html


def test_held_back_only_with_reboot_shows_required_badge():
    html = _render(packages=[_pkg("nginx", True)], reboot_required=True)
    assert "held back / phased" in html
    assert "Reboot required" in html
    assert "Upgrade" not in html


def test_up_to_date_when_empty():
    html = _render(packages=[])
    assert "Up to date" in html
    assert "Upgrade" not in html


def test_proxmox_api_packages_without_field_render_amber():
    # Packages from the Proxmox-API path carry no held_back key.
    html = _render(packages=[{"name": "curl", "current": "1", "available": "2"}])
    assert "1 update" in html
    assert "Upgrade" in html
