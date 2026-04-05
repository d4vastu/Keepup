"""Tests for package manager detection and output parsing."""

import pytest
from app.package_managers import (
    AptPackageManager,
    DnfPackageManager,
    YumPackageManager,
    ZypperPackageManager,
    PacmanPackageManager,
    ApkPackageManager,
    UnknownPackageManager,
    get_package_manager,
    _is_kernel_package,
    _kernel_update_in,
)


# ---------------------------------------------------------------------------
# Kernel heuristic
# ---------------------------------------------------------------------------


def test_is_kernel_package_linux_image():
    assert _is_kernel_package("linux-image-6.1.0-amd64")


def test_is_kernel_package_linux_headers():
    assert _is_kernel_package("linux-headers-generic")


def test_is_kernel_package_kernel():
    assert _is_kernel_package("kernel")


def test_is_kernel_package_kernel_core():
    assert _is_kernel_package("kernel-core")


def test_is_kernel_package_linux_arch():
    assert _is_kernel_package("linux")


def test_is_kernel_package_linux_lts():
    assert _is_kernel_package("linux-lts")


def test_is_not_kernel_package():
    assert not _is_kernel_package("bash")
    assert not _is_kernel_package("nginx")
    assert not _is_kernel_package("curl")


def test_kernel_update_in_detects():
    packages = [
        {"name": "bash", "current": "5.0", "available": "5.1"},
        {"name": "linux-image-6.1", "current": "6.0", "available": "6.1"},
    ]
    assert _kernel_update_in(packages)


def test_kernel_update_in_false():
    packages = [{"name": "bash", "current": "5.0", "available": "5.1"}]
    assert not _kernel_update_in(packages)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_get_package_manager_apt():
    assert get_package_manager("apt").name == "apt"


def test_get_package_manager_dnf():
    assert get_package_manager("dnf").name == "dnf"


def test_get_package_manager_yum():
    assert get_package_manager("yum").name == "yum"


def test_get_package_manager_zypper():
    assert get_package_manager("zypper").name == "zypper"


def test_get_package_manager_pacman():
    assert get_package_manager("pacman").name == "pacman"


def test_get_package_manager_apk():
    assert get_package_manager("apk").name == "apk"


def test_get_package_manager_unknown():
    pm = get_package_manager("unknown")
    assert isinstance(pm, UnknownPackageManager)


def test_get_package_manager_empty_string():
    pm = get_package_manager("")
    assert isinstance(pm, UnknownPackageManager)


# ---------------------------------------------------------------------------
# APT
# ---------------------------------------------------------------------------

APT_OUTPUT = (
    "Listing... Done\n"
    "curl/stable 8.0.0 amd64 [upgradable from: 7.0.0]\n"
    "nginx/stable 1.26.0-1 amd64 [upgradable from: 1.24.0-1]\n"
    "__REBOOT__\n"
    "no\n"
)

APT_REBOOT_OUTPUT = (
    "linux-image-6.1.0/stable 6.1.0 amd64 [upgradable from: 6.0.0]\n"
    "__REBOOT__\n"
    "yes\n"
)


def test_apt_parses_packages():
    pm = AptPackageManager()
    packages, reboot = pm.parse(APT_OUTPUT)
    assert len(packages) == 2
    assert packages[0]["name"] == "curl"
    assert packages[0]["current"] == "7.0.0"
    assert packages[0]["available"] == "8.0.0"


def test_apt_parses_nginx():
    pm = AptPackageManager()
    packages, _ = pm.parse(APT_OUTPUT)
    nginx = next(p for p in packages if p["name"] == "nginx")
    assert nginx["available"] == "1.26.0-1"
    assert nginx["current"] == "1.24.0-1"


def test_apt_reboot_not_required():
    pm = AptPackageManager()
    _, reboot = pm.parse(APT_OUTPUT)
    assert reboot is False


def test_apt_reboot_required_by_flag():
    pm = AptPackageManager()
    _, reboot = pm.parse(APT_OUTPUT.replace("__REBOOT__\nno", "__REBOOT__\nyes"))
    assert reboot is True


def test_apt_reboot_required_by_kernel():
    pm = AptPackageManager()
    _, reboot = pm.parse(APT_REBOOT_OUTPUT)
    # /var/run/reboot-required says yes AND kernel package
    assert reboot is True


