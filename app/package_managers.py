"""
Package manager abstraction.

Each PackageManager knows how to:
  - produce a single shell command that lists available updates
  - parse that command's stdout into a list of Package dicts
  - determine whether a reboot is recommended from its output + the package list

Reboot logic has two independent signals — either triggers the flag:
  1. PM-provided reboot indicator (e.g. /var/run/reboot-required, needs-restarting)
  2. A kernel package appearing in the update list
"""

from __future__ import annotations

import re


# ---------------------------------------------------------------------------
# Package dict shape
# ---------------------------------------------------------------------------

# {"name": str, "current": str, "available": str}
Package = dict[str, str]


# ---------------------------------------------------------------------------
# Kernel package heuristic (shared across all PMs)
# ---------------------------------------------------------------------------

_KERNEL_NAMES = frozenset({"linux", "linux-lts", "linux-zen", "linux-hardened"})

def _is_kernel_package(name: str) -> bool:
    return (
        name in _KERNEL_NAMES
        or name.startswith("kernel")
        or name.startswith("linux-image")
        or name.startswith("linux-headers")
    )


def _kernel_update_in(packages: list[Package]) -> bool:
    return any(_is_kernel_package(p["name"]) for p in packages)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

# Single command that prints the PM name and nothing else.
DETECT_CMD = (
    "command -v apt-get >/dev/null 2>&1 && echo apt && exit; "
    "command -v dnf    >/dev/null 2>&1 && echo dnf && exit; "
    "command -v yum    >/dev/null 2>&1 && echo yum && exit; "
    "command -v zypper >/dev/null 2>&1 && echo zypper && exit; "
    "command -v pacman >/dev/null 2>&1 && echo pacman && exit; "
    "command -v apk    >/dev/null 2>&1 && echo apk && exit; "
    "echo unknown"
)


def get_package_manager(name: str) -> "PackageManager":
    return _REGISTRY.get(name.strip(), UnknownPackageManager(name.strip()))


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

