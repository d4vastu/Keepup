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

# The file this module replaces. Imported once on first read, then renamed out
# of the way so it can never be imported twice.
_LEGACY_PATH = _DATA_DIR / "auto_update_log.json"

_LEGACY_KINDS = {"os": "os_upgrade", "docker": "container_redeploy"}

# Guards one whole record_run cycle: the output-file write, the index read,
# and the index write (with any pruning) must happen as a single unit from
# another thread's point of view. Splitting them let one thread's prune sweep
# delete a second thread's output file before its index record had landed.
_lock = threading.Lock()

# Separate from _lock because record_run reaches migration while already
# holding _lock — reusing it would deadlock. Serialises the read/import/rename
# so two concurrent readers cannot both import the same legacy entries.
_migrate_lock = threading.Lock()

MAX_ENTRIES = 500
MAX_AGE_DAYS = 90

# Run ids are uuid4 hex truncated to 8 chars. get_run_output takes its id from a
# URL path, so anything not matching this never touches the filesystem.
_ID_RE = re.compile(r"^[0-9a-f]{8}$")

_OUTPUT_UNAVAILABLE = "Output unavailable — it was not written to disk."


# ---------------------------------------------------------------------------
# Index I/O
# ---------------------------------------------------------------------------


def migrate_legacy_log() -> None:
    """Import ``auto_update_log.json`` once, then rename it out of the way.

    Legacy entries only ever came from the scheduler, so they all migrate as
    ``trigger="scheduled"``. Their ``ran_at`` becomes both start and finish —
    the old format never recorded a duration. The rename happens even when the
    file was unreadable, so a corrupt legacy log is not re-parsed on every
    single read for the rest of the install's life.
    """
    if not _LEGACY_PATH.exists():
        return
    with _migrate_lock:
        # Re-check under the lock: another thread may have finished the whole
        # import while this one was waiting for it.
        if not _LEGACY_PATH.exists():
            return
        try:
            legacy = json.loads(_LEGACY_PATH.read_text())
            if not isinstance(legacy, list):
                raise ValueError("legacy log is not a list")
        except Exception as exc:
            log.error("Legacy auto-update log unreadable (%s) — skipping import", exc)
            legacy = []

        records = []
        for entry in legacy:
            if not isinstance(entry, dict):
                continue
            run_id = uuid.uuid4().hex[:8]
            lines = [str(line) for line in entry.get("lines", [])]
            ran_at = entry.get("ran_at", "")
            failed = entry.get("status") == "error"
            records.append(
                {
                    "id": run_id,
                    "kind": _LEGACY_KINDS.get(entry.get("type", ""), "os_upgrade"),
                    "target": entry.get("target", ""),
                    "target_name": entry.get("target_name", ""),
                    "trigger": "scheduled",
                    "status": entry.get("status", "error"),
                    "started_at": ran_at,
                    "finished_at": ran_at,
                    "duration_s": 0.0,
                    "error": lines[-1] if failed and lines else "",
                    "line_count": _write_output(run_id, redact(lines)),
                }
            )

        try:
            if records:
                # Merged rather than prepended: migration can land after runs
                # have already been recorded, and everything downstream —
                # get_recent, and the MAX_ENTRIES cut in _select_kept — reads
                # the index as newest-first.
                now = datetime.now(timezone.utc)
                merged = records + _read_index_file()
                merged.sort(key=lambda e: _parse_ts(e.get("started_at", ""), now), reverse=True)
                _save_index(merged)
            os.replace(_LEGACY_PATH, _LEGACY_PATH.with_name("auto_update_log.json.migrated"))
            log.info("Migrated %d entries from the legacy auto-update log", len(records))
        except OSError as exc:
            log.error("Legacy auto-update log migration failed: %s", exc)


def _read_index_file() -> list[dict]:
    """Return the index, or an empty list if it is missing or unreadable.

    Non-dict elements are dropped rather than left to crash a later ``.get``
    call on whatever survived the JSON parse.
    """
    if not _INDEX_PATH.exists():
        return []
    try:
        data = json.loads(_INDEX_PATH.read_text())
        if not isinstance(data, list):
            raise ValueError("index is not a list")
        return [e for e in data if isinstance(e, dict)]
    except Exception as exc:
        log.error("Activity index unreadable (%s) — preserved as index.json.corrupt", exc)
        try:
            os.replace(_INDEX_PATH, _ACTIVITY_DIR / "index.json.corrupt")
        except OSError:
            pass
        return []


def _load_index() -> list[dict]:
    """The index every caller should read, legacy history folded in.

    Migration hangs off the read rather than off startup so there is no
    ordering requirement between the two — the first caller to look at the
    index, whoever that turns out to be, pays for the one-time import.
    """
    migrate_legacy_log()
    return _read_index_file()


def _save_index(entries: list[dict]) -> None:
    """Write the index atomically so a crash mid-write cannot truncate history."""
    _ACTIVITY_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _ACTIVITY_DIR / "index.json.tmp"
    with tmp.open("w", encoding="utf-8") as f:
        f.write(json.dumps(entries, indent=2))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, _INDEX_PATH)


# ---------------------------------------------------------------------------
# Output files
# ---------------------------------------------------------------------------


def _write_output(run_id: str, lines: list[str]) -> int:
    """Write one run's output. Returns the line count, or 0 if it could not.

    Every element is stringified and split on embedded newlines before
    writing, so a caller that appends a multi-line chunk as a single list
    entry still gets back the same number of lines it wrote in, and a stray
    non-string element (``None``, an exit code, ...) can't blow up the write.
    """
    try:
        flat: list[str] = []
        for line in lines:
            flat.extend(str(line).split("\n"))
        _RUNS_DIR.mkdir(parents=True, exist_ok=True)
        (_RUNS_DIR / f"{run_id}.log").write_text("\n".join(flat))
        return len(flat)
    except OSError as exc:
        log.error("Could not write activity output for run %s: %s", run_id, exc)
        return 0


