#!/usr/bin/env python3
"""Package a local ol_catalog.db for the GitHub ``data-seed`` release.

GitHub Release assets are capped (~2 GiB). This script gzip-compresses the DB
and splits the archive into parts under ``--out-dir``, plus a manifest the
installer understands:

  ol_catalog.db.gz.part01 ...
  ol_catalog.manifest.json

Upload every file in the output directory to the ``data-seed`` release
(https://github.com/brutaliccus/Library/releases/tag/data-seed).

Usage (on a host that already built the catalog, e.g. the Pi):
  python scripts/export_ol_catalog_seed.py /path/to/ol_catalog.db ./seed/ol
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shutil
import sys
from pathlib import Path

DEFAULT_PART_BYTES = 1900 * 1024 * 1024  # stay under GitHub's ~2 GiB limit
RELEASE_BASE = "https://github.com/brutaliccus/Library/releases/download/data-seed"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def export(src: Path, out_dir: Path, part_bytes: int) -> int:
    if not src.is_file():
        print(f"missing source DB: {src}", file=sys.stderr)
        return 1
    out_dir.mkdir(parents=True, exist_ok=True)
    gz_path = out_dir / "ol_catalog.db.gz"
    print(f"source={src} size_gib={src.stat().st_size / (1024**3):.2f}")
    print(f"compressing -> {gz_path} (this can take a while)...")
    with open(src, "rb") as fin, gzip.open(gz_path, "wb", compresslevel=6) as fout:
        shutil.copyfileobj(fin, fout, length=1024 * 1024)
    gz_size = gz_path.stat().st_size
    print(f"gz_size_gib={gz_size / (1024**3):.2f}")
    gz_sha = _sha256(gz_path)

    parts_meta: list[dict] = []
    if gz_size <= part_bytes:
        # Single-file publish path — rename is enough; also emit a 1-part manifest.
        single = out_dir / "ol_catalog.db.gz"
        assert single == gz_path
        parts_meta.append(
            {
                "name": "ol_catalog.db.gz",
                "sha256": gz_sha,
                "bytes": gz_size,
            }
        )
        print("Single-part archive fits under the part size limit.")
    else:
        print(f"Splitting into <={part_bytes} byte parts...")
        idx = 1
        with open(gz_path, "rb") as fin:
            while True:
                chunk = fin.read(part_bytes)
                if not chunk:
                    break
                name = f"ol_catalog.db.gz.part{idx:02d}"
                part_path = out_dir / name
                part_path.write_bytes(chunk)
                parts_meta.append(
                    {
                        "name": name,
                        "sha256": _sha256(part_path),
                        "bytes": len(chunk),
                    }
                )
                print(f"  wrote {name} ({len(chunk) / (1024**2):.1f} MiB)")
                idx += 1
        # Keep the full gz for local verification; upload parts + manifest only if huge.
        print(f"Note: upload the .partNN files + manifest (full gz is {gz_size / (1024**3):.2f} GiB).")

    manifest = {
        "name": "ol_catalog.db",
        "format": "gzip-split",
        "base_url": RELEASE_BASE,
        "sha256_gz": gz_sha,
        "bytes_gz": gz_size,
        "bytes_raw": src.stat().st_size,
        "parts": parts_meta,
    }
    man_path = out_dir / "ol_catalog.manifest.json"
    man_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {man_path}")
    print("Upload to GitHub Release tag data-seed, then fresh installs can Download prebuilt.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "source",
        nargs="?",
        default="",
        help="Path to ol_catalog.db (default: ./data/ol_catalog.db)",
    )
    parser.add_argument(
        "out_dir",
        nargs="?",
        default="",
        help="Output directory (default: ./seed/ol)",
    )
    parser.add_argument(
        "--part-bytes",
        type=int,
        default=DEFAULT_PART_BYTES,
        help="Max bytes per part (default ~1.9 GiB)",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    src = Path(args.source) if args.source else root / "data" / "ol_catalog.db"
    out_dir = Path(args.out_dir) if args.out_dir else root / "seed" / "ol"
    return export(src, out_dir, args.part_bytes)


if __name__ == "__main__":
    raise SystemExit(main())
