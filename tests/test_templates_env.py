"""Tests for custom Jinja2 filters in app.templates_env."""

import re

import pytest

from app.templates_env import _css_id


@pytest.mark.parametrize(
    "raw, expected",
    [
        # standalone-container ref prefix '~' is a CSS sibling combinator
        ("ssh/my-lxc/~portainer", "ssh-my-lxc--portainer"),
        # compose ref with path + tag separators
        ("ssh/my-node/mystack:app", "ssh-my-node-mystack-app"),
        # portainer backend numeric ref
        ("portainer/3:1", "portainer-3-1"),
        # URL-encoded container name (quote() emits '%')
        ("ssh/host/my%2Fstack", "ssh-host-my-2Fstack"),
        # parentheses (the CLAUDE.md special-char case)
        ("ssh/host/app(prod)", "ssh-host-app-prod-"),
        # dots
        ("ssh/192.168.5.239/app", "ssh-192-168-5-239-app"),
    ],
)
def test_css_id_strips_unsafe_chars(raw, expected):
    out = _css_id(raw)
    assert out == expected
    # result is always usable as both an id and a CSS selector
    assert re.fullmatch(r"[A-Za-z0-9_-]+", out)


def test_css_id_handles_empty_and_none():
    assert _css_id("") == ""
    assert _css_id(None) == ""


def test_css_id_preserves_already_safe():
    assert _css_id("already-safe_123") == "already-safe_123"
