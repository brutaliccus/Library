"""Pending-actions aggregator for Admin Health."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from app.services import pending_actions


def test_collect_pending_actions_shape():
    async def _run():
        with (
            patch.object(pending_actions, "_count_quarantined", new_callable=AsyncMock) as cq,
            patch.object(pending_actions, "_unprocessed_total", new_callable=AsyncMock) as ut,
        ):
            cq.side_effect = [3, 2, 1]  # downloads, audiobook sweep, ebook sweep
            ut.side_effect = [4, 5]  # audiobook / ebook unprocessed
            out = await pending_actions.collect_pending_actions()

        assert out["total"] == 3 + 2 + 1 + 4 + 5
        ids = [i["id"] for i in out["items"]]
        assert ids == [
            "quarantined_downloads",
            "audiobook_sweep_review",
            "ebook_sweep_review",
            "audiobook_sweep_unprocessed",
            "ebook_sweep_unprocessed",
        ]
        by_id = {i["id"]: i for i in out["items"]}
        assert by_id["quarantined_downloads"]["href"] == "/admin?tab=requests"
        assert "sweep=audiobook" in by_id["audiobook_sweep_review"]["href"]
        assert "queue=needs-review" in by_id["audiobook_sweep_review"]["href"]
        assert "queue=unprocessed" in by_id["ebook_sweep_unprocessed"]["href"]
        assert by_id["ebook_sweep_review"]["count"] == 1

    asyncio.run(_run())
