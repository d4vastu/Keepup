"""Shared Jinja2 templates factory with custom filters registered."""

import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi.templating import Jinja2Templates

_TEMPLATES_DIR = Path(__file__).parent / "templates"

_CSS_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


def _css_id(value: str) -> str:
    """Make a string safe to use as an HTML ``id`` and matching CSS selector.

    Any character outside ``[A-Za-z0-9_-]`` is replaced with ``-``. This covers
    the ``~`` standalone-container ref prefix (a CSS sibling combinator), ``/``
    and ``:`` path separators, and ``%`` from URL-encoded container names. The
    same filter feeds both the ``id`` attribute and the ``hx-target`` selector,
    so they always match; without it a ``~`` would break the selector and htmx
    raises ``htmx:targetError``.
    """
    return _CSS_UNSAFE.sub("-", value or "")


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
    t.env.filters["css_id"] = _css_id
    return t