def test_apt_kernel_triggers_reboot_even_without_file():
    pm = AptPackageManager()
    output = "linux-image-6.1.0/stable 6.1.0 amd64 [upgradable from: 6.0.0]\n__REBOOT__\nno\n"
    _, reboot = pm.parse(output)
    assert reboot is True  # kernel heuristic fires


def test_apt_empty_output():
    pm = AptPackageManager()
    packages, reboot = pm.parse("__REBOOT__\nno\n")
    assert packages == []
    assert reboot is False


def test_apt_no_reboot_sentinel():
    pm = AptPackageManager()
    packages, reboot = pm.parse("curl/stable 8.0.0 amd64 [upgradable from: 7.0.0]\n")
    assert len(packages) == 1
    assert reboot is False


def test_apt_upgrade_cmd():
    pm = AptPackageManager()
    assert "apt-get upgrade" in pm.upgrade_cmd()
    assert "DEBIAN_FRONTEND=noninteractive" in pm.upgrade_cmd()


# ---------------------------------------------------------------------------
# DNF
# ---------------------------------------------------------------------------

DNF_OUTPUT = (
    "Last metadata expiration check: 0:01:00 ago.\n"
    "\n"
    "bash.x86_64                   5.1.8-6.el9     baseos\n"
    "kernel.x86_64                 5.14.0-362.el9  baseos\n"
    "curl.x86_64                   7.76.1-26.el9   baseos\n"
    "__EXIT__100\n"
    "__REBOOT__\n"
    "0\n"
)

DNF_NO_UPDATES = "__EXIT__0\n__REBOOT__\n0\n"


def test_dnf_parses_packages():
    pm = DnfPackageManager()
    packages, _ = pm.parse(DNF_OUTPUT)
    names = [p["name"] for p in packages]
    assert "bash" in names
    assert "kernel" in names
    assert "curl" in names


def test_dnf_strips_arch():
    pm = DnfPackageManager()
    packages, _ = pm.parse(DNF_OUTPUT)
    assert all("." not in p["name"] for p in packages)


def test_dnf_reboot_by_needs_restarting():
    pm = DnfPackageManager()
    output = DNF_OUTPUT.replace("__REBOOT__\n0", "__REBOOT__\n1")
    _, reboot = pm.parse(output)
    assert reboot is True


def test_dnf_reboot_by_kernel():
    pm = DnfPackageManager()
    _, reboot = pm.parse(DNF_OUTPUT)
    assert reboot is True  # kernel.x86_64 in packages


def test_dnf_no_updates_no_reboot():
    pm = DnfPackageManager()
    packages, reboot = pm.parse(DNF_NO_UPDATES)
    assert packages == []
    assert reboot is False


def test_dnf_upgrade_cmd():
    assert "dnf upgrade" in DnfPackageManager().upgrade_cmd()


# ---------------------------------------------------------------------------
# YUM (delegates to DNF parser)
# ---------------------------------------------------------------------------


def test_yum_parses_same_as_dnf():
    yum = YumPackageManager()
    dnf = DnfPackageManager()
    yum_pkgs, yum_reboot = yum.parse(DNF_OUTPUT)
    dnf_pkgs, dnf_reboot = dnf.parse(DNF_OUTPUT)
    assert yum_pkgs == dnf_pkgs
    assert yum_reboot == dnf_reboot


def test_yum_upgrade_cmd():
    assert "yum upgrade" in YumPackageManager().upgrade_cmd()


# ---------------------------------------------------------------------------
# Zypper
# ---------------------------------------------------------------------------

ZYPPER_OUTPUT = (
    "Loading repository data...\n"
    "Reading installed packages...\n"
    "S | Repository   | Name           | Current Version | Available Version | Arch\n"
    "--+-------------+----------------+-----------------+-------------------+-------\n"
    "v | Main Update  | bash           | 4.4-19.9        | 5.1-1.1           | x86_64\n"
    "v | Main Update  | kernel-default | 5.14.21         | 6.1.0             | x86_64\n"
    "__REBOOT__\n"
    "no\n"
)


