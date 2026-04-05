import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

_DATA_DIR = Path(os.getenv("DATA_PATH", "/app/data"))
_LOG_PATH = _DATA_DIR / "auto_update_log.json"
_lock = threading.Lock()
_MAX_ENTRIES = 100


def _load() -> list[dict]:
    if not _LOG_PATH.exists():
        return []
    try:
        return json.loads(_LOG_PATH.read_text())
    except Exception:
        return []


def _save(entries: list[dict]) -> None:
    _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _LOG_PATH.write_text(json.dumps(entries, indent=2))


def append_log(
    type: str,
    target: str,
    target_name: str,
    status: str,
    lines: list[str],
) -> None:
    """Prepend a new auto-update result entry. type is 'os' or 'docker'."""
    with _lock:
        entries = _load()
        entries.insert(
            0,
            {
                "id": uuid.uuid4().hex[:8],
                "type": type,
                "target": target,
                "target_name": target_name,
                "ran_at": datetime.now(timezone.utc).isoformat(),
                "status": status,
                "lines": lines[-50:],
                "read": False,
            },
        )
        _save(entries[:_MAX_ENTRIES])


def get_unread_error_count() -> int:
    entries = _load()
    return sum(1 for e in entries if not e.get("read") and e.get("status") == "error")


def get_recent(limit: int = 20) -> list[dict]:
    return _load()[:limit]


def mark_all_read() -> None:
    with _lock:
        entries = _load()
        for e in entries:
            e["read"] = True
        _save(entries)
