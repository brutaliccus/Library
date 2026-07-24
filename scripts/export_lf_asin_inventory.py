#!/usr/bin/env python3
"""Export library paths that LibraForge can resolve an ASIN for.

Runs inside the LibraForge container. Uses the same ownership chain as LF
``_owned_asins_for_folder``: filename ``[B0…]`` token → ``scan_cache`` /
sidecar audible ASIN → embedded mutagen tags.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, "/app")

from app.main import (  # noqa: E402
    AUDIOBOOKS_ROOT,
    _categorise_book_unit,
    _filter_ignored_units,
    _owned_asins_for_folder,
    _scan_book_units,
)


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else str(AUDIOBOOKS_ROOT))
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "/app/reports/asin-inventory.json")
    ignored = ["_unorganized", ".unorganized"]
    if len(sys.argv) > 3:
        ignored = [x for x in sys.argv[3].split(",") if x]

    units = _filter_ignored_units(_scan_book_units(root), root, ignored)
    cats: Counter[str] = Counter()
    rows: list[dict] = []
    for _ref, audio, book_dir in units:
        cat = _categorise_book_unit(audio, book_dir, root)
        cats[cat] += 1
        folder = book_dir if book_dir.is_dir() else book_dir.parent
        asins = sorted(_owned_asins_for_folder(folder))
        asin = asins[0] if asins else ""
        if not asin:
            continue
        rows.append(
            {
                "folder": str(folder),
                "audio": str(audio[0]) if audio else "",
                "asin": asin,
                "asins": asins,
                "category": cat,
            }
        )

    payload = {
        "root": str(root),
        "ignored": ignored,
        "categories": dict(cats),
        "complete_ui": cats.get("organized", 0) + cats.get("ready_to_organize", 0),
        "with_asin": len(rows),
        "books": rows,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in payload.items() if k != "books"}, indent=2))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
