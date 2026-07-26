"""Global M4B encode queue — concurrency 1 across requests."""

from __future__ import annotations

import asyncio

import pytest

from app.services import m4b_queue


@pytest.fixture(autouse=True)
def _clean_queue():
    m4b_queue.reset_m4b_queue_for_tests()
    yield
    m4b_queue.reset_m4b_queue_for_tests()


def test_format_queue_detail():
    assert "request #9 converting" in m4b_queue.format_queue_detail(2, 9)
    assert m4b_queue.format_queue_detail(1, None).startswith("Waiting in M4B queue (#1)")


def test_serializes_two_encodes():
    order: list[str] = []

    async def _run():
        started = asyncio.Event()
        release_first = asyncio.Event()

        async def first() -> None:
            async with m4b_queue.m4b_encode_slot(1, poll_seconds=0.05):
                order.append("1-enter")
                started.set()
                await release_first.wait()
                order.append("1-exit")

        async def second() -> None:
            await started.wait()
            async with m4b_queue.m4b_encode_slot(2, poll_seconds=0.05):
                order.append("2-enter")
                order.append("2-exit")

        t1 = asyncio.create_task(first())
        t2 = asyncio.create_task(second())
        await started.wait()
        # Second must be waiting while first holds the slot.
        await asyncio.sleep(0.1)
        assert "2-enter" not in order
        snap = m4b_queue.queue_snapshot()
        assert snap["active_request_id"] == 1
        assert 2 in snap["waiting_request_ids"]
        release_first.set()
        await asyncio.gather(t1, t2)
        assert order == ["1-enter", "1-exit", "2-enter", "2-exit"]

    asyncio.run(_run())


def test_on_queued_reports_position():
    events: list[tuple[int, int, int | None]] = []

    async def _run():
        release_first = asyncio.Event()
        second_waiting = asyncio.Event()

        async def on_queued(req_id: int, pos: int, active: int | None) -> None:
            events.append((req_id, pos, active))
            if req_id == 2:
                second_waiting.set()

        async def first() -> None:
            async with m4b_queue.m4b_encode_slot(1, poll_seconds=0.05):
                await release_first.wait()

        async def second() -> None:
            async with m4b_queue.m4b_encode_slot(
                2,
                on_queued=lambda pos, active: on_queued(2, pos, active),
                poll_seconds=0.05,
            ):
                pass

        t1 = asyncio.create_task(first())
        await asyncio.sleep(0.05)
        t2 = asyncio.create_task(second())
        await asyncio.wait_for(second_waiting.wait(), timeout=2.0)
        assert any(e[0] == 2 and e[1] == 1 and e[2] == 1 for e in events)
        release_first.set()
        await asyncio.gather(t1, t2)

    asyncio.run(_run())


def test_reentrant_same_request():
    async def _run():
        async with m4b_queue.m4b_encode_slot(7, poll_seconds=0.05):
            assert m4b_queue.queue_snapshot()["active_request_id"] == 7
            async with m4b_queue.m4b_encode_slot(7, poll_seconds=0.05):
                assert m4b_queue.queue_snapshot()["active_request_id"] == 7
        assert m4b_queue.queue_snapshot()["active_request_id"] is None

    asyncio.run(_run())


def test_waiter_removed_on_cancel_before_acquire():
    async def _run():
        release_first = asyncio.Event()

        async def first() -> None:
            async with m4b_queue.m4b_encode_slot(1, poll_seconds=0.05):
                await release_first.wait()

        async def second() -> None:
            async with m4b_queue.m4b_encode_slot(2, poll_seconds=0.05):
                pass

        t1 = asyncio.create_task(first())
        await asyncio.sleep(0.05)
        t2 = asyncio.create_task(second())
        await asyncio.sleep(0.1)
        assert 2 in m4b_queue.queue_snapshot()["waiting_request_ids"]
        t2.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t2
        assert 2 not in m4b_queue.queue_snapshot()["waiting_request_ids"]
        release_first.set()
        await t1
        assert m4b_queue.queue_snapshot()["active_request_id"] is None

    asyncio.run(_run())
