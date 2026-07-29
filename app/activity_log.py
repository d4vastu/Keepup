"""Persistent record of every update, redeploy and restart run.

Each finished run produces one small index record (metadata) plus one output
file holding its full captured output. The index is small enough to rewrite on
every append; output files are written once and deleted when their record is
pruned.

Replaces ``auto_update_log``, which kept only the last 50 lines of scheduled
runs and nothing at all for manual ones.

Recording is best-effort by design: ``record_run`` swallows every exception,
because failing to write history must never fail the update being described.
"""

import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_DATA_DIR = Path(os.getenv("DATA_PATH", "/app/data"))
_ACTIVITY_DIR = _DATA_DIR / "activity"
_INDEX_PATH = _ACTIVITY_DIR / "index.json"
_RUNS_DIR = _ACTIVITY_DIR / "runs"

_lock = threading.Lock()

MAX_ENTRIES = 500
MAX_AGE_DAYS = 90

# Run ids are uuid4 hex truncated to 8 chars. get_run_output takes its id from a
# URL path, so anything not matching this never touches the filesystem.
_ID_RE = re.compile(r"^[0-9a-f]{8}$")

_OUTPUT_UNAVAILABLE = "Output unavailable — it was not written to disk."


# ---------------------------------------------------------------------------
# Index I/O
# ---------------------------------------------------------------------------


def _load_index() -> list[dict]:
    """Return the index, or an empty list if it is missing or unreadable."""
    if not _INDEX_PATH.exists():
        return []
    try:
        data = json.loads(_INDEX_PATH.read_text())
        if not isinstance(data, list):
            raise ValueError("index is not a list")
        return data
    except Exception as exc:
        log.error("Activity index unreadable (%s) — preserved as index.json.corrupt", exc)
        try:
            os.replace(_INDEX_PATH, _ACTIVITY_DIR / "index.json.corrupt")
        except OSError:
            pass
        return []


def _save_index(entries: list[dict]) -> None:
    """Write the index atomically so a crash mid-write cannot truncate history."""
    _ACTIVITY_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _ACTIVITY_DIR / "index.json.tmp"
    tmp.write_text(json.dumps(entries, indent=2))
    os.replace(tmp, _INDEX_PATH)


# ---------------------------------------------------------------------------
# Output files
# ---------------------------------------------------------------------------


def _write_output(run_id: str, lines: list[str]) -> int:
    """Write one run's output. Returns the line count, or 0 if it could not."""
    try:
        _RUNS_DIR.mkdir(parents=True, exist_ok=True)
        (_RUNS_DIR / f"{run_id}.log").write_text("\n".join(lines))
        return len(lines)
    except OSError as exc:
        log.error("Could not write activity output for run %s: %s", run_id, exc)
        return 0


def _delete_orphan_outputs(keep_ids: set[str]) -> None:
    """Remove output files with no surviving index record.

    Defensive throughout: cleanup is housekeeping, and must never be the reason
    a run fails to be recorded.
    """
    if not _RUNS_DIR.is_dir():
        return
    try:
        stale = [p for p in _RUNS_DIR.glob("*.log") if p.stem not in keep_ids]
    except OSError:
        return
    for path in stale:
        try:
            path.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------


def _parse_ts(value: str, default: datetime) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return default
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _prune(entries: list[dict]) -> list[dict]:
    """Drop records past MAX_ENTRIES or older than MAX_AGE_DAYS, and their files."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=MAX_AGE_DAYS)
    kept = [
        e
        for e in entries[:MAX_ENTRIES]
        if _parse_ts(e.get("started_at", ""), now) >= cutoff
    ]
    _delete_orphan_outputs({e.get("id", "") for e in kept})
    return kept


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def record_run(
    kind: str,
    target: str,
    target_name: str,
    trigger: str,
    status: str,
    output: list[str],
    started_at: str = "",
    error: str = "",
) -> str:
    """Persist one finished run.

    ``kind`` is one of ``os_upgrade``, ``container_redeploy``, ``reboot``;
    ``trigger`` is ``manual`` or ``scheduled``; ``status`` is ``success``,
    ``error`` or ``skipped``. Returns the new run id, or ``""`` if recording
    failed — callers must not treat that as an update failure.
    """
    run_id = uuid.uuid4().hex[:8]
    try:
        finished = datetime.now(timezone.utc)
        started = _parse_ts(started_at, finished)
        line_count = _write_output(run_id, list(output or []))
        record = {
            "id": run_id,
            "kind": kind,
            "target": target,
            "target_name": target_name,
            "trigger": trigger,
            "status": status,
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "duration_s": round(max((finished - started).total_seconds(), 0.0), 1),
            "error": error,
            "line_count": line_count,
        }
        with _lock:
            entries = _load_index()
            entries.insert(0, record)
            _save_index(_prune(entries))
        return run_id
    except Exception:
        log.exception("Failed to record activity for %s %s", kind, target)
        return ""


def get_recent(
    limit: int = 100,
    status: str = "",
    trigger: str = "",
    kind: str = "",
) -> list[dict]:
    """Return up to ``limit`` records, newest first, optionally filtered."""
    entries = _load_index()
    if status:
        entries = [e for e in entries if e.get("status") == status]
    if trigger:
        entries = [e for e in entries if e.get("trigger") == trigger]
    if kind:
        entries = [e for e in entries if e.get("kind") == kind]
    return entries[:limit]


def get_run(run_id: str) -> dict | None:
    """Return one index record, or None."""
    return next((e for e in _load_index() if e.get("id") == run_id), None)


def get_run_output(run_id: str) -> list[str]:
    """Return a run's full output. Empty list for an unknown or malformed id."""
    if not _ID_RE.match(run_id or ""):
        return []
    try:
        text = (_RUNS_DIR / f"{run_id}.log").read_text()
    except OSError:
        return [_OUTPUT_UNAVAILABLE]
    return text.split("\n") if text else []
