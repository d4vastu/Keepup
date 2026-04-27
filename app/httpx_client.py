"""Central httpx client factory with standardised timeouts and per-host circuit breakers."""

from datetime import timedelta
from urllib.parse import urlparse

import aiobreaker
import httpx

_DEFAULT_TIMEOUT = httpx.Timeout(connect=5, read=15, write=15, pool=30)

_breakers: dict[str, aiobreaker.CircuitBreaker] = {}


def get_breaker(host: str) -> aiobreaker.CircuitBreaker:
    if host not in _breakers:
        _breakers[host] = aiobreaker.CircuitBreaker(
            fail_max=5,
            timeout_duration=timedelta(seconds=60),
            name=host,
        )
    return _breakers[host]


def make_client(
    *,
    verify: bool | str = True,
    base_url: str = "",
    headers: dict | None = None,
    timeout: httpx.Timeout | None = None,
    follow_redirects: bool = False,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base_url,
        headers=headers or {},
        verify=verify,
        timeout=timeout or _DEFAULT_TIMEOUT,
        follow_redirects=follow_redirects,
    )


def _host_from_url(url: str) -> str:
    return urlparse(url).hostname or url
