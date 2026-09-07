"""Pushover push notification sender.

Every failure here is logged. The function still returns a bool for the Admin
"Test" button, but no other caller inspects it, so a silent False was the same
thing as a delivered push from the outside (OP#238).
"""

import logging

import httpx

from .activity_log import exc_text
from .credentials import get_integration_credentials
from .httpx_client import make_client

logger = logging.getLogger(__name__)

PUSHOVER_API = "https://api.pushover.net/1/messages.json"

# Explicit rather than inherited from make_client(). A push dropped on a
# borrowed timeout looks exactly like one that was never sent — the shape of
# defect OP#228 was.
PUSHOVER_TIMEOUT = httpx.Timeout(connect=5, read=15, write=15, pool=30)


async def send_pushover(title: str, message: str) -> bool:
    """Post one push. Pure transport — the `pushover.enabled` flag is checked by
    the event-dispatch path in `notifications`, so the Test button keeps working
    while notifications are switched off."""
    creds = get_integration_credentials("pushover")
    token = creds.get("api_token", "")
    user_key = creds.get("user_key", "")
    if not token or not user_key:
        logger.warning("Pushover push not sent: no API token or user key stored.")
        return False
    try:
        async with make_client(timeout=PUSHOVER_TIMEOUT) as client:
            resp = await client.post(
                PUSHOVER_API,
                data={
                    "token": token,
                    "user": user_key,
                    "title": title,
                    "message": message,
                },
            )
            if resp.status_code != 200:
                body = str(getattr(resp, "text", "") or "").strip()[:300]
                logger.warning(
                    "Pushover rejected the push: HTTP %s — %s",
                    resp.status_code,
                    body or "(empty response body)",
                )
                return False
            return True
    except Exception as e:
        logger.warning("Pushover push failed: %s", exc_text(e))
        return False