def _delete_output_files(run_ids: set[str]) -> None:
    """Delete the output files for records that were just pruned.

    Provenance here is known — these ids came from index records that
    genuinely finished and then aged or counted out — so deleting them
    outright is safe. Defensive throughout: cleanup is housekeeping, and must
    never be the reason a run fails to be recorded. Callers must only invoke
    this after the new index (which no longer lists these ids) has actually
    been committed to disk — otherwise a crash between deletion and the index
    write can leave surviving records pointing at files that no longer exist.
    """
    for run_id in run_ids:
        if not run_id:
            continue
        try:
            (_RUNS_DIR / f"{run_id}.log").unlink(missing_ok=True)
        except OSError:
            pass


def _sweep_aged_orphans(keep_ids: set[str]) -> None:
    """Delete output files with no index record at all, once they're old.

    Provenance here is unknown — an orphan could be quarantine fallout, or a
    crash between the output write and the index write, and it might be the
    only surviving copy of a failed run's output. So it isn't deleted on
    sight; it's left to age out under the same MAX_AGE_DAYS retention the
    module already promises for everything else, which needs no operator to
    notice anything happened.
    """
    if not _RUNS_DIR.is_dir():
        return
    cutoff = datetime.now(timezone.utc).timestamp() - MAX_AGE_DAYS * 86400
    try:
        candidates = [p for p in _RUNS_DIR.glob("*.log") if p.stem not in keep_ids]
    except OSError:
        return
    for path in candidates:
        try:
            if path.stat().st_mtime < cutoff:
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


def _select_kept(
    entries: list[dict], protect_id: str = ""
) -> tuple[list[dict], list[dict]]:
    """Split ``entries`` into ``(kept, dropped)`` after MAX_ENTRIES/MAX_AGE_DAYS.

    Pure selection — no filesystem side effects; deleting the dropped
    records' files is a separate step the caller runs only after the
    selection has been durably saved. ``protect_id`` (the record just
    inserted by this call) is exempt from the age cutoff, so a skewed host
    clock can't produce a run id that record_run hands back only for get_run
    to immediately resolve to None.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=MAX_AGE_DAYS)
    kept, dropped = [], list(entries[MAX_ENTRIES:])
    for e in entries[:MAX_ENTRIES]:
        if e.get("id") == protect_id or _parse_ts(e.get("started_at", ""), now) >= cutoff:
            kept.append(e)
        else:
            dropped.append(e)
    return kept, dropped


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

# Credential fields whose values are secrets worth masking. ``ssh_key`` names a
# file in the keys directory rather than holding key material, so it is
# excluded — masking a filename makes the output harder to read for no security
# gain.
_SECRET_KEY_HINTS = ("password", "token", "secret", "api_key", "user_key", "passphrase")

# Below this length a "secret" is more likely to collide with ordinary words in
# the output than it is to be worth masking.
_MIN_SECRET_LEN = 8

REDACTED = "***"


def exc_text(exc: BaseException) -> str:
    """Readable text for an exception, even when it carries no message.

    httpx timeouts stringify to ``""``, which produced job errors and activity
    records naming neither the failure nor anything else — the one case this
    module exists to explain, explaining nothing. A class name is a poor
    description but an infinitely better one than the empty string.
    """
    return str(exc) or exc.__class__.__name__


def _secret_values() -> set[str]:
    """Every stored credential value worth masking, host and integration alike."""
    from .credentials import _load_store

    values: set[str] = set()
    for entry in _load_store().values():
        if not isinstance(entry, dict):
            continue
        for key, value in entry.items():
            if not isinstance(value, str) or len(value) < _MIN_SECRET_LEN:
                continue
            if any(hint in key.lower() for hint in _SECRET_KEY_HINTS):
                values.add(value)
    return values


def redact(lines: list[str]) -> list[str]:
    """Mask stored credential values in captured output.

    The same text already reaches the job modal, so this is not a new exposure
    path — but persisting it makes anything a command echoes durable, and the
    activity directory outlives the process that produced it.
    """
    try:
        secrets = _secret_values()
    except Exception:
        log.warning("Could not load credentials for redaction — storing output as-is")
        return lines
    if not secrets:
        return lines
    # Longest first, so a secret containing another is masked whole rather than
    # being chewed into gibberish by the shorter one landing first.
    ordered = sorted(secrets, key=len, reverse=True)
    out = []
    for line in lines:
        # str() for the same reason _write_output does it: a caller may append
        # an exit code or None, and losing the whole run to an AttributeError
        # here would defeat the point of recording.
        line = str(line)
        for secret in ordered:
            line = line.replace(secret, REDACTED)
        out.append(line)
    return out


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
    try:
        run_id = uuid.uuid4().hex[:8]
        finished = datetime.now(timezone.utc)
        started = _parse_ts(started_at, finished)
        with _lock:
            # Output write, index read and index write must all happen while
            # holding the lock — see its comment for why.
            line_count = _write_output(run_id, redact(list(output or [])))
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
            entries = _load_index()
            entries.insert(0, record)
            kept, dropped = _select_kept(entries, protect_id=run_id)
            _save_index(kept)
            _delete_output_files({e.get("id", "") for e in dropped})
            _sweep_aged_orphans({e.get("id", "") for e in kept})
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
