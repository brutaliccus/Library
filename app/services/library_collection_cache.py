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
        _CACHE.pop(key, None)
        return None
    return data


def set(key: str, data: Any) -> None:
    _CACHE[key] = (time.time(), data)


def invalidate() -> None:
    _CACHE.clear()