def test_zypper_parses_packages():
    pm = ZypperPackageManager()
    packages, _ = pm.parse(ZYPPER_OUTPUT)
    names = [p["name"] for p in packages]
    assert "bash" in names
    assert "kernel-default" in names


def test_zypper_versions():
    pm = ZypperPackageManager()
    packages, _ = pm.parse(ZYPPER_OUTPUT)
    bash = next(p for p in packages if p["name"] == "bash")
    assert bash["current"] == "4.4-19.9"
    assert bash["available"] == "5.1-1.1"


def test_zypper_reboot_by_kernel():
    pm = ZypperPackageManager()
    _, reboot = pm.parse(ZYPPER_OUTPUT)
    assert reboot is True  # kernel-default triggers heuristic


def test_zypper_reboot_by_flag():
    pm = ZypperPackageManager()
    output = ZYPPER_OUTPUT.replace("__REBOOT__\nno", "__REBOOT__\nyes")
    _, reboot = pm.parse(output)
    assert reboot is True


def test_zypper_upgrade_cmd():
    assert "zypper" in ZypperPackageManager().upgrade_cmd()


# ---------------------------------------------------------------------------
# Pacman
# ---------------------------------------------------------------------------

PACMAN_OUTPUT = (
    "bash 5.2.021-2 -> 5.2.026-1\n"
    "linux 6.6.1.arch1-1 -> 6.7.0.arch1-1\n"
    "curl 8.0.0-1 -> 8.1.0-1\n"
)


def test_pacman_parses_packages():
    pm = PacmanPackageManager()
    packages, _ = pm.parse(PACMAN_OUTPUT)
    names = [p["name"] for p in packages]
    assert "bash" in names
    assert "linux" in names
    assert "curl" in names


def test_pacman_versions():
    pm = PacmanPackageManager()
    packages, _ = pm.parse(PACMAN_OUTPUT)
    bash = next(p for p in packages if p["name"] == "bash")
    assert bash["current"] == "5.2.021-2"
    assert bash["available"] == "5.2.026-1"


def test_pacman_reboot_by_linux():
    pm = PacmanPackageManager()
    _, reboot = pm.parse(PACMAN_OUTPUT)
    assert reboot is True  # linux package


def test_pacman_no_reboot_no_kernel():
    pm = PacmanPackageManager()
    output = "bash 5.2.021-2 -> 5.2.026-1\n"
    _, reboot = pm.parse(output)
    assert reboot is False


def test_pacman_skips_informational_lines():
    pm = PacmanPackageManager()
    output = ":: Synchronizing package databases...\nbash 5.2.021-2 -> 5.2.026-1\n"
    packages, _ = pm.parse(output)
    assert len(packages) == 1


def test_pacman_upgrade_cmd():
    assert "pacman" in PacmanPackageManager().upgrade_cmd()


# ---------------------------------------------------------------------------
# APK
# ---------------------------------------------------------------------------

APK_OUTPUT = (
    "bash-5.1.16-r2 < 5.2.0-r0 [community]\n"
    "linux-lts-6.1.57-r0 < 6.1.63-r0 [community]\n"
    "curl-8.1.2-r0 < 8.4.0-r0 [community]\n"
)


def test_apk_parses_packages():
    pm = ApkPackageManager()
    packages, _ = pm.parse(APK_OUTPUT)
    names = [p["name"] for p in packages]
    assert "bash" in names
    assert "linux-lts" in names
    assert "curl" in names


def test_apk_versions():
    pm = ApkPackageManager()
    packages, _ = pm.parse(APK_OUTPUT)
    bash = next(p for p in packages if p["name"] == "bash")
    assert bash["current"] == "5.1.16-r2"
    assert bash["available"] == "5.2.0-r0"


def test_apk_reboot_by_linux_lts():
    pm = ApkPackageManager()
    _, reboot = pm.parse(APK_OUTPUT)
    assert reboot is True


def test_apk_no_reboot_no_kernel():
    pm = ApkPackageManager()
    _, reboot = pm.parse("bash-5.1.16-r2 < 5.2.0-r0 [community]\n")
    assert reboot is False


def test_apk_upgrade_cmd():
    assert "apk upgrade" in ApkPackageManager().upgrade_cmd()


