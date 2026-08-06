"""Aggregated admin pending-action counts for the Health dashboard."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, or_, select

from app.database import async_session
from app.models import DownloadRequest
from app.services import library_ingest


async def _count_quarantined(*, sweep: bool | None, media_type: str | None = None) -> int:
    """Count quarantined DownloadRequests.

    sweep=True → Library Sweep only; sweep=False → non-sweep (user/upload);
    sweep=None → all sources.
    """
    clauses = [DownloadRequest.status == "quarantined"]
    if sweep is True:
        clauses.append(DownloadRequest.source == library_ingest.SOURCE_SWEEP)
    elif sweep is False:
        clauses.append(
            or_(
                DownloadRequest.source.is_(None),
                DownloadRequest.source != library_ingest.SOURCE_SWEEP,
            )
        )
    if media_type:
        clauses.append(DownloadRequest.media_type == media_type)

    async with async_session() as db:
        result = await db.execute(
            select(func.count()).select_from(DownloadRequest).where(*clauses)
        )
        return int(result.scalar_one() or 0)


async def _unprocessed_total(media_type: str) -> int:
    statuses = tuple(library_ingest._UNPROCESSED_STATUSES)
    async with async_session() as db:
        result = await db.execute(
            select(func.count())
            .select_from(DownloadRequest)
            .where(
                DownloadRequest.source == library_ingest.SOURCE_SWEEP,
                DownloadRequest.media_type == media_type,
                DownloadRequest.status.in_(statuses),
            )
        )
        return int(result.scalar_one() or 0)


async def collect_pending_actions() -> dict[str, Any]:
    """Return review/action queues that exist in the admin UI today."""
    quarantined_downloads = await _count_quarantined(sweep=False)
    audiobook_sweep_review = await _count_quarantined(sweep=True, media_type="audiobook")
    ebook_sweep_review = await _count_quarantined(sweep=True, media_type="ebook")
    audiobook_unprocessed = await _unprocessed_total("audiobook")
    ebook_unprocessed = await _unprocessed_total("ebook")

    items: list[dict[str, Any]] = [
        {
            "id": "quarantined_downloads",
            "label": "Downloads needing review",
            "description": "Quarantined user/upload requests awaiting Quick Review or Match metadata",
            "count": quarantined_downloads,
            "href": "/admin?tab=requests",
            "priority": 1,
        },
        {
            "id": "audiobook_sweep_review",
            "label": "Audiobook sweep review",
            "description": "Library Sweep audiobooks waiting for metadata review",
            "count": audiobook_sweep_review,
            "href": "/admin?tab=library-sweep&sweep=audiobook&queue=needs-review",
            "priority": 2,
        },
        {
            "id": "ebook_sweep_review",
            "label": "Ebook sweep review",
            "description": "Library Sweep ebooks waiting for metadata matching",
            "count": ebook_sweep_review,
            "href": "/admin?tab=library-sweep&sweep=ebook&queue=needs-review",
            "priority": 3,
        },
        {
            "id": "audiobook_sweep_unprocessed",
            "label": "Audiobook sweep unprocessed",
            "description": "Cancelled / failed / skipped / rejected sweep audiobooks",
            "count": audiobook_unprocessed,
            "href": "/admin?tab=library-sweep&sweep=audiobook&queue=unprocessed",
            "priority": 4,
        },
        {
            "id": "ebook_sweep_unprocessed",
            "label": "Ebook sweep unprocessed",
            "description": "Cancelled / failed / skipped / rejected sweep ebooks",
            "count": ebook_unprocessed,
            "href": "/admin?tab=library-sweep&sweep=ebook&queue=unprocessed",
            "priority": 5,
        },
    ]

    total = sum(int(i["count"] or 0) for i in items)
    return {
        "total": total,
        "items": items,
    }
