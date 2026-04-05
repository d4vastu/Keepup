"""In-memory log buffer — captures uvicorn access + app log records."""
import logging
from collections import deque
from datetime import datetime, timezone

_MAX_LINES = 500
_buffer: deque = deque(maxlen=_MAX_LINES)

_LEVEL_CLASS = {
    "DEBUG": "text-slate-500",
    "INFO": "text-slate-300",
    "WARNING": "text-amber-400",
    "ERROR": "text-red-400",
    "CRITICAL": "text-red-300",
    "ACCESS": "text-slate-400",
}


class _BufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            ts = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%H:%M:%S")
            level = record.levelname
            msg = self.format(record)
            _buffer.append({"ts": ts, "level": level, "msg": msg, "css": _LEVEL_CLASS.get(level, "text-slate-300")})
        except Exception:
            pass


def setup_log_buffer() -> None:
    """Attach the buffer handler to uvicorn and root loggers."""
    handler = _BufferHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))

    for name in ("uvicorn.access", "uvicorn.error", "uvicorn", ""):
        logger = logging.getLogger(name)
        logger.addHandler(handler)


def get_log_lines(limit: int = 200) -> list[dict]:
    """Return the most recent `limit` log lines (oldest first)."""
    lines = list(_buffer)
    return lines[-limit:]
