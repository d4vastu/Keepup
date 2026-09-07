"""Notification store — persisted to data dir, drives the bell badge."""

import asyncio
import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .activity_log import exc_text
from .config_manager import get_pushover_config

logger = logging.getLogger(__name__)

_DATA_DIR = Path(os.getenv("DATA_PATH", "/app/data"))
_NOTIF_PATH = _DATA_DIR / "notifications.json"
_lock = threading.Lock()
_MAX = 50

# Strong references to in-flight push tasks. Without this the event loop holds
# only a weak reference and a push can be collected before its request finishes.
_pending: set[asyncio.Task] = set()


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


def notify(title: str, message: str, level: str = "error", url: str = "") -> None:
    """Add a notification. Also pushes it to Pushover when that is enabled.

    The in-app entry is written first and unconditionally: a push that cannot be
    delivered must never cost the user the record of what happened.
    """
    with _lock:
        entries = _load()
        entry: dict = {
            "id": uuid.uuid4().hex[:8],
            "title": title,
            "message": message,
            "level": level,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "read": False,
        }
        if url:
            entry["url"] = url
        entries.insert(0, entry)
        _save(entries[:_MAX])
    try:
        _dispatch_push(title, message)
    except Exception as e:
        logger.warning("Pushover dispatch failed: %s", exc_text(e))


async def _push(title: str, message: str) -> None:
    """Await one push, logging anything it raises rather than losing it."""
    from .pushover import send_pushover

    try:
        await send_pushover(title, message)
    except Exception as e:
        logger.warning("Pushover push failed: %s", exc_text(e))


def _dispatch_push(title: str, message: str) -> None:
    """Send the push from wherever notify() was called.

    notify() is sync and reached both from request handlers on the event loop and
    from scheduler jobs that may run in a worker thread. `ensure_future` only
    works in the first case; off-loop it raised RuntimeError, which was swallowed
    and left the user with an in-app notification and no push (OP#238).
    """
    if not get_pushover_config().get("enabled", False):
        return

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None:
        task = loop.create_task(_push(title, message))
        _pending.add(task)
        task.add_done_callback(_pending.discard)
    else:
        threading.Thread(
            target=asyncio.run,
            args=(_push(title, message),),
            name="pushover-push",
            daemon=True,
        ).start()


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
