"""Preload catalog-linked torrents into server debrid accounts."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, or_

from app.database import async_session
from app.models import CatalogTorrentMatch, IndexerTorrent
from app.services import debrid, real_debrid, torbox
from app.services.debrid_tokens import apply_server_debrid_tokens

logger = logging.getLogger(__name__)

AUDIO_EXTENSIONS = re.compile(r"\.(m4b|m4a|mp3|flac|aac|ogg|opus)\b", re.I)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class _PreloadCandidate:
    info_hash: str
    magnet_url: str
    providers: list[str]
    has_rd_id: bool
    has_torbox_id: bool


def _row_ok_for_preload(row: IndexerTorrent) -> bool:
    """Final gate before RD/TorBox — never preload adult/music/movie/tiny-audio junk."""
    from app.services.rss_content_filters import (
        is_too_small_for_audiobook,
        title_is_non_book,
    )

    if title_is_non_book(row.title or ""):
        return False
    if is_too_small_for_audiobook(row.size_bytes, row.media_type):
        return False
    return True


async def _select_audio_files(client, torrent_id: str) -> None:
    info = await client.get_torrent_info(torrent_id)
    audio_ids: list[str] = []
    for f in info.get("files", []):
        path = f.get("path", "")
        fname = path.rsplit("/", 1)[-1] if "/" in path else path
        if AUDIO_EXTENSIONS.search(fname):
            audio_ids.append(str(f.get("id")))
    if audio_ids:
        await client.select_files(torrent_id, ",".join(audio_ids))
    else:
        await client.select_files(torrent_id, "all")


async def preload_magnet(
    magnet: str,
    info_hash: str,
    providers: list[str],
    *,
    skip_rd: bool = False,
    skip_torbox: bool = False,
    poll_timeout: int = 90,
) -> dict[str, str]:
    """Add one magnet to debrid accounts. Returns {provider: torrent_id}."""
    magnet = (magnet or "").strip()
    if not magnet:
        return {}

    added: dict[str, str] = {}
    for provider in providers:
        if provider == debrid.RD and skip_rd:
            continue
        if provider == debrid.TORBOX and skip_torbox:
            continue

        client = debrid.get_client(provider)
        try:
            if provider == debrid.RD:
                result = await real_debrid.ensure_magnet_in_account(magnet, info_hash)
            else:
                result = await torbox.ensure_magnet_in_account(magnet, info_hash)
            torrent_id = str(result.get("id") or "")
            if not torrent_id:
                continue

            try:
                await _select_audio_files(client, torrent_id)
            except Exception as e:
                logger.debug("Preload file select skipped for %s: %s", info_hash[:12], e)

            if poll_timeout > 0:
                try:
                    await client.poll_until_ready(
                        torrent_id, interval=2, timeout=poll_timeout
                    )
                except Exception as e:
                    logger.debug(
                        "Preload poll incomplete for %s on %s: %s",
                        info_hash[:12],
                        provider,
                        e,
                    )

            # Magnet accepted into the account counts as preloaded (RD/Torbox may still be fetching).
            added[provider] = torrent_id
        except Exception as e:
            logger.warning(
                "Debrid preload failed for %s on %s: %s",
                info_hash[:12],
                provider,
                e,
            )
    return added


async def preload_torrent_row(
    row: IndexerTorrent,
    providers: list[str],
) -> dict[str, str]:
    """Add one torrent to debrid accounts. Returns {provider: torrent_id}."""
    return await preload_magnet(
        row.magnet_url or "",
        row.info_hash,
        providers,
        skip_rd=bool(row.rd_debrid_id),
        skip_torbox=bool(row.torbox_debrid_id),
    )


def _needs_provider(row: IndexerTorrent, providers: list[str]) -> list[str]:
    need: list[str] = []
    if debrid.RD in providers and not row.rd_debrid_id:
        need.append(debrid.RD)
    if debrid.TORBOX in providers and not row.torbox_debrid_id:
        need.append(debrid.TORBOX)
    return need


async def run_preload_batch(
    batch_size: int = 12,
    *,
    poll_timeout: int = 90,
    concurrency: int = 4,
) -> dict[str, int]:
    """Add book torrents to configured server debrid accounts (catalog matches first).

    DB sessions are not held across debrid HTTP calls — that was exhausting the
    SQLAlchemy connection pool during full rescans.
    """
    await apply_server_debrid_tokens()
    providers = debrid.available_providers()
    if not providers:
        return {"candidates": 0, "preloaded": 0}

    candidates: list[_PreloadCandidate] = []
    seen_hashes: set[str] = set()

    async with async_session() as db:
        # Priority 1: catalog exact/likely matches
        linked = (
            await db.execute(
                select(IndexerTorrent)
                .join(
                    CatalogTorrentMatch,
                    CatalogTorrentMatch.info_hash == IndexerTorrent.info_hash,
                )
                .where(
                    IndexerTorrent.is_active.is_(True),
                    CatalogTorrentMatch.match_tier.in_(("exact", "likely")),
                    IndexerTorrent.magnet_url.isnot(None),
                    IndexerTorrent.magnet_url != "",
                    or_(
                        IndexerTorrent.rd_debrid_id.is_(None),
                        IndexerTorrent.torbox_debrid_id.is_(None),
                    ),
                )
                .order_by(CatalogTorrentMatch.score.desc())
                .limit(max(batch_size * 4, 40))
            )
        ).scalars().unique().all()

        for row in linked:
            if not _row_ok_for_preload(row):
                continue
            need = _needs_provider(row, providers)
            if not need:
                continue
            candidates.append(
                _PreloadCandidate(
                    info_hash=row.info_hash,
                    magnet_url=row.magnet_url or "",
                    providers=need,
                    has_rd_id=bool(row.rd_debrid_id),
                    has_torbox_id=bool(row.torbox_debrid_id),
                )
            )
            seen_hashes.add(row.info_hash)
            if len(candidates) >= batch_size:
                break

        # Priority 2: any active audiobook/ebook with a magnet
        if len(candidates) < batch_size:
            extra = (
                await db.execute(
                    select(IndexerTorrent)
                    .where(
                        IndexerTorrent.is_active.is_(True),
                        IndexerTorrent.media_type.in_(("audiobook", "ebook")),
                        IndexerTorrent.magnet_url.isnot(None),
                        IndexerTorrent.magnet_url != "",
                        or_(
                            IndexerTorrent.rd_debrid_id.is_(None),
                            IndexerTorrent.torbox_debrid_id.is_(None),
                        ),
                    )
                    .order_by(IndexerTorrent.seeders.desc(), IndexerTorrent.last_seen_at.desc())
                    .limit(batch_size * 6)
                )
            ).scalars().all()

            for row in extra:
                if row.info_hash in seen_hashes:
                    continue
                if not _row_ok_for_preload(row):
                    continue
                need = _needs_provider(row, providers)
                if not need:
                    continue
                candidates.append(
                    _PreloadCandidate(
                        info_hash=row.info_hash,
                        magnet_url=row.magnet_url or "",
                        providers=need,
                        has_rd_id=bool(row.rd_debrid_id),
                        has_torbox_id=bool(row.torbox_debrid_id),
                    )
                )
                seen_hashes.add(row.info_hash)
                if len(candidates) >= batch_size:
                    break

    # Network work with no DB connection held — bounded concurrency.
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(cand: _PreloadCandidate) -> tuple[str, dict[str, str]] | None:
        async with sem:
            ids = await preload_magnet(
                cand.magnet_url,
                cand.info_hash,
                cand.providers,
                skip_rd=cand.has_rd_id,
                skip_torbox=cand.has_torbox_id,
                poll_timeout=poll_timeout,
            )
            return (cand.info_hash, ids) if ids else None

    raw = await asyncio.gather(*(_one(c) for c in candidates))
    results = [r for r in raw if r]

    preloaded = 0
    if results:
        now = _utcnow()
        async with async_session() as db:
            by_hash = {h: ids for h, ids in results}
            rows = (
                await db.execute(
                    select(IndexerTorrent).where(IndexerTorrent.info_hash.in_(list(by_hash)))
                )
            ).scalars().all()
            for row in rows:
                ids = by_hash.get(row.info_hash) or {}
                if not ids:
                    continue
                if debrid.RD in ids:
                    row.rd_debrid_id = ids[debrid.RD]
                    row.rd_preloaded_at = now
                    row.rd_cached = True
                if debrid.TORBOX in ids:
                    row.torbox_debrid_id = ids[debrid.TORBOX]
                    row.torbox_preloaded_at = now
                    row.torbox_cached = True
                row.last_debrid_check_at = now
                preloaded += 1
            if preloaded:
                await db.commit()
                real_debrid.invalidate_account_cache()
                torbox.invalidate_account_cache()

    logger.info(
        "Debrid preload: added %s/%s torrents to accounts",
        preloaded,
        len(candidates),
    )
    return {"candidates": len(candidates), "preloaded": preloaded}
