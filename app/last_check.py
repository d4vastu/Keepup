"""When Keepup last actually checked each host and the container backends.

Written by the scheduled job *and* by the on-demand dashboard checks, so the
stored time means "when we last looked", not "when the scheduler last ran". It
persists because the dashboard renders it on first paint, before any check of
its own has finished; `update_check_cache` stays process-local and keeps its own
job of suppressing redundant `apt-get update` runs.
"""

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

_DATA_DIR = Path(os.getenv("DATA_PATH", "/app/data"))
_PATH = _DATA_DIR / "last_check.json"
_lock = threading.Lock()


def _empty() -> dict:
    return {"hosts": {}, "containers": None}


def _load() -> dict:
    if not _PATH.exists():
        return _empty()
    try:
        data = json.loads(_PATH.read_text())
    except Exception:
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    hosts = data.get("hosts")
    return {
        "hosts": hosts if isinstance(hosts, dict) else {},
        "containers": data.get("containers"),
    }


def _save(state: dict) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(state, indent=2))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def record_host_check(slug: str) -> None:
    with _lock:
        state = _load()
        state["hosts"][slug] = _now()
        _save(state)


def record_container_check() -> None:
    with _lock:
        state = _load()
        state["containers"] = _now()
        _save(state)


def oldest_host_check(slugs: list[str]) -> datetime | None:
    """The staleset check among `slugs`, so the caller can say "nothing here is
    older than this". Hosts never checked are skipped rather than treated as
    infinitely old, and hosts no longer configured are ignored entirely."""
    state = _load()
    seen = [_parse(state["hosts"].get(s)) for s in slugs]
    times = [t for t in seen if t is not None]
    return min(times) if times else None


def container_check() -> datetime | None:
    return _parse(_load()["containers"])


def relative(when: datetime | None) -> str:
    """Short wording for a section header: "14m ago", "3h ago", "never checked"."""
    if when is None:
        return "never checked"
    seconds = (datetime.now(timezone.utc) - when).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"
