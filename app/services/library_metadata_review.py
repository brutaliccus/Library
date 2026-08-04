"""Admin metadata match/apply for a single in-library audiobook or ebook.

Reuses the request-pipeline matchers' search/apply backends (LibraForge Manual
Review for audio; Hardcover + Open Library + Calibre embed for ebooks) against
library item paths instead of quarantine staging.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.services import audiobookshelf, kavita, libraforge
from app.services.ebook_pipeline import embed_ebook_metadata, pick_primary_ebook
from app.services.ebook_quick_review import (
    EbookQuickReviewError,
    search_ebook_metadata_candidates,
    selected_result_to_ebook_meta,
)
from app.services.forge_pipeline import (
    _collect_audio,
    seed_staging_metadata_hints,
    staging_path_for_libraforge,
)
from app.services.library_media_delete import resolve_abs_book_dir
from app.services.quick_review import (
    _build_query,
    _enrich_selected_for_apply,
    _provider_hint_from_meta,
    resolve_apply_edit_mode,
)

logger = logging.getLogger(__name__)
settings = get_settings()


class LibraryMetadataReviewError(ValueError):
    """User-facing library metadata review failure."""


def _abs_meta_fields(item: dict[str, Any]) -> dict[str, Any]:
    media = item.get("media") or {}
    meta = media.get("metadata") or {}
    title = str(meta.get("title") or meta.get("titleIgnorePrefix") or "").strip()
    author = str(meta.get("authorName") or "").strip()
    narrator = str(meta.get("narratorName") or "").strip()
    subtitle = str(meta.get("subtitle") or "").strip()
    asin = str(meta.get("asin") or "").strip()
    year = str(meta.get("publishedYear") or meta.get("year") or "").strip()
    publisher = str(meta.get("publisher") or "").strip()
    language = str(meta.get("language") or "").strip()
    genre_raw = meta.get("genres") or []
    if isinstance(genre_raw, list):
        genre = ", ".join(str(g).strip() for g in genre_raw if str(g).strip())
    else:
        genre = str(genre_raw or "").strip()
    summary = str(meta.get("description") or meta.get("summary") or "").strip()

    series = ""
    sequence = ""
    series_list = meta.get("series") or []
    if isinstance(series_list, dict):
        series_list = [series_list]
    if isinstance(series_list, list) and series_list:
        first = series_list[0] if isinstance(series_list[0], dict) else {}
        series = str(first.get("name") or "").strip()
        sequence = str(first.get("sequence") or "").strip()
    if not series:
        from app.utils.book_series import parse_abs_series_label

        series, sequence = parse_abs_series_label(meta.get("seriesName"))
        series = series or ""
        sequence = sequence or ""

    return {
        "title": title,
        "subtitle": subtitle,
        "author": author,
        "narrator": narrator,
        "series": series,
        "sequence": sequence,
        "year": year,
        "asin": asin,
        "publisher": publisher,
        "language": language,
        "genre": genre,
        "summary": summary,
        "cover_url": f"/api/stream/abs/proxy/cover/{item.get('id')}" if item.get("id") else "",
    }


async def _resolve_abs_book_dir(item_id: str) -> tuple[dict[str, Any], Path]:
    item = await audiobookshelf.get_library_item(item_id)
    if not item:
        raise LibraryMetadataReviewError("Audiobook not found")
    audiobook_dir = Path(settings.audiobook_dir)
    try:
        book_dir = resolve_abs_book_dir(audiobook_dir, item)
    except ValueError as e:
        raise LibraryMetadataReviewError(str(e)) from e
    if not book_dir.exists():
        raise LibraryMetadataReviewError(f"Audiobook folder missing: {book_dir}")
    return item, book_dir


async def load_abs_metadata_review(item_id: str) -> dict[str, Any]:
    """Load match clues for an in-library audiobook (ABS item)."""
    item, book_dir = await _resolve_abs_book_dir(item_id)
    fields = _abs_meta_fields(item)
    audio = _collect_audio(book_dir)
    if not audio:
        raise LibraryMetadataReviewError("No audio files found for this audiobook")

    lf_path = staging_path_for_libraforge(book_dir)
    loaded: dict[str, Any] = {}
    try:
        loaded = await libraforge.manual_review_load(lf_path)
    except libraforge.LibraForgeError:
        try:
            lf_path = staging_path_for_libraforge(audio[0])
            loaded = await libraforge.manual_review_load(lf_path)
        except libraforge.LibraForgeError as e:
            logger.info("LibraForge load skipped for library item %s: %s", item_id, e)

    clues_loaded = loaded.get("clues") if isinstance(loaded.get("clues"), dict) else {}
    meta_loaded = loaded.get("metadata") if isinstance(loaded.get("metadata"), dict) else {}

    title = str(clues_loaded.get("title") or fields["title"] or "").strip()
    author = str(clues_loaded.get("author") or fields["author"] or "").strip()
    series = str(clues_loaded.get("series") or fields["series"] or "").strip()
    sequence = str(clues_loaded.get("sequence") or fields["sequence"] or "").strip()
    narrator = str(clues_loaded.get("narrator") or fields["narrator"] or "").strip()
    query = str(clues_loaded.get("query") or "").strip()
    if not query:
        query = _build_query(title=title, author=author, series=series, sequence=sequence)

    metadata = {**fields, **{k: v for k, v in meta_loaded.items() if v not in (None, "")}}
    provider_hint = _provider_hint_from_meta(metadata) or _provider_hint_from_meta(fields)

    return {
        "item_id": item_id,
        "media_type": "audiobook",
        "title": title or fields["title"],
        "author": author or fields["author"] or None,
        "status": "library",
        "quarantine_reason": None,
        "staging_path": lf_path,
        "manual_review_url": libraforge.public_manual_review_url() or None,
        "targets": [
            {
                "relative_path": "",
                "path": lf_path,
                "display_name": book_dir.name,
                "file_count": len(audio),
                "is_grouped": len(audio) > 1,
            }
        ],
        "selected_relative_path": "",
        "target_path": lf_path,
        "source_path": lf_path,
        "is_grouped": len(audio) > 1,
        "file_count": len(audio),
        "queries": [query] if query else [],
        "clues": {
            "query": query,
            "title": title,
            "author": author,
            "series": series,
            "sequence": sequence,
            "narrator": narrator,
        },
        "metadata": metadata,
        "provider_hint": provider_hint,
        "already_applied": False,
    }


async def search_abs_metadata_review(
    item_id: str,
    *,
    query: str = "",
    title: str = "",
    author: str = "",
    series: str = "",
    sequence: str = "",
    narrator: str = "",
    limit: int = 12,
    provider: str = "audible",
) -> dict[str, Any]:
    """Search Audible / specialty catalogs for a library audiobook."""
    await _resolve_abs_book_dir(item_id)

    q = (query or "").strip()
    if not q:
        q = _build_query(title=title, author=author, series=series, sequence=sequence)
    if not q:
        raise LibraryMetadataReviewError("Search query is required")

    metadata = {
        "title": (title or "").strip(),
        "author": (author or "").strip(),
        "series": (series or "").strip(),
        "sequence": (sequence or "").strip(),
        "narrator": (narrator or "").strip(),
    }
    provider_key = (provider or "audible").strip().lower() or "audible"
    try:
        data = await libraforge.manual_review_search(
            query=q,
            metadata=metadata,
            limit=limit,
            provider=provider_key,
        )
    except libraforge.LibraForgeError as e:
        raise LibraryMetadataReviewError(str(e)) from e

    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        results = []
    return {
        "item_id": item_id,
        "queries": data.get("queries") if isinstance(data, dict) else [q],
        "results": results,
        "provider": provider_key,
    }


def _abs_payload_from_selected(
    selected: dict[str, Any],
    *,
    edit_mode: str,
) -> tuple[dict[str, Any], str]:
    """Build ABS PATCH metadata + cover URL from a Manual Review candidate."""
    from app.services.quick_review import _cover_url_from_candidate

    # Prefer chosen_metadata blob when present (same as wizard compare table).
    chosen = selected.get("chosen_metadata")
    if not isinstance(chosen, dict):
        by_mode = selected.get("chosen_metadata_by_mode") or {}
        if isinstance(by_mode, dict):
            preferred = edit_mode or selected.get("recommended_edit_mode") or "full"
            chosen = by_mode.get(preferred) if isinstance(by_mode.get(preferred), dict) else None
        if not isinstance(chosen, dict):
            chosen = {}

    def pick(*keys: str) -> str:
        for key in keys:
            for source in (chosen, selected):
                if not isinstance(source, dict):
                    continue
                val = source.get(key)
                if isinstance(val, list):
                    joined = ", ".join(str(x).strip() for x in val if str(x).strip())
                    if joined:
                        return joined
                text = str(val or "").strip()
                if text:
                    return text
        return ""

    title = pick("title")
    subtitle = pick("subtitle")
    author = pick("author")
    if not author:
        authors = selected.get("authors")
        if isinstance(authors, list):
            author = ", ".join(str(a).strip() for a in authors if str(a).strip())
    narrator = pick("narrator")
    if not narrator:
        narrators = selected.get("narrators")
        if isinstance(narrators, list):
            narrator = ", ".join(str(n).strip() for n in narrators if str(n).strip())
    series = pick("series")
    sequence = pick("sequence")
    year = pick("year", "publishedYear")
    asin = pick("asin")
    publisher = pick("publisher")
    language = pick("language")
    genre = pick("genre")
    summary = pick("summary", "description")
    cover = _cover_url_from_candidate(selected, edit_mode)

    payload: dict[str, Any] = {}
    if title:
        payload["title"] = title
    if subtitle:
        payload["subtitle"] = subtitle
    if author:
        payload["authors"] = [{"name": a.strip()} for a in author.split(",") if a.strip()]
    if narrator:
        payload["narrators"] = [n.strip() for n in narrator.split(",") if n.strip()]
    if asin:
        payload["asin"] = asin
    if publisher:
        payload["publisher"] = publisher
    if year:
        payload["publishedYear"] = year
    if language:
        payload["language"] = language
    if summary:
        payload["description"] = summary
    if genre:
        payload["genres"] = [g.strip() for g in genre.split(",") if g.strip()]
    if series and series.casefold() != title.casefold():
        entry: dict[str, str] = {"name": series}
        if sequence:
            entry["sequence"] = sequence
        payload["series"] = [entry]
    return payload, cover


async def apply_abs_metadata_review(
    item_id: str,
    *,
    selected_result: dict[str, Any],
    edit_mode: str = "full",
    replace_cover: bool = True,
) -> dict[str, Any]:
    """Apply selected metadata to library audiobook files + ABS item."""
    if not isinstance(selected_result, dict) or not selected_result:
        raise LibraryMetadataReviewError("selected_result is required")

    item, book_dir = await _resolve_abs_book_dir(item_id)
    audio = _collect_audio(book_dir)
    if not audio:
        raise LibraryMetadataReviewError("No audio files found for this audiobook")

    lf_path = staging_path_for_libraforge(book_dir)
    mode = resolve_apply_edit_mode(
        selected_result, edit_mode=edit_mode, replace_cover=replace_cover
    )
    enriched, metadata_override = _enrich_selected_for_apply(
        selected_result, edit_mode=mode, replace_cover=replace_cover
    )

    apply_result: dict[str, Any] = {}
    try:
        apply_result = await libraforge.manual_review_apply(
            path=lf_path,
            selected_result=enriched,
            edit_mode=mode,
            write_policy="overwrite",
            replace_cover=replace_cover,
            cover_if_missing=False,
            backup=False,
            metadata_override=metadata_override or None,
        )
    except libraforge.LibraForgeError as e:
        # Fall back to single-file path (some ABS layouts nest oddly for LF).
        try:
            lf_path = staging_path_for_libraforge(audio[0])
            apply_result = await libraforge.manual_review_apply(
                path=lf_path,
                selected_result=enriched,
                edit_mode=mode,
                write_policy="overwrite",
                replace_cover=replace_cover,
                cover_if_missing=False,
                backup=False,
                metadata_override=metadata_override or None,
            )
        except libraforge.LibraForgeError as e2:
            raise LibraryMetadataReviewError(str(e2)) from e2

    payload, cover = _abs_payload_from_selected(enriched, edit_mode=mode)
    preview = (
        apply_result.get("metadata_preview")
        if isinstance(apply_result.get("metadata_preview"), dict)
        else {}
    )
    seed_title = str((preview or {}).get("title") or payload.get("title") or "").strip()
    seed_author = ""
    authors = payload.get("authors")
    if isinstance(authors, list) and authors:
        seed_author = ", ".join(
            str(a.get("name") if isinstance(a, dict) else a).strip()
            for a in authors
            if str(a.get("name") if isinstance(a, dict) else a).strip()
        )
    seed_asin = str((preview or {}).get("asin") or payload.get("asin") or "").strip()
    seed_series = ""
    series_raw = payload.get("series")
    if isinstance(series_raw, list) and series_raw and isinstance(series_raw[0], dict):
        seed_series = str(series_raw[0].get("name") or "").strip()

    try:
        seed_staging_metadata_hints(
            book_dir,
            title=seed_title,
            author=seed_author or None,
            asin=seed_asin or None,
            series=seed_series or None,
            force=True,
        )
    except Exception as e:
        logger.warning("Could not seed metadata.json for library item %s: %s", item_id, e)

    sync = await audiobookshelf.sync_book_dir_metadata_to_abs(book_dir)
    patched = False
    cover_updated = bool(sync.get("cover_updated"))
    if payload:
        patched = await audiobookshelf.update_item_metadata(item_id, metadata=payload)
    if replace_cover and cover:
        cover_updated = await audiobookshelf.set_item_cover_from_url(item_id, cover) or cover_updated
    if patched or cover_updated or sync.get("updated"):
        audiobookshelf.invalidate_cache()

    return {
        "ok": True,
        "item_id": item_id,
        "applied": True,
        "edit_mode": mode,
        "libraforge": apply_result,
        "abs_synced": bool(sync.get("updated") or patched),
        "cover_updated": cover_updated,
        "metadata_preview": {
            "title": seed_title,
            "author": seed_author,
            "asin": seed_asin,
            "series": seed_series,
            "cover_url": cover,
        },
        "cover_url": cover,
    }


def _ebook_author_from_meta(meta: dict[str, Any], series: dict[str, Any]) -> str:
    writers = meta.get("writers") or series.get("authors") or []
    if writers:
        first = writers[0]
        if isinstance(first, dict):
            return str(first.get("name") or "").strip()
        return str(first).strip()
    return ""


async def _resolve_ebook_series(series_id: int) -> tuple[dict[str, Any], dict[str, Any], list[Path]]:
    series_list = await kavita.get_all_series(formats=kavita.EBOOK_FORMATS)
    series = next((s for s in series_list if s.get("id") == series_id), None)
    if not series:
        raise LibraryMetadataReviewError("Ebook series not found")
    meta = await kavita.get_series_metadata(series_id)
    paths = await kavita.get_series_local_file_paths(series_id)
    if not paths:
        raise LibraryMetadataReviewError("No ebook files found for this series")
    return series, meta or {}, paths


async def load_ebook_metadata_review(series_id: int) -> dict[str, Any]:
    """Load match clues for an in-library ebook (Kavita series)."""
    series, meta, paths = await _resolve_ebook_series(series_id)
    title = str(
        series.get("name") or series.get("localizedName") or series.get("originalName") or ""
    ).strip()
    author = _ebook_author_from_meta(meta, series)
    summary = str(meta.get("summary") or meta.get("description") or "").strip()
    query = f"{title} {author}".strip()

    # Series/sequence: prefer folder cues from primary ebook parent name when present.
    series_name = ""
    sequence = ""
    primary = paths[0]
    parent = primary.parent if primary.is_file() else primary
    # Author / Series / Title layout → parent is title, grandparent may be series.
    try:
        ebook_root = Path(settings.ebook_dir).resolve()
        parts = parent.resolve().relative_to(ebook_root).parts
        if len(parts) >= 3:
            series_name = parts[-2]
        elif len(parts) == 2:
            # Author/Title — no series folder
            series_name = ""
    except (ValueError, OSError):
        pass

    cover_url = f"/api/library/reader/cover/ebook?seriesId={series_id}"
    return {
        "series_id": series_id,
        "media_type": "ebook",
        "title": title,
        "author": author or None,
        "status": "library",
        "quarantine_reason": None,
        "staging_path": staging_path_for_libraforge(parent),
        "targets": [
            {
                "relative_path": "",
                "path": staging_path_for_libraforge(p),
                "display_name": p.name,
                "file_count": 1,
                "is_grouped": False,
            }
            for p in paths
        ],
        "selected_relative_path": "",
        "primary_ebook": primary.name,
        "queries": [query] if query else [],
        "clues": {
            "query": query,
            "title": title,
            "author": author,
            "series": series_name,
            "sequence": sequence,
            "cover_url": cover_url,
        },
        "metadata": {
            "title": title,
            "author": author,
            "series": series_name,
            "sequence": sequence,
            "summary": summary,
            "cover_url": cover_url,
        },
        "already_applied": False,
        "provider": "hardcover+open_library",
    }


async def search_ebook_metadata_review(
    series_id: int,
    *,
    query: str = "",
    title: str = "",
    author: str = "",
    limit: int = 12,
) -> dict[str, Any]:
    """Search Hardcover + Open Library for a library ebook."""
    await _resolve_ebook_series(series_id)
    try:
        data = await search_ebook_metadata_candidates(
            query=query,
            title=title,
            author=author,
            limit=limit,
        )
    except EbookQuickReviewError as e:
        raise LibraryMetadataReviewError(str(e)) from e
    return {
        "ok": True,
        "series_id": series_id,
        "query": data.get("query"),
        "provider": data.get("provider"),
        "providers": data.get("providers"),
        "results": data.get("results") or [],
        "queries": data.get("queries") or [],
        "errors": data.get("errors") or [],
    }


async def apply_ebook_metadata_review(
    series_id: int,
    *,
    selected_result: dict[str, Any],
) -> dict[str, Any]:
    """Embed selected metadata into library ebook files and refresh Kavita."""
    if not isinstance(selected_result, dict) or not selected_result:
        raise LibraryMetadataReviewError("selected_result is required")

    series, _meta, paths = await _resolve_ebook_series(series_id)
    try:
        ebook_meta = selected_result_to_ebook_meta(selected_result)
    except EbookQuickReviewError as e:
        raise LibraryMetadataReviewError(str(e)) from e
    ebook_meta.score = max(ebook_meta.score, 0.95)
    ebook_meta.ambiguous = False

    # Prefer the same primary picker when files share a folder; else first path.
    parent = paths[0].parent if paths[0].is_file() else paths[0]
    primary = pick_primary_ebook(parent) if parent.is_dir() else paths[0]
    if primary is None or not primary.exists():
        primary = paths[0]

    embedded_paths: list[str] = []
    for path in paths:
        try:
            ok = await embed_ebook_metadata(path, ebook_meta)
            if ok:
                embedded_paths.append(path.name)
        except Exception as e:
            logger.warning("Could not embed metadata into %s: %s", path, e)

    kavita_updated = await kavita.update_series_identity(
        series_id,
        name=ebook_meta.title,
        author=ebook_meta.author,
        summary=str(selected_result.get("summary") or "").strip() or None,
    )
    try:
        await kavita.scan_library()
    except Exception as e:
        logger.warning("Kavita scan after ebook metadata apply failed: %s", e)
    kavita.invalidate_cache()

    return {
        "ok": True,
        "series_id": series_id,
        "applied": True,
        "embedded": bool(embedded_paths),
        "embedded_files": embedded_paths,
        "primary_ebook": primary.name if primary else None,
        "kavita_updated": kavita_updated,
        "previous_title": str(
            series.get("name") or series.get("localizedName") or ""
        ).strip(),
        "metadata_preview": {
            "title": ebook_meta.title,
            "author": ebook_meta.author,
            "series": ebook_meta.series,
            "sequence": ebook_meta.series_index,
            "isbn13": ebook_meta.isbn13,
            "isbn10": ebook_meta.isbn10,
            "cover_url": ebook_meta.cover_url,
            "source": ebook_meta.source,
        },
        "cover_url": ebook_meta.cover_url,
    }
