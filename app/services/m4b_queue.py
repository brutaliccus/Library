"""Global M4B encode queue — at most one LibraForge ``POST /api/m4b/runs`` at a time.

The Pi can only encode one m4b comfortably. Automated forge, Quick Review, continue-forge,
and startup resume all share this slot so concurrent requests wait instead of slamming Pi.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

logger = logging.getLogger(__name__)

OnQueued = Callable[[int, int | None], Awaitable[None]]
"""Callback ``(queue_position_1_based, active_request_id)`` while waiting."""


class _M4BQueueState:
    def __init__(self) -> None:
        self.waiters: list[int] = []
        self.active_request_id: int | None = None
        self._lock: asyncio.Lock | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def lock(self) -> asyncio.Lock:
        """Return a Lock bound to the current event loop (recreate across test loops)."""
        loop = asyncio.get_running_loop()
        if self._lock is None or self._loop is not loop:
            if self._lock is not None and self._lock.locked():
                raise RuntimeError("M4B queue lock still held across event-loop change")
            self._lock = asyncio.Lock()
            self._loop = loop
            self.waiters.clear()
            self.active_request_id = None
        return self._lock


_state = _M4BQueueState()


def queue_snapshot() -> dict[str, Any]:
    """Introspection for tests / diagnostics."""
    locked = bool(_state._lock and _state._lock.locked())
    return {
        "active_request_id": _state.active_request_id,
        "waiting_request_ids": list(_state.waiters),
        "waiting": len(_state.waiters),
        "busy": _state.active_request_id is not None or locked,
    }


def reset_m4b_queue_for_tests() -> None:
    """Clear queue state between unit tests (must not call while slots are held)."""
    if _state._lock is not None and _state._lock.locked():
        raise RuntimeError("cannot reset M4B queue while a slot is held")
    _state.waiters.clear()
    _state.active_request_id = None
    _state._lock = None
    _state._loop = None


def _position_of(request_id: int) -> int:
    """1-based position among waiters (1 = next to run)."""
    try:
        return _state.waiters.index(request_id) + 1
    except ValueError:
        return 1


@asynccontextmanager
async def m4b_encode_slot(
    request_id: int,
    *,
    on_queued: OnQueued | None = None,
    poll_seconds: float = 5.0,
) -> AsyncIterator[None]:
    """Acquire the global M4B encode slot (concurrency 1).

    Re-entrant for the same ``request_id`` (nested acquire yields immediately) so a
    multi-folder convert under one request does not deadlock.
    """
    if _state.active_request_id == request_id:
        yield
        return

    lock = _state.lock()
    _state.waiters.append(request_id)
    acquired = False
    acquire_task = asyncio.create_task(lock.acquire())
    try:
        if on_queued is not None and (
            _state.active_request_id is not None or lock.locked()
        ):
            await on_queued(_position_of(request_id), _state.active_request_id)

        while not acquire_task.done():
            if on_queued is not None:
                await on_queued(_position_of(request_id), _state.active_request_id)
            await asyncio.wait({acquire_task}, timeout=max(0.05, poll_seconds))

        await acquire_task
        acquired = True

        if request_id in _state.waiters:
            _state.waiters.remove(request_id)
        _state.active_request_id = request_id
        logger.info(
            "M4B encode slot acquired by request %s (waiting=%s)",
            request_id,
            len(_state.waiters),
        )
        yield
    finally:
        if not acquired:
            if not acquire_task.done():
                acquire_task.cancel()
                try:
                    await acquire_task
                except (asyncio.CancelledError, Exception):
                    pass
            elif not acquire_task.cancelled() and acquire_task.exception() is None:
                # Acquired in a cancel race — still own the lock.
                acquired = True
        if request_id in _state.waiters:
            _state.waiters.remove(request_id)
        if acquired:
            if _state.active_request_id == request_id:
                _state.active_request_id = None
            lock.release()
            logger.info(
                "M4B encode slot released by request %s (waiting=%s)",
                request_id,
                len(_state.waiters),
            )


def format_queue_detail(position: int, active_request_id: int | None) -> str:
    """Human status_detail while waiting for the encode slot."""
    if active_request_id is not None:
        return (
            f"Waiting in M4B queue (#{position}; "
            f"request #{active_request_id} converting)…"
        )[:400]
    return f"Waiting in M4B queue (#{position})…"[:400]