# ---------------------------------------------------------------------------
# UnknownPackageManager
# ---------------------------------------------------------------------------


def test_unknown_returns_empty():
    pm = UnknownPackageManager("mystery")
    packages, reboot = pm.parse("some output")
    assert packages == []
    assert reboot is False


def test_unknown_list_cmd():
    assert "not supported" in UnknownPackageManager().list_cmd()


def test_unknown_upgrade_cmd():
    assert "not supported" in UnknownPackageManager().upgrade_cmd()


# ---------------------------------------------------------------------------
# list_cmd coverage
# ---------------------------------------------------------------------------


def test_dnf_list_cmd():
    assert "dnf check-update" in DnfPackageManager().list_cmd()
    assert "__EXIT__" in DnfPackageManager().list_cmd()


def test_yum_list_cmd():
    assert "yum check-update" in YumPackageManager().list_cmd()


def test_zypper_list_cmd():
    assert "zypper" in ZypperPackageManager().list_cmd()


def test_pacman_list_cmd():
    assert "pacman" in PacmanPackageManager().list_cmd()


def test_apk_list_cmd():
    assert "apk" in ApkPackageManager().list_cmd()


# ---------------------------------------------------------------------------
# Edge case coverage
# ---------------------------------------------------------------------------


def test_dnf_parse_skips_obsoleting_line():
    pm = DnfPackageManager()
    # "Obsoleting Packages" header is skipped; package lines after it are still parsed
    output = "bash.x86_64    5.1.8    baseos\nObsoleting Packages\n__EXIT__100\n__REBOOT__\n0\n"
    packages, _ = pm.parse(output)
    assert len(packages) == 1
    assert packages[0]["name"] == "bash"


def test_dnf_parse_skips_short_lines():
    pm = DnfPackageManager()
    output = "toolong\n__EXIT__100\n__REBOOT__\n0\n"
    packages, _ = pm.parse(output)
    assert packages == []


def test_zypper_parse_no_reboot_sentinel():
    pm = ZypperPackageManager()
    output = "v | Main Update  | bash | 4.4-19.9 | 5.1-1.1 | x86_64\n"
    packages, reboot = pm.parse(output)
    assert len(packages) == 1
    assert reboot is False


def test_zypper_parse_skips_short_pipe_rows():
    pm = ZypperPackageManager()
    output = "v | only | three\n"
    packages, _ = pm.parse(output)
    assert packages == []


def test_pacman_parse_skips_short_arrow_lines():
    pm = PacmanPackageManager()
    output = "name 1.0 ->\n"  # only 3 parts
    packages, _ = pm.parse(output)
    assert packages == []


def test_apk_parse_skips_fetch_lines():
    pm = ApkPackageManager()
    output = "fetch http://dl-cdn.alpinelinux.org/alpine/edge/main\nbash-5.1-r2 < 5.2-r0 [community]\n"
    packages, _ = pm.parse(output)
    assert len(packages) == 1


def test_apk_parse_skips_wrong_separator():
    pm = ApkPackageManager()
    output = "bash-5.1-r2 > 5.2-r0 [community]\n"  # > instead of <, but line has no <
    packages, _ = pm.parse(output)
    assert packages == []


def test_apk_parse_no_regex_match():
    pm = ApkPackageManager()
    # pkg_ver with no leading digit after hyphen → regex fails
    output = "pkgname < 1.0-r0 [community]\n"
    packages, _ = pm.parse(output)
    assert len(packages) == 1
    assert packages[0]["name"] == "pkgname"
    assert packages[0]["current"] == "?"


# ---------------------------------------------------------------------------
# Base class raises NotImplementedError
# ---------------------------------------------------------------------------


def test_base_list_cmd_not_implemented():
    from app.package_managers import PackageManager
    import pytest

    pm = PackageManager()
    with pytest.raises(NotImplementedError):
        pm.list_cmd()


def test_base_upgrade_cmd_not_implemented():
    from app.package_managers import PackageManager

    pm = PackageManager()
    with pytest.raises(NotImplementedError):
        pm.upgrade_cmd()


def test_base_parse_not_implemented():
    from app.package_managers import PackageManager

    pm = PackageManager()
    with pytest.raises(NotImplementedError):
        pm.parse("")
