"""Tests for OP#241 — distinguish phased from needs-full-upgrade hold-backs.

`held_back` was a bare bool, so the UI told users that *every* held-back
package "rolls out automatically over time". That is true of a phased update
and false of one kept back because it pulls in a new package: `apt-get upgrade`
never installs new packages, so such a package is deferred forever.

`apt-get -s full-upgrade` is the discriminator — it plans the install a plain
upgrade refuses, while a phased package is skipped by both.
"""

import subprocess

from app.package_managers import AptPackageManager

_PM = AptPackageManager()


def _stdout(listing: str, apply_sim: str, full_sim: str, reboot: str = "no") -> str:
    return (
        f"{listing}\n__APPLY__\n{apply_sim}\n__FULL__\n{full_sim}\n__REBOOT__\n{reboot}"
    )


_LISTING = (
    "Listing...\n"
    "curl/stable 8.14.1-2 amd64 [upgradable from: 8.14.1-1]\n"
    "linux-image-amd64/stable-security 6.12.107-1 amd64 [upgradable from: 6.12.94-1]\n"
    "libfoo/stable 2.0 amd64 [upgradable from: 1.9]\n"
)

# plain upgrade applies only curl
_APPLY = "Inst curl [8.14.1-1] (8.14.1-2 Debian:13/stable [amd64])"

# full-upgrade additionally plans the kernel (note the new ABI package)
_FULL = (
    "Inst curl [8.14.1-1] (8.14.1-2 Debian:13/stable [amd64])\n"
    "Inst linux-image-6.12.107+deb13-amd64 (6.12.107-1 Debian-Security:13 [amd64])\n"
    "Inst linux-image-amd64 [6.12.94-1] (6.12.107-1 Debian-Security:13 [amd64])"
)


def _by_name(packages):
    return {p["name"]: p for p in packages}


# ---------------------------------------------------------------------------
# The discrimination itself
# ---------------------------------------------------------------------------


def test_package_needing_full_upgrade_is_marked_as_such():
    packages, _ = _PM.parse(_stdout(_LISTING, _APPLY, _FULL))
    kernel = _by_name(packages)["linux-image-amd64"]
    assert kernel["held_back"] is True
    assert kernel["held_back_reason"] == "needs_full_upgrade"


def test_package_skipped_by_both_simulations_is_phased():
    """libfoo appears in neither simulation — that is what phasing looks like."""
    packages, _ = _PM.parse(_stdout(_LISTING, _APPLY, _FULL))
    libfoo = _by_name(packages)["libfoo"]
    assert libfoo["held_back"] is True
    assert libfoo["held_back_reason"] == "phased"


def test_applicable_package_has_no_hold_back_reason():
    packages, _ = _PM.parse(_stdout(_LISTING, _APPLY, _FULL))
    curl = _by_name(packages)["curl"]
    assert curl["held_back"] is False
    assert curl["held_back_reason"] is None


def test_reason_is_none_when_full_upgrade_section_absent():
    """Older output (and the Proxmox path) carry no __FULL__ marker.

    The reason must be None rather than a wrong guess, so the UI can fall back
    to the combined wording instead of asserting something untrue.
    """
    body = f"{_LISTING}\n__APPLY__\n{_APPLY}\n__REBOOT__\nno"
    packages, _ = _PM.parse(body)
    kernel = _by_name(packages)["linux-image-amd64"]
    assert kernel["held_back"] is True
    assert kernel["held_back_reason"] is None


def test_held_back_flag_is_unchanged_by_the_new_section():
    """OP#179's behaviour must not shift — only the reason is new."""
    packages, _ = _PM.parse(_stdout(_LISTING, _APPLY, _FULL))
    held = {p["name"] for p in packages if p["held_back"]}
    assert held == {"linux-image-amd64", "libfoo"}


def test_kernel_held_back_does_not_flag_reboot_required():
    """A kernel that will never be installed must not claim a pending reboot."""
    _, reboot = _PM.parse(_stdout(_LISTING, _APPLY, _FULL))
    assert reboot is False


