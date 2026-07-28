"""Deduplication store for container image update notifications.

Tracks which stacks have already triggered a notification so we don't
spam on every docker check. Clears entries when the stack goes back
to up-to-date (i.e. after an update is applied).

Also tracks how long each container has been stuck in the "unknown" state so a
persistently-failing update check raises one rollup warning instead of being
mistaken for "no update available" (OP#217).
"""

import json
import os
import threading
import time
from collections import Counter
from pathlib import Path

_DATA_DIR = Path(os.getenv("DATA_PATH", "/app/data"))
_PATH = _DATA_DIR / "notified_updates.json"
_lock = threading.Lock()

# How long a container must stay unknown before it is worth a push (OP#217).
# A Docker Hub rate-limit flips many containers to unknown at once and usually
# clears on the next cycle, so anything shorter is noise.
UNKNOWN_STALE_SECONDS = 86400


def _empty_state() -> dict:
    return {"notified": [], "unknown_since": {}, "unknown_notified": []}


def _load() -> dict:
    """Load the store, migrating the pre-OP#217 bare-list format on read."""
    if not _PATH.exists():
        return _empty_state()
    try:
        data = json.loads(_PATH.read_text())
    except Exception:
        return _empty_state()

    if isinstance(data, list):
        return {"notified": list(data), "unknown_since": {}, "unknown_notified": []}
    if not isinstance(data, dict):
        return _empty_state()

    return {
        "notified": list(data.get("notified") or []),
        "unknown_since": dict(data.get("unknown_since") or {}),
        "unknown_notified": list(data.get("unknown_notified") or []),
    }


def _save(state: dict) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(
        json.dumps(
            {
                "notified": sorted(state["notified"]),
                "unknown_since": state["unknown_since"],
                "unknown_notified": sorted(state["unknown_notified"]),
            },
            indent=2,
        )
    )


def _notify_stale_unknowns(stale: set[str], stacks: list[dict]) -> None:
    """Fire one rollup notification naming the long-unknown containers."""
    from .notifications import notify
    from .registry_client import reason_label

    by_path = {s.get("update_path", ""): s for s in stacks}
    names = sorted((by_path.get(p, {}).get("name") or p) for p in stale)
    reasons = [
        by_path.get(p, {}).get("unknown_reason")
        for p in stale
        if by_path.get(p, {}).get("unknown_reason")
    ]
    hours = UNKNOWN_STALE_SECONDS // 3600
    plural = len(names) != 1
    message = (
        f"{len(names)} container{'s' if plural else ''} "
        f"ha{'ve' if plural else 's'} not been checked for over "
        f"{hours}h: {', '.join(names)}."
    )
    if reasons:
        dominant = Counter(reasons).most_common(1)[0][0]
        message += f" Most common reason: {reason_label(dominant).lower()}."

    notify("Container update checks failing", message, level="warning")


def check_and_notify(stacks: list[dict]) -> None:
    """Given a list of stack dicts with update_path and update_status, fire
    notifications for newly-available updates and for containers whose update
    check has been failing long enough to matter, and clear stale entries."""
    from .notifications import notify

    with _lock:
        state = _load()
        notified = set(state["notified"])
        unknown_since = state["unknown_since"]
        changed = False
        now = time.time()
        seen_unknown: set[str] = set()

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

            if status == "unknown" and path:
                seen_unknown.add(path)
                if path not in unknown_since:
                    unknown_since[path] = now
                    changed = True

        # Drop unknown tracking for containers that recovered or disappeared.
        for path in list(unknown_since):
            if path not in seen_unknown:
                del unknown_since[path]
                changed = True

        stale = {
            path
            for path, first_seen in unknown_since.items()
            if now - first_seen >= UNKNOWN_STALE_SECONDS
        }
        already = set(state["unknown_notified"])
        if stale - already:
            _notify_stale_unknowns(stale, stacks)
        if stale != already:
            state["unknown_notified"] = sorted(stale)
            changed = True

        if changed:
            state["notified"] = sorted(notified)
            _save(state)
