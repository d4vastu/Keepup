"""Central httpx client factory with standardised timeouts and per-host circuit breakers."""

from contextlib import asynccontextmanager
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


class _BreakerClient:
    """Wraps an httpx.AsyncClient so every HTTP call flows through a circuit breaker."""

    def __init__(self, client: httpx.AsyncClient, breaker: aiobreaker.CircuitBreaker):
        self._client = client
        self._breaker = breaker

    async def get(self, *args, **kwargs):
        return await self._breaker.call_async(self._client.get, *args, **kwargs)

    async def post(self, *args, **kwargs):
        return await self._breaker.call_async(self._client.post, *args, **kwargs)

    async def put(self, *args, **kwargs):
        return await self._breaker.call_async(self._client.put, *args, **kwargs)

    async def delete(self, *args, **kwargs):
        return await self._breaker.call_async(self._client.delete, *args, **kwargs)

    async def head(self, *args, **kwargs):
        return await self._breaker.call_async(self._client.head, *args, **kwargs)


@asynccontextmanager
async def make_breaker_client(
    *,
    base_url: str,
    verify: bool | str = True,
    headers: dict | None = None,
    timeout: httpx.Timeout | None = None,
):
    """Factory for integration clients — identical to make_client but wraps every
    request through a per-host circuit breaker (fail_max=5, reset=60s)."""
    host = _host_from_url(base_url)
    breaker = get_breaker(host)
    async with make_client(
        base_url=base_url, verify=verify, headers=headers, timeout=timeout
    ) as client:
        yield _BreakerClient(client, breaker)


def _host_from_url(url: str) -> str:
    return urlparse(url).hostname or url