# ---------------------------------------------------------------------------
# The command that produces the data
# ---------------------------------------------------------------------------


def test_list_cmd_simulates_full_upgrade():
    cmd = _PM.list_cmd()
    assert "__FULL__" in cmd
    assert "apt-get -s full-upgrade" in cmd


def test_list_cmd_full_upgrade_is_only_ever_simulated():
    """A check must never mutate the host — `-s` is not optional here."""
    cmd = _PM.list_cmd()
    assert "full-upgrade" in cmd
    for segment in cmd.split(";"):
        if "full-upgrade" in segment:
            assert "-s" in segment.split("full-upgrade")[0]


def test_list_cmd_markers_appear_in_parse_order():
    cmd = _PM.list_cmd()
    assert cmd.index("__APPLY__") < cmd.index("__FULL__") < cmd.index("__REBOOT__")


def test_list_cmd_sections_survive_a_real_shell():
    """Run the emitted command shape through an actual shell.

    OP#240 shipped a command that was correct as a string and broken in a
    shell. Here the apt calls are replaced with stubs, but the redirections,
    separators and marker echoes are the real ones.
    """
    cmd = _PM.list_cmd(refresh=False)
    stub = (
        "apt() { echo 'Listing...'; }; "
        "apt-get() { echo 'Inst curl [1] (2 Debian:13 [amd64])'; }; "
    )
    out = subprocess.run(
        stub + cmd, shell=True, capture_output=True, text=True
    ).stdout
    assert "__APPLY__" in out
    assert "__FULL__" in out
    assert "__REBOOT__" in out
    # and the parser can still read what the shell produced
    packages, _ = _PM.parse(out)
    assert isinstance(packages, list)


# ---------------------------------------------------------------------------
# The rendered copy — this is the defect the user actually reads (OP#241)
# ---------------------------------------------------------------------------

from pathlib import Path  # noqa: E402

from jinja2 import Environment, FileSystemLoader, select_autoescape  # noqa: E402


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


def _held(name, reason):
    return {
        "name": name,
        "current": "1.0",
        "available": "2.0",
        "held_back": True,
        "held_back_reason": reason,
    }


def test_full_upgrade_package_is_not_described_as_self_resolving():
    """The reported bug: a permanently-deferred package claimed to roll out."""
    html = _render(packages=[_held("linux-image-amd64", "needs_full_upgrade")])
    assert "roll out automatically" not in html
    assert "otherwise up to date" not in html
    assert "never apply" in html
    assert "full-upgrade" in html


def test_full_upgrade_package_says_waiting_will_not_help():
    html = _render(packages=[_held("linux-image-amd64", "needs_full_upgrade")])
    assert "Waiting will not clear" in html


def test_phased_package_still_says_it_resolves_itself():
    """The phased case was always correct and must stay that way."""
    html = _render(packages=[_held("libfoo", "phased")])
    assert "phased" in html
    assert "roll out automatically" in html
    assert "never apply" not in html


def test_mixed_causes_are_reported_separately():
    html = _render(packages=[
        _held("linux-image-amd64", "needs_full_upgrade"),
        _held("libfoo", "phased"),
    ])
    assert "never apply" in html          # the full-upgrade one
    assert "roll out automatically" in html  # the phased one


def test_unknown_reason_falls_back_without_claiming_self_resolution():
    """Older/Proxmox output carries no reason — say less, not something false."""
    html = _render(packages=[_held("linux-image-amd64", None)])
    assert "won&#39;t apply" in html or "won't apply" in html
    assert "roll out automatically" not in html
    assert "otherwise up to date" not in html


def test_full_upgrade_hold_back_is_visually_distinct_from_phased():
    """A permanently-deferred security package should not read as routine."""
    full = _render(packages=[_held("linux-image-amd64", "needs_full_upgrade")])
    phased = _render(packages=[_held("libfoo", "phased")])
    assert "amber" in full
    assert "amber" not in phased
