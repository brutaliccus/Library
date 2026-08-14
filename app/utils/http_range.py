"""HTTP Range helpers for audio proxies.

Players (Android WebView / ExoPlayer) send Range so they never buffer a
multi-GB audiobook. If upstream ignores Range and returns 200 with the
full body, we must rewrite status/headers and slice bytes or the phone OOMs.
"""

from __future__ import annotations

import re
from typing import NamedTuple

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)", re.IGNORECASE)


class ByteRange(NamedTuple):
    """Inclusive byte range. start is None for suffix-length requests."""

    start: int | None
    end: int | None


def parse_range_header(header: str | None) -> ByteRange | None:
    if not header or not str(header).strip():
        return None
    m = _RANGE_RE.search(str(header).strip())
    if not m:
        return None
    a, b = m.group(1), m.group(2)
    if a == "" and b == "":
        return None
    if a == "":
        return ByteRange(start=None, end=int(b))
    return ByteRange(start=int(a), end=int(b) if b else None)


def resolve_range(
    rng: ByteRange, total: int | None
) -> tuple[int, int | None, int | None]:
    """Return (skip_bytes, last_inclusive_or_none, total_or_none)."""
    if rng.start is None:
        n = rng.end or 0
        if total is None or total <= 0:
            return 0, None, total
        start = max(0, total - n)
        return start, total - 1, total
    start = max(0, rng.start)
    last = rng.end
    if total is not None and total > 0:
        if start >= total:
            start = max(0, total - 1)
        if last is not None:
            last = min(last, total - 1)
    return start, last, total


def response_for_slice(
    *,
    start: int,
    last: int | None,
    total: int | None,
    content_type: str | None,
) -> tuple[int, dict[str, str]]:
    """Status + headers for a (possibly sliced) audio response."""
    headers: dict[str, str] = {
        "accept-ranges": "bytes",
        "x-accel-buffering": "no",
        "cache-control": "no-store",
    }
    if content_type:
        headers["content-type"] = content_type

    if start <= 0 and last is None:
        if total is not None and total >= 0:
            headers["content-length"] = str(total)
        return 200, headers

    end = last
    if end is None and total is not None and total > 0:
        end = total - 1
    if end is None:
        if start <= 0:
            if total is not None and total >= 0:
                headers["content-length"] = str(total)
            return 200, headers
        headers["content-range"] = f"bytes {start}-/*"
        return 206, headers

    if end < start:
        end = start
    length = end - start + 1
    headers["content-length"] = str(length)
    total_s = str(total) if total is not None and total > 0 else "*"
    headers["content-range"] = f"bytes {start}-{end}/{total_s}"
    return 206, headers
