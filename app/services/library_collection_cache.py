"""Short-TTL cache for assembled ABS/Kavita My Library collection payloads.

Invalidated whenever ABS/Kavita item caches are cleared (scan, delete, rematch).
Keeps soft-poll refetches cheap after the first cold rebuild.
"""

from __future__ import annotations

import time
from typing import Any

_CACHE: dict[str, tuple[float, Any]] = {}
_TTL_SECONDS = 45.0


def get(key: str) -> Any | None:
    hit = _CACHE.get(key)
    if not hit:
        return None
    ts, data = hit
    if time.time() - ts >= _TTL_SECONDS:
        # Keep the expired entry: it is served as a stale fallback while a
        # library scan is running (rebuilding mid-scan snapshots partial data).
        return None
    return data


def get_stale(key: str) -> Any | None:
    """Return the last assembled payload even if the TTL has expired."""
    hit = _CACHE.get(key)
    return hit[1] if hit else None


def set(key: str, data: Any) -> None:
    _CACHE[key] = (time.time(), data)


def invalidate() -> None:
    """Expire every entry so the next get() misses, but keep the payloads.

    The expired payloads back get_stale(), which serves the last assembled
    collection while a library scan is running instead of letting a rebuild
    snapshot Kavita/ABS mid-scan (partial series made books vanish).
    """
    for key, (_, data) in list(_CACHE.items()):
        _CACHE[key] = (0.0, data)
