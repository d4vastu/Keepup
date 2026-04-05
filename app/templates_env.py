"""Shared Jinja2 templates factory with custom filters registered."""

from datetime import datetime, timezone
from pathlib import Path

from fastapi.templating import Jinja2Templates

_TEMPLATES_DIR = Path(__file__).parent / "templates"


def _as_local(dt_str: str) -> str:
    """Convert a UTC ISO datetime string to the configured display timezone."""
    try:
        from zoneinfo import ZoneInfo
        from .config_manager import get_timezone

        tz_name = get_timezone()
        dt = datetime.fromisoformat(dt_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local_dt = dt.astimezone(ZoneInfo(tz_name))
        tz_abbr = local_dt.strftime("%Z")
        return local_dt.strftime(f"%Y-%m-%d %H:%M {tz_abbr}")
    except Exception:
        return dt_str[:16].replace("T", " ") + " UTC"


def make_templates() -> Jinja2Templates:
    t = Jinja2Templates(directory=_TEMPLATES_DIR)
    t.env.filters["as_local"] = _as_local
    return t
