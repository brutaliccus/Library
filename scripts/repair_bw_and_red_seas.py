"""Repair Burning Witch OPF indexes + consolidate Red Seas chapter folders."""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path


async def repair_burning_witch() -> None:
    from app.services import kavita
    from app.services.ebook_pipeline import EbookMeta, embed_ebook_metadata, read_ebook_metadata

    series_id = 106
    paths = await kavita.get_series_local_file_paths(series_id)
    print("BW paths", [p.name for p in paths])

    plans = {
        "the burning witch.epub": ("The Burning Witch", "The Burning Witch", "1"),
        "the burning witch 2.epub": ("The Burning Witch 2", "The Burning Witch", "2"),
        "the burning witch 3 a humorous romantic fantasy.epub": (
            "The Burning Witch 3: A Humorous Romantic Fantasy",
            "The Burning Witch",
            "3",
        ),
    }

    for path in paths:
        key = path.name.lower()
        plan = plans.get(key)
        if not plan:
            # fuzzy stem match
            stem = path.stem.lower()
            if "burning witch 3" in stem:
                plan = plans["the burning witch 3 a humorous romantic fantasy.epub"]
            elif "burning witch 2" in stem:
                plan = plans["the burning witch 2.epub"]
            elif "burning witch" in stem:
                plan = plans["the burning witch.epub"]
        if not plan:
            print("SKIP unknown", path.name)
            continue
        title, series, index = plan
        before = await read_ebook_metadata(path)
        print("BEFORE", path.name, before.get("title"), before.get("series_index"))
        meta = EbookMeta(
            title=title,
            author="Delemhach",
            series=series,
            series_index=index,
            score=1.0,
            source="repair",
            reason="restore distinct Burning Witch volumes",
        )
        ok = await embed_ebook_metadata(path, meta)
        after = await read_ebook_metadata(path)
        print("AFTER", path.name, ok, after.get("title"), after.get("series_index"))

    await kavita.update_series_identity(
        series_id,
        name="The Burning Witch",
        author="Delemhach",
    )
    await kavita.scan_series(series_id)
    kavita.invalidate_cache()
    vols = await kavita.get_series_volumes(series_id)
    print("BW volumes after scan:", len(vols or []))
    for vol in vols or []:
        for ch in vol.get("chapters") or []:
            print(
                "  vol",
                vol.get("id"),
                "ch",
                ch.get("id"),
                "files",
                [Path(f.get("filePath") or "").name for f in (ch.get("files") or [])],
            )


def consolidate_red_seas() -> int:
    root = Path("/audiobooks/Scott Lynch")
    if not root.is_dir():
        root = Path("/mnt/Audiobooks/Scott Lynch")
    if not root.is_dir():
        print("Red Seas root missing")
        return 0

    dest = root / "Red Seas Under Red Skies"
    dest.mkdir(parents=True, exist_ok=True)

    # Folders that belong to the fragmented book (sibling chapter folders).
    move_names = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        name = child.name
        if name == "Red Seas Under Red Skies":
            continue
        low = name.lower()
        if any(
            low.startswith(p)
            for p in (
                "ch ",
                "prologue",
                "epilogue",
                "reminiscence",
                "cards in the",
                "cards on the",
            )
        ):
            move_names.append(name)

    moved = 0
    for name in move_names:
        src = root / name
        # Move audio files + sidecar into dest, flatten one level.
        for f in src.rglob("*"):
            if not f.is_file():
                continue
            if f.suffix.lower() not in {".m4b", ".mp3", ".m4a", ".flac", ".ogg", ".json", ".jpg", ".jpeg", ".png", ".nfo"}:
                continue
            target = dest / f.name
            if target.exists():
                target = dest / f"{src.name}__{f.name}"
            print("MOVE", f, "->", target)
            shutil.move(str(f), str(target))
            moved += 1
        # Remove empty chapter folder tree
        try:
            shutil.rmtree(src)
            print("RMDIR", src)
        except OSError as e:
            print("RMDIR fail", src, e)
    print("Red Seas files moved", moved, "from", len(move_names), "folders")
    return moved


async def main() -> None:
    await repair_burning_witch()
    n = consolidate_red_seas()
    if n:
        from app.services import audiobookshelf

        # Best-effort ABS library scan so chapter folders disappear from item list.
        try:
            await audiobookshelf.scan_library()
            print("ABS scan triggered")
        except Exception as e:
            print("ABS scan failed/skipped:", e)
        audiobookshelf.invalidate_cache()


if __name__ == "__main__":
    asyncio.run(main())