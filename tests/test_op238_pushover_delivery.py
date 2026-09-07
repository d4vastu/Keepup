"""OP#238 — Pushover delivery must be observable, reachable and gated.

Four defects, all on the path between ``notify()`` and the Pushover API:

1. every failure was swallowed silently — ``send_pushover`` returned False and
   nothing was logged, so a rejected token looked exactly like a delivered push;
2. dispatch used ``asyncio.ensure_future``, which needs a running event loop, so
   a call from a worker thread wrote the in-app notification and sent nothing;
3. the task was never referenced and could be collected mid-request;
4. ``pushover.enabled`` was never consulted, so the Admin checkbox did nothing.

The message-less exception case exists because of OP#228: ``status == "error"``
passing while the reported text is empty is how that bug stayed invisible.
"""

import asyncio
import logging
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


def _fake_client(*, status_code: int = 200, post_side_effect=None):
    """An async context-manager standing in for make_client()'s return value."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = "invalid token"
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    if post_side_effect is not None:
        client.post = AsyncMock(side_effect=post_side_effect)
    else:
        client.post = AsyncMock(return_value=resp)
    return client


def _creds():
    return patch(
        "app.pushover.get_integration_credentials",
        return_value={"api_token": "tok", "user_key": "usr"},
    )


# ---------------------------------------------------------------------------
# 1. Failures are logged with a reason (app/pushover.py)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rejected_push_logs_the_status_and_body(data_dir, caplog):
    """A non-200 from Pushover names the status and what the API said."""
    from app.pushover import send_pushover

    caplog.set_level(logging.WARNING)
    with _creds(), patch(
        "app.pushover.make_client", return_value=_fake_client(status_code=400)
    ):
        result = await send_pushover("title", "msg")

    assert result is False
    assert "400" in caplog.text
    assert "invalid token" in caplog.text


@pytest.mark.asyncio
async def test_message_less_failure_still_names_the_cause(data_dir, caplog):
    """A timeout stringifies to "" — the log must still say what broke."""
    from app.pushover import send_pushover

    caplog.set_level(logging.WARNING)
    with _creds(), patch(
        "app.pushover.make_client",
        return_value=_fake_client(post_side_effect=httpx.ReadTimeout("")),
    ):
        result = await send_pushover("title", "msg")

    assert result is False
    assert "ReadTimeout" in caplog.text


@pytest.mark.asyncio
async def test_send_pushover_passes_its_own_timeout(data_dir):
    """The push carries an explicit timeout rather than inheriting the default."""
    import app.pushover as p

    with _creds(), patch("app.pushover.make_client") as mk:
        mk.return_value = _fake_client()
        await p.send_pushover("title", "msg")

    timeout = mk.call_args.kwargs.get("timeout")
    assert timeout is not None, "make_client() was called without an explicit timeout"
    assert timeout.read >= 10


# ---------------------------------------------------------------------------
# 2. Dispatch reaches Pushover from a thread with no event loop
# ---------------------------------------------------------------------------


def test_push_is_sent_when_no_event_loop_is_running(data_dir, monkeypatch):
    """notify() from a plain worker thread must still deliver the push."""
    import app.notifications as n

    sent = threading.Event()

    async def fake_send(title, message):
        sent.set()
        return True

    monkeypatch.setattr(
        n, "get_pushover_config", lambda: {"enabled": True}, raising=False
    )
    with patch("app.pushover.send_pushover", new=fake_send):
        n.notify("Update failed", "apt exploded")
        # Inside the patch: the push runs on its own thread, and letting the
        # patch lapse first would race it back onto the real sender.
        assert sent.wait(5), "no push was attempted without a running event loop"

    assert n.get_unread_count() == 1


@pytest.mark.asyncio
async def test_dispatched_task_is_referenced_until_it_finishes(data_dir, monkeypatch):
    """On the event loop the task is held, so it cannot be collected mid-flight."""
    import app.notifications as n

    release = asyncio.Event()

    async def fake_send(title, message):
        await release.wait()
        return True

    monkeypatch.setattr(
        n, "get_pushover_config", lambda: {"enabled": True}, raising=False
    )
    with patch("app.pushover.send_pushover", new=fake_send):
        n.notify("Update failed", "apt exploded")
        await asyncio.sleep(0)
        assert n._pending, "dispatched task was not referenced anywhere"

        release.set()
        for _ in range(100):
            await asyncio.sleep(0.01)
            if not n._pending:
                break

    assert not n._pending, "completed task was never released"


def test_failed_dispatch_is_logged_and_never_raises(data_dir, monkeypatch, caplog):
    """A push that blows up leaves the in-app entry intact and says why."""
    import app.notifications as n

    caplog.set_level(logging.WARNING)
    failed = threading.Event()

    async def fake_send(title, message):
        try:
            raise TimeoutError()
        finally:
            failed.set()

    monkeypatch.setattr(
        n, "get_pushover_config", lambda: {"enabled": True}, raising=False
    )
    with patch("app.pushover.send_pushover", new=fake_send):
        n.notify("Update failed", "apt exploded")
        assert failed.wait(5)

    for _ in range(50):
        if "TimeoutError" in caplog.text:
            break
        threading.Event().wait(0.02)

    assert n.get_unread_count() == 1
    assert "TimeoutError" in caplog.text


# ---------------------------------------------------------------------------
# 3. The enabled flag gates events, not the transport
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_pushover_sends_no_push(data_dir, monkeypatch):
    """Unchecking "Enable Pushover notifications" actually stops the pushes.

    Runs on the event loop deliberately: off-loop this would pass today for the
    wrong reason, since ``ensure_future`` raises before reaching any gate.
    """
    import app.notifications as n

    sent = asyncio.Event()

    async def fake_send(title, message):
        sent.set()
        return True

    monkeypatch.setattr(
        n, "get_pushover_config", lambda: {"enabled": False}, raising=False
    )
    with patch("app.pushover.send_pushover", new=fake_send):
        n.notify("Update failed", "apt exploded")
        for _ in range(10):
            await asyncio.sleep(0.01)

    assert not sent.is_set(), "a push was sent while Pushover was disabled"
    assert n.get_unread_count() == 1, "the in-app notification must still be written"


@pytest.mark.asyncio
async def test_test_button_sends_while_notifications_are_disabled(data_dir, monkeypatch):
    """send_pushover stays a pure transport — you test before you enable."""
    import app.config_manager as cm
    import app.pushover as p

    monkeypatch.setattr(cm, "get_pushover_config", lambda: {"enabled": False})

    client = _fake_client()
    with _creds(), patch("app.pushover.make_client", return_value=client):
        result = await p.send_pushover("Test", "Keepup test notification")

    assert result is True
    assert client.post.await_count == 1