class PackageManager:
    name: str = "unknown"

    def list_cmd(self) -> str:
        """Single shell command — stdout fed to parse()."""
        raise NotImplementedError

    def upgrade_cmd(self) -> str:
        """Command that upgrades all packages non-interactively."""
        raise NotImplementedError

    def parse(self, stdout: str) -> tuple[list[Package], bool]:
        """
        Returns (packages, reboot_required).
        reboot_required is True when the PM or kernel heuristic says so.
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# APT  (Debian / Ubuntu / Raspberry Pi OS)
# ---------------------------------------------------------------------------

class AptPackageManager(PackageManager):
    name = "apt"

    def list_cmd(self) -> str:
        return (
            "apt-get update -qq 2>/dev/null; "
            "apt list --upgradable 2>/dev/null; "
            "echo __REBOOT__; "
            "[ -f /var/run/reboot-required ] && echo yes || echo no"
        )

    def upgrade_cmd(self) -> str:
        return "DEBIAN_FRONTEND=noninteractive apt-get upgrade -y 2>&1"

    def parse(self, stdout: str) -> tuple[list[Package], bool]:
        reboot_required = False
        if "__REBOOT__" in stdout:
            apt_part, reboot_part = stdout.split("__REBOOT__", 1)
            reboot_required = reboot_part.strip().startswith("yes")
        else:
            apt_part = stdout

        packages: list[Package] = []
        for line in apt_part.splitlines():
            if "[upgradable from:" not in line:
                continue
            try:
                name = line.split("/")[0]
                parts = line.split()
                available = parts[1] if len(parts) > 1 else "?"
                current = parts[-1].rstrip("]") if len(parts) > 3 else "?"
                packages.append({"name": name, "current": current, "available": available})
            except Exception:
                continue

        return packages, reboot_required or _kernel_update_in(packages)


# ---------------------------------------------------------------------------
# DNF  (Fedora / RHEL 8+ / Rocky / AlmaLinux)
# ---------------------------------------------------------------------------

class DnfPackageManager(PackageManager):
    name = "dnf"

    def list_cmd(self) -> str:
        # dnf check-update exits 100 when updates exist, 0 when none, other on error.
        # needs-restarting -r exits 1 when reboot needed (may not be installed).
        return (
            "dnf check-update -q 2>/dev/null; echo __EXIT__$?; "
            "echo __REBOOT__; "
            "needs-restarting -r >/dev/null 2>&1; echo $?"
        )

    def upgrade_cmd(self) -> str:
        return "dnf upgrade -y 2>&1"

    def parse(self, stdout: str) -> tuple[list[Package], bool]:
        packages: list[Package] = []
        reboot_required = False

        # Split on sentinels
        parts = stdout.split("__EXIT__")
        update_block = parts[0]

        if len(parts) > 1:
            after_exit = parts[1]
            reboot_block = after_exit.split("__REBOOT__", 1)[-1].strip() if "__REBOOT__" in after_exit else ""
            # needs-restarting -r returns 1 when reboot needed
            reboot_required = reboot_block.strip().startswith("1")

        # Parse dnf check-update output:
        # name.arch    available_version    repo
        for line in update_block.splitlines():
            line = line.strip()
            if not line or line.startswith("Last metadata") or line.startswith("Obsoleting"):
                continue
            cols = line.split()
            if len(cols) < 3:
                continue
            name_arch = cols[0]
            available = cols[1]
            # Strip arch suffix (e.g. bash.x86_64 → bash)
            name = name_arch.rsplit(".", 1)[0]
            packages.append({"name": name, "current": "installed", "available": available})

        return packages, reboot_required or _kernel_update_in(packages)


# ---------------------------------------------------------------------------
# YUM  (CentOS 7 / RHEL 7)
# ---------------------------------------------------------------------------

class YumPackageManager(PackageManager):
    name = "yum"

    def list_cmd(self) -> str:
        return (
            "yum check-update -q 2>/dev/null; echo __EXIT__$?; "
            "echo __REBOOT__; "
            "needs-restarting -r >/dev/null 2>&1; echo $?"
        )

    def upgrade_cmd(self) -> str:
        return "yum upgrade -y 2>&1"

    def parse(self, stdout: str) -> tuple[list[Package], bool]:
        # Same format as dnf
        return DnfPackageManager().parse(stdout)


# ---------------------------------------------------------------------------
# Zypper  (openSUSE / SLES)
# ---------------------------------------------------------------------------

class ZypperPackageManager(PackageManager):
    name = "zypper"

    def list_cmd(self) -> str:
        return (
            "zypper -q lu 2>/dev/null; "
            "echo __REBOOT__; "
            "zypper needs-rebooting >/dev/null 2>&1 && echo yes || echo no"
        )

    def upgrade_cmd(self) -> str:
        return "zypper --non-interactive up 2>&1"

    def parse(self, stdout: str) -> tuple[list[Package], bool]:
        packages: list[Package] = []
        reboot_required = False

        if "__REBOOT__" in stdout:
            zypper_part, reboot_part = stdout.split("__REBOOT__", 1)
            reboot_required = reboot_part.strip().startswith("yes")
        else:
            zypper_part = stdout

        # zypper lu columns: S | Repo | Name | Current Version | Available Version | Arch
        for line in zypper_part.splitlines():
            if "|" not in line:
                continue
            cols = [c.strip() for c in line.split("|")]
            if len(cols) < 6:
                continue
            status = cols[0]
            if status not in ("v", "i", ">"):
                continue
            name = cols[2]
            current = cols[3]
            available = cols[4]
            if name and available:
                packages.append({"name": name, "current": current, "available": available})

        return packages, reboot_required or _kernel_update_in(packages)


# ---------------------------------------------------------------------------
# Pacman  (Arch Linux / Manjaro)
# ---------------------------------------------------------------------------

class PacmanPackageManager(PackageManager):
    name = "pacman"

    def list_cmd(self) -> str:
        # pacman -Qu lists packages with pending updates: "name old -> new"
        return "pacman -Sy -q 2>/dev/null; pacman -Qu 2>/dev/null"

    def upgrade_cmd(self) -> str:
        return "pacman -Su --noconfirm 2>&1"

    def parse(self, stdout: str) -> tuple[list[Package], bool]:
        packages: list[Package] = []
        # Format: "bash 5.2.021-2 -> 5.2.026-1"
        for line in stdout.splitlines():
            line = line.strip()
            if not line or line.startswith("::") or "->" not in line:
                continue
            parts = line.split()
            if len(parts) < 4:
                continue
            name = parts[0]
            current = parts[1]
            available = parts[3]
            packages.append({"name": name, "current": current, "available": available})

        return packages, _kernel_update_in(packages)


# ---------------------------------------------------------------------------
# APK  (Alpine Linux)
# ---------------------------------------------------------------------------

class ApkPackageManager(PackageManager):
    name = "apk"

    def list_cmd(self) -> str:
        # apk version -q -l '<' lists packages where installed < available
        return "apk update -q 2>/dev/null; apk version -q -l '<' 2>/dev/null"

    def upgrade_cmd(self) -> str:
        return "apk upgrade 2>&1"

    def parse(self, stdout: str) -> tuple[list[Package], bool]:
        packages: list[Package] = []
        # Format: "bash-5.1.16-r2 < 5.2.0-r0 [community]"
        for line in stdout.splitlines():
            line = line.strip()
            if "<" not in line or line.startswith("fetch") or line.startswith("OK"):
                continue
            # Split on whitespace, find < separator
            parts = line.split()
            if len(parts) < 3 or parts[1] != "<":
                continue
            # "bash-5.1.16-r2" → name="bash", current="5.1.16-r2"
            pkg_ver = parts[0]
            # Split on last hyphen-preceded-by-digit run
            match = re.match(r"^(.+?)-(\d.*)$", pkg_ver)
            if match:
                name, current = match.group(1), match.group(2)
            else:
                name, current = pkg_ver, "?"
            available = parts[2]
            packages.append({"name": name, "current": current, "available": available})

        return packages, _kernel_update_in(packages)


# ---------------------------------------------------------------------------
# Unknown fallback
# ---------------------------------------------------------------------------

class UnknownPackageManager(PackageManager):
    def __init__(self, name: str = "unknown"):
        self.name = name

    def list_cmd(self) -> str:
        return "echo 'Package manager not supported'"

    def upgrade_cmd(self) -> str:
        return "echo 'Package manager not supported'"

    def parse(self, stdout: str) -> tuple[list[Package], bool]:
        return [], False


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, PackageManager] = {
    "apt":    AptPackageManager(),
    "dnf":    DnfPackageManager(),
    "yum":    YumPackageManager(),
    "zypper": ZypperPackageManager(),
    "pacman": PacmanPackageManager(),
    "apk":    ApkPackageManager(),
}
