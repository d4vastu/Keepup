"""Pushover push notification sender."""

from .credentials import get_integration_credentials
from .httpx_client import make_client

PUSHOVER_API = "https://api.pushover.net/1/messages.json"


async def send_pushover(title: str, message: str) -> bool:
    creds = get_integration_credentials("pushover")
    token = creds.get("api_token", "")
    user_key = creds.get("user_key", "")
    if not token or not user_key:
        return False
    try:
        async with make_client() as client:
            resp = await client.post(
                PUSHOVER_API,
                data={
                    "token": token,
                    "user": user_key,
                    "title": title,
                    "message": message,
                },
            )
            return resp.status_code == 200
    except Exception:
        return False
