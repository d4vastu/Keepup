"""Tests for app/pushover.py — Pushover push notification sender."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_send_pushover_returns_false_when_no_creds(data_dir, monkeypatch):
    """Missing token/user_key → returns False without making any HTTP call."""
    from app.pushover import send_pushover

    with patch("app.pushover.get_integration_credentials", return_value={}):
        result = await send_pushover("title", "msg")
    assert result is False


@pytest.mark.asyncio
async def test_send_pushover_returns_true_on_success(data_dir, monkeypatch):
    """Valid creds + 200 response → returns True."""
    from app.pushover import send_pushover

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_resp)

    with (
        patch(
            "app.pushover.get_integration_credentials",
            return_value={"api_token": "tok", "user_key": "usr"},
        ),
        patch("app.pushover.httpx.AsyncClient", return_value=mock_client),
    ):
        result = await send_pushover("title", "msg")

    assert result is True


@pytest.mark.asyncio
async def test_send_pushover_returns_false_on_http_error(data_dir, monkeypatch):
    """Network exception → returns False."""
    from app.pushover import send_pushover

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(side_effect=Exception("connection refused"))

    with (
        patch(
            "app.pushover.get_integration_credentials",
            return_value={"api_token": "tok", "user_key": "usr"},
        ),
        patch("app.pushover.httpx.AsyncClient", return_value=mock_client),
    ):
        result = await send_pushover("title", "msg")

    assert result is False


@pytest.mark.asyncio
async def test_send_pushover_returns_false_on_non_200(data_dir, monkeypatch):
    """Non-200 response → returns False."""
    from app.pushover import send_pushover

    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_resp)

    with (
        patch(
            "app.pushover.get_integration_credentials",
            return_value={"api_token": "tok", "user_key": "usr"},
        ),
        patch("app.pushover.httpx.AsyncClient", return_value=mock_client),
    ):
        result = await send_pushover("title", "msg")

    assert result is False
