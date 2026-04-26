"""Append-only JSON audit log for sensitive operations (OP#117)."""

import json
import logging
import logging.handlers
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Request

_DATA_DIR = Path(os.getenv("DATA_PATH", "/app/data"))

_audit_log = logging.getLogger("app.audit")
_audit_log.setLevel(logging.INFO)
_audit_log.propagate = False


def setup_audit_log(data_dir: Path | None = None) -> None:
    """Configure the rotating file handler. Call once at startup (or in tests)."""
    target_dir = data_dir if data_dir is not None else _DATA_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    for h in list(_audit_log.handlers):
        _audit_log.removeHandler(h)
        h.close()
    handler = logging.handlers.RotatingFileHandler(
        target_dir / "audit.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    _audit_log.addHandler(handler)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def audit(
    request: Request,
    action: str,
    target: str = "",
    result: str = "ok",
    details: dict[str, Any] | None = None,
    actor: str | None = None,
) -> None:
    """Append one JSON-per-line audit entry. Secrets must never appear in details."""
    if actor is None:
        from .auth import get_admin_username
        actor = get_admin_username() or "unknown"
    request_id = getattr(request.state, "request_id", None) or str(uuid.uuid4())
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "request_id": request_id,
        "actor": actor,
        "source_ip": _client_ip(request),
        "action": action,
        "target": target,
        "result": result,
        "details": details or {},
    }
    _audit_log.info(json.dumps(entry, default=str))
