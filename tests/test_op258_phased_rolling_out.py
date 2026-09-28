"""Tests for OP#258 — phased updates read as "rolling out", not as a pending state.

OP#241 taught Keepup *why* a package is held back, but a host whose only
pending packages were phased still got its own grey "1 held back / phased"
pill in place of "Up to date", and the scheduled check announced them as
"package updates available". A phased update needs nothing from the user: it
installs itself once Ubuntu's rollout reaches the machine. So it must neither
change a host's state nor trigger a notification.
"""

import re
from pathlib import Path
from unittest.mock import patch

import pytest
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


def _visible(html: str) -> str:
    """The text a user reads without hovering — tooltips stripped."""
    return re.sub(r'title="[^"]*"', "", html)


def _pkg(name, reason=None, held_back=True):
    return {
        "name": name,
        "current": "2.90",
        "available": "2.91",
        "held_back": held_back,
        "held_back_reason": reason,
    }


PHASED = _pkg("dnsmasq-base", "phased")
FULL = _pkg("linux-image-amd64", "needs_full_upgrade")
UNKNOWN = _pkg("libc6", None)
REAL = _pkg("curl", None, held_back=False)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_phased_only_host_is_up_to_date():
    html = _render(packages=[PHASED])
    assert "Up to date" in html
    assert "↻ 1 rolling out" in html
    assert "Upgrade" not in html


def test_phased_only_host_does_not_use_apt_jargon():
    visible = _visible(_render(packages=[PHASED]))
    assert "phased" not in visible
    assert "held back" not in visible


def test_phased_package_is_listed_with_versions():
    html = _render(packages=[PHASED])
    assert "dnsmasq-base" in html
    assert "2.90 → 2.91" in html


def test_phased_note_says_it_installs_on_its_own():
    html = _render(packages=[PHASED])
    assert "Ubuntu is releasing this gradually" in html
    assert "install on its own" in html


def test_phased_note_agrees_in_number():
    """The OP#241 copy said "1 of these are phased" — mind the plural."""
    html = _render(packages=[PHASED, _pkg("libfoo", "phased")])
    assert "↻ 2 rolling out" in html
    assert "Ubuntu is releasing these gradually" in html
    assert "they will install on their own" in html


def test_rolling_out_chip_explains_itself_on_hover():
    html = _render(packages=[PHASED])
    tooltip = re.search(r'title="([^"]*rolling out[^"]*|[^"]*gradually[^"]*)"', html)
    assert tooltip, "the rolling-out chip carries no explanation"
    assert "nothing to do" in tooltip.group(1).lower()


def test_rolling_out_chip_is_neutral_not_amber():
    html = _render(packages=[PHASED])
    chip = re.search(r'<span[^>]*>\s*↻ 1 rolling out', html).group(0)
    assert "amber" not in chip


def test_mixed_real_and_phased_shows_updates_plus_rolling_out():
    html = _render(packages=[REAL, PHASED])
    assert "1 update" in html
    assert "↻ 1 rolling out" in html
    assert "Upgrade" in html
    assert "held back" not in _visible(html)


def test_mixed_host_explains_the_phased_package_too():
    """Found in QA: the note appeared only when nothing else was pending."""
    html = _render(packages=[REAL, PHASED])
    assert "Ubuntu is releasing this gradually" in html


def test_mixed_real_phased_and_full_counts_only_the_stuck_one_as_held_back():
    html = _render(packages=[REAL, PHASED, FULL])
    assert "1 held back" in html
    assert "↻ 1 rolling out" in html


def test_full_upgrade_case_is_unchanged():
    html = _render(packages=[FULL])
    assert "1 need full-upgrade" in html
    assert "Waiting will not clear" in html
    assert "rolling out" not in html
    assert "Up to date" not in html


def test_full_upgrade_plus_phased_keeps_the_amber_state():
    html = _render(packages=[FULL, PHASED])
    assert "1 need full-upgrade" in html
    assert "↻ 1 rolling out" in html
    assert "Up to date" not in html


def test_unknown_reason_is_not_claimed_to_be_rolling_out():
    html = _render(packages=[UNKNOWN])
    assert "1 held back" in html
    assert "rolling out" not in html
    assert "install on its own" not in html
    assert "Up to date" not in html


def test_phased_only_with_reboot_still_offers_the_reboot():
    html = _render(packages=[PHASED], reboot_required=True)
    assert "Reboot required" in html
    assert "↻ 1 rolling out" in html


def test_phased_only_on_a_proxmox_node_keeps_the_reboot_preview():
    html = _render(packages=[PHASED], reboot_required=True, is_proxmox_node=True)
    assert "Reboot node" in html
    assert "↻ 1 rolling out" in html


def test_proxmox_badge_survives_on_a_phased_only_host():
    html = _render(packages=[PHASED], proxmox_node="pve")
    assert "Proxmox · pve" in html


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


HOST = {"name": "NGINX", "slug": "nginx"}


@pytest.fixture
def notifier(data_dir, monkeypatch):
    import app.update_notifier as un

    monkeypatch.setattr(un, "_PATH", data_dir / "notified_updates.json")
    return un


def _notify(packages, reboot=False):
    import app.update_scan as us

    sent = []
    with patch(
        "app.update_scan.notify", side_effect=lambda *a, **k: sent.append((a, k))
    ):
        us._notify_host(HOST, {"packages": packages, "reboot_required": reboot})
    return sent


def test_phased_only_host_sends_no_notification(notifier):
    assert _notify([PHASED]) == []


def test_phased_only_host_counts_as_clean_for_dedup(notifier):
    """Real update → announced; it goes phased-only → re-armed; next real one is heard."""
    assert len(_notify([REAL])) == 1
    assert _notify([PHASED]) == []
    assert len(_notify([REAL])) == 1


def test_phased_to_real_update_notifies(notifier):
    _notify([PHASED])
    sent = _notify([REAL, PHASED])
    assert len(sent) == 1


def test_notification_count_excludes_phased_packages(notifier):
    (args, _), = _notify([REAL, PHASED, _pkg("libfoo", "phased")])
    title, message = args[0], args[1]
    assert "OS updates available: NGINX" == title
    assert message.startswith("1 package update available on NGINX.")


def test_phased_only_with_reboot_sends_the_reboot_notification(notifier):
    (args, _), = _notify([PHASED], reboot=True)
    assert args[0] == "Reboot required: NGINX"


def test_full_upgrade_packages_still_notify(notifier):
    """They need the user to act, so they are news — unlike phased ones."""
    (args, _), = _notify([FULL])
    assert args[0] == "OS updates available: NGINX"
    assert args[1].startswith("1 package update available")


def test_unknown_reason_held_back_packages_still_notify(notifier):
    assert len(_notify([UNKNOWN])) == 1
