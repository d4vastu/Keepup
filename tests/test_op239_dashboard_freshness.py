"""OP#239 — the dashboard says when it last actually checked.

`#banner-time` already existed, hidden on load and hardcoded by JS to the string
"just now" once the page's own checks settle. Left alone it would contradict the
section headers on a freshly opened page, so it renders the stored time on first
paint and the existing JS still overwrites it afterwards.
"""

from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(autouse=True)
def store(data_dir, monkeypatch):
    import app.last_check as lc

    monkeypatch.setattr(lc, "_PATH", data_dir / "last_check.json")
    return lc


def _ago(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


def test_a_never_checked_dashboard_says_never(client):
    response = client.get("/home")

    assert response.status_code == 200
    assert "never checked" in response.text


def test_the_os_section_reports_the_oldest_host(client, store):
    store._save({"hosts": {"test-host": _ago(hours=9)}, "containers": None})

    response = client.get("/home")

    assert "9h ago" in response.text


def test_the_banner_shows_the_stored_time_instead_of_hiding(client, store):
    """Otherwise the banner reads "just now" beside a header reading "9h ago"."""
    store._save({"hosts": {"test-host": _ago(minutes=14)}, "containers": _ago(minutes=14)})

    response = client.get("/home")

    banner = response.text.split('id="banner-time"')[1].split(">")[0]
    assert "hidden" not in banner
    assert "14m ago" in response.text
