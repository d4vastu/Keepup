"""In-memory TTL cache tracking when `apt-get update` was last run per host.

Used to skip redundant refreshes on frequent dashboard loads. Cache is
process-local — restart clears it, so the first check after restart always
refreshes (safe default).
"""

import time


_last_refresh: dict[str, float] = {}


def is_cache_fresh(key: str, ttl_minutes: int) -> bool:
    """Return True if a refresh for `key` is still within the TTL window."""
    if ttl_minutes <= 0:
        return False
    last = _last_refresh.get(key)
    if last is None:
        return False
    return (time.time() - last) < (ttl_minutes * 60)


def mark_refreshed(key: str) -> None:
    _last_refresh[key] = time.time()


def clear() -> None:
    _last_refresh.clear()
