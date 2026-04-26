"""
CSRF protection for state-changing HTMX requests.

All HTMX requests (identified by the HX-Request header) that use a state-changing
HTTP method must carry an X-CSRF-Token header whose value matches the token stored
in the user's session.  Traditional form POSTs (no HX-Request header) are protected
by the SameSite=strict session cookie policy instead.

Token provisioning: every request ensures request.session["_csrf_token"] is set so
templates can embed it via {{ request.session.get("_csrf_token", "") }}.
"""

import logging
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse

log = logging.getLogger(__name__)

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# Setup routes are pre-authentication; cross-site attacks on them cannot leverage
# a victim's session credentials, so they are exempt from token validation.
_EXEMPT_PREFIXES = ("/setup",)


def get_csrf_token(session: dict) -> str:
    """Return the session CSRF token, generating one if absent."""
    if "_csrf_token" not in session:
        session["_csrf_token"] = secrets.token_hex(32)
    return session["_csrf_token"]


class CSRFMiddleware(BaseHTTPMiddleware):
    """
    Enforces CSRF protection on HTMX state-changing requests.

    Checks the X-CSRF-Token request header against the per-session token for every
    HTMX POST/PUT/DELETE/PATCH that does not target an exempt path prefix.

    Non-HTMX form POSTs are implicitly protected by the SameSite=strict attribute
    set on the session cookie in main.py.
    """

    async def dispatch(self, request: Request, call_next):
        # Provision the token on every request so templates always have it available.
        get_csrf_token(request.session)

        if (
            request.method not in _SAFE_METHODS
            and request.headers.get("HX-Request")
            and not any(request.url.path.startswith(p) for p in _EXEMPT_PREFIXES)
        ):
            if not _validate(request):
                remote = (
                    request.headers.get("X-Forwarded-For")
                    or getattr(request.client, "host", "unknown")
                )
                log.warning(
                    "CSRF validation failed: %s %s from %s",
                    request.method,
                    request.url.path,
                    remote,
                )
                return HTMLResponse("Forbidden", status_code=403)

        return await call_next(request)


def _validate(request: Request) -> bool:
    """Return True when the submitted CSRF token matches the session token."""
    session_token = request.session.get("_csrf_token", "")
    if not session_token:
        return False
    submitted = request.headers.get("X-CSRF-Token", "")
    if not submitted:
        return False
    # Use constant-time comparison to prevent timing attacks.
    return secrets.compare_digest(submitted, session_token)
