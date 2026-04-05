"""Notification store — persisted to data dir, drives the bell badge."""

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

_DATA_DIR = Path(os.getenv("DATA_PATH", "/app/data"))
_NOTIF_PATH = _DATA_DIR / "notifications.json"
_lock = threading.Lock()
_MAX = 50


def _load() -> list[dict]:
    if not _NOTIF_PATH.exists():
        return []
    try:
        return json.loads(_NOTIF_PATH.read_text())
    except Exception:
        return []


def _save(entries: list[dict]) -> None:
    _NOTIF_PATH.parent.mkdir(parents=True, exist_ok=True)
    _NOTIF_PATH.write_text(json.dumps(entries, indent=2))


def notify(title: str, message: str, level: str = "error") -> None:
    """Add a notification. Also fires Pushover if configured."""
    with _lock:
        entries = _load()
        entries.insert(
            0,
            {
                "id": uuid.uuid4().hex[:8],
                "title": title,
                "message": message,
                "level": level,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "read": False,
            },
        )
        _save(entries[:_MAX])
    # Fire-and-forget Pushover push
    try:
        import asyncio
        from .pushover import send_pushover

        asyncio.ensure_future(send_pushover(title, message))
    except Exception:
        pass


def get_unread_count() -> int:
    return sum(1 for e in _load() if not e.get("read"))


def get_notifications(limit: int = 20) -> list[dict]:
    return _load()[:limit]


def mark_all_read() -> None:
    with _lock:
        entries = _load()
        for e in entries:
            e["read"] = True
        _save(entries)
