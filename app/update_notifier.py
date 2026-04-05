"""Deduplication store for container image update notifications.

Tracks which stacks have already triggered a notification so we don't
spam on every docker check. Clears entries when the stack goes back
to up-to-date (i.e. after an update is applied).
"""
import json
import os
import threading
from pathlib import Path

_DATA_DIR = Path(os.getenv("DATA_PATH", "/app/data"))
_PATH = _DATA_DIR / "notified_updates.json"
_lock = threading.Lock()


def _load() -> set[str]:
    if not _PATH.exists():
        return set()
    try:
        data = json.loads(_PATH.read_text())
        return set(data) if isinstance(data, list) else set()
    except Exception:
        return set()


def _save(notified: set[str]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(sorted(notified), indent=2))


def check_and_notify(stacks: list[dict]) -> None:
    """Given a list of stack dicts with update_path and update_status,
    fire notifications for newly-available updates and clear stale entries."""
    from .notifications import notify

    with _lock:
        notified = _load()
        changed = False

        for stack in stacks:
            path = stack.get("update_path", "")
            status = stack.get("update_status", "")
            name = stack.get("name", path)

            if status == "update_available":
                if path and path not in notified:
                    notify(
                        f"Image update available: {name}",
                        f"A newer image is available for {name}. "
                        f"Open the dashboard to update.",
                        level="info",
                    )
                    notified.add(path)
                    changed = True
            elif status in ("up_to_date", "mixed"):
                # stack was updated — clear the dedup entry
                if path in notified:
                    notified.discard(path)
                    changed = True

        if changed:
            _save(notified)
