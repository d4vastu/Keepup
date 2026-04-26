"""
Security headers middleware and conditional HTTPS redirect — OP#114.

Provides two ASGI middleware classes:

  SecurityHeadersMiddleware
      Injects hardening response headers on every reply:
        - X-Content-Type-Options: nosniff    (prevents MIME-type sniffing)
        - X-Frame-Options: DENY              (blocks iframe-based clickjacking)
        - Referrer-Policy: no-referrer       (suppresses the Referer header)
        - Content-Security-Policy-Report-Only (CSP in observation mode first)
        - Strict-Transport-Security          (only when TLS cert is present)

  ConditionalHTTPSRedirectMiddleware
      Issues a 301 redirect from http:// to https:// when TLS is configured.
      Completely transparent (no-op) when TLS is not active so that plain-HTTP
      deployments are unaffected.
"""

import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.types import ASGIApp

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Content Security Policy (Report-Only — first rollout pass)
# ---------------------------------------------------------------------------

# We ship CSP in Report-Only mode so violations are logged at /api/csp-report
# without blocking any existing functionality. Once the reported violations are
# reviewed and the policy tightened, a later ticket will promote it to
# Content-Security-Policy enforcement.
#
# Rationale for each directive:
#   script-src unsafe-inline  — templates contain <script> blocks; needed until
#                               a nonce-based approach is adopted in a future pass.
#   cdn.tailwindcss.com       — Tailwind CSS CDN loaded by all page templates.
#   unpkg.com                 — htmx is loaded from unpkg CDN.
#   img-src data:             — base64-encoded SVG favicons / inline images.
#   object-src 'none'         — disallow Flash / embedded objects.
#   base-uri 'self'           — prevent injected <base> tags from hijacking URLs.
#   form-action 'self'        — disallow form submissions to external origins.
_CSP_REPORT_ONLY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://unpkg.com; "
    "style-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "report-uri /api/csp-report"
)

# HSTS: instruct browsers to require HTTPS for 1 year.
# includeSubDomains is intentionally omitted — Keepup is a self-hosted
# single-instance tool and we must not make assumptions about subdomain ownership.
_HSTS_VALUE = "max-age=31536000"


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Append security-hardening HTTP response headers to every reply.

    Pass ``tls_active=True`` (via ``app.add_middleware(..., tls_active=True)``)
    when a TLS certificate is present so that HSTS is included.
    """

    def __init__(self, app: ASGIApp, *, tls_active: bool = False) -> None:
        super().__init__(app)
        self._tls_active = tls_active

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)

        # Prevent browsers from sniffing Content-Type away from the declared value.
        response.headers["X-Content-Type-Options"] = "nosniff"

        # Deny rendering this app inside an iframe on any origin.
        response.headers["X-Frame-Options"] = "DENY"

        # Strip the Referer header on all navigations — the app has no use for it
        # and it could leak authenticated URLs to third-party CDN requests.
        response.headers["Referrer-Policy"] = "no-referrer"

        # Attach CSP in observation mode so we can audit violations before
        # switching to enforcement in a future release.
        response.headers["Content-Security-Policy-Report-Only"] = _CSP_REPORT_ONLY

        # Only set HSTS when TLS is actually active — setting it over HTTP would
        # be ignored by browsers anyway, but it avoids confusing logs.
        if self._tls_active:
            response.headers["Strict-Transport-Security"] = _HSTS_VALUE

        return response


class ConditionalHTTPSRedirectMiddleware(BaseHTTPMiddleware):
    """Redirect plain-HTTP requests to HTTPS when a TLS certificate is present.

    When *tls_active* is False this middleware is a transparent pass-through so
    that HTTP-only deployments (development, LAN without a cert) work unchanged.
    """

    def __init__(self, app: ASGIApp, *, tls_active: bool = False) -> None:
        super().__init__(app)
        self._tls_active = tls_active

    async def dispatch(self, request: Request, call_next) -> Response:
        # Only redirect when TLS is configured *and* the incoming request is
        # plain HTTP (a reverse proxy that terminates TLS may forward as HTTP,
        # but in Keepup's direct-serve model the scheme is accurate).
        if self._tls_active and request.url.scheme == "http":
            https_url = str(request.url).replace("http://", "https://", 1)
            return RedirectResponse(https_url, status_code=301)
        return await call_next(request)
