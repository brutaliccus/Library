#!/usr/bin/env python3
"""Download a prebuilt Open Library catalog DB into the path the app expects.

Looks for GitHub Release assets on tag ``data-seed`` (same channel as the
indexer cache seed):

  - ol_catalog.db.gz              (single file, preferred when < ~2 GB)
  - ol_catalog.manifest.json      (multipart: lists ol_catalog.db.gz.partNN)

Destination defaults to ``./data/ol_catalog.db`` (container:
``/app/data/ol_catalog.db`` via OL_CATALOG_DB_PATH).

Env:
  LIBRARY_OL_CACHE_URL   — override primary download URL (file or manifest)
  LIBRARY_OL_CACHE_DIR   — override destination directory (default: <repo>/data)
  OL_CATALOG_DB_PATH     — full destination path (wins over dir + filename)
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = ROOT / "data"
DEFAULT_DB_NAME = "ol_catalog.db"
RELEASE_BASE = "https://github.com/brutaliccus/Library/releases/download/data-seed"
UA = "LibrarySite-OLFetch/1.0 (+https://github.com/brutaliccus/Library)"


def _dest_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    env_path = os.environ.get("OL_CATALOG_DB_PATH", "").strip()
    if env_path and not env_path.startswith("/app/"):
        return Path(env_path)
    # Container path → host data dir
    base = Path(os.environ.get("LIBRARY_OL_CACHE_DIR", DEFAULT_DIR))
    return base / DEFAULT_DB_NAME


def _catalog_ready(path: Path, min_bytes: int = 1024 * 1024) -> bool:
    try:
        return path.is_file() and path.stat().st_size >= min_bytes
    except OSError:
        return False


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    print(f"Downloading {url}")
    with urllib.request.urlopen(req, timeout=600) as resp, open(tmp, "wb") as out:
        shutil.copyfileobj(resp, out, length=1024 * 1024)
    tmp.replace(dest)
    print(f"  -> {dest} ({dest.stat().st_size / (1024 * 1024):.1f} MiB)")


def _url_exists(url: str) -> bool:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return 200 <= resp.status < 400
    except urllib.error.HTTPError as e:
        # Some CDNs dislike HEAD — try a ranged GET.
        if e.code in (403, 405):
            try:
                req2 = urllib.request.Request(
                    url,
                    headers={"User-Agent": UA, "Range": "bytes=0-0"},
                )
                with urllib.request.urlopen(req2, timeout=20) as resp:
                    return 200 <= resp.status < 400
            except Exception:
                return False
        return False
    except Exception:
        return False


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _decompress_gz(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"Decompressing {src.name} -> {dest}")
    with gzip.open(src, "rb") as fin, open(tmp, "wb") as fout:
        shutil.copyfileobj(fin, fout, length=1024 * 1024)
    tmp.replace(dest)
    print(f"  -> {dest} ({dest.stat().st_size / (1024 ** 3):.2f} GiB)")


def _fetch_multipart(manifest_url: str, work: Path, dest: Path) -> None:
    man_path = work / "ol_catalog.manifest.json"
    _download(manifest_url, man_path)
    manifest = json.loads(man_path.read_text(encoding="utf-8"))
    parts = manifest.get("parts") or []
    if not parts:
        raise RuntimeError("manifest has no parts")
    base = manifest.get("base_url") or str(manifest_url.rsplit("/", 1)[0])
    assembled = work / "ol_catalog.db.gz"
    with open(assembled, "wb") as out:
        for part in parts:
            name = part["name"] if isinstance(part, dict) else str(part)
            url = part.get("url") if isinstance(part, dict) else None
            if not url:
                url = f"{base.rstrip('/')}/{name}"
            part_path = work / name
            if not part_path.is_file():
                _download(url, part_path)
            expect = part.get("sha256") if isinstance(part, dict) else None
            if expect:
                got = _sha256_file(part_path)
                if got != expect:
                    raise RuntimeError(f"checksum mismatch for {name}")
            with open(part_path, "rb") as inp:
                shutil.copyfileobj(inp, out, length=1024 * 1024)
    expect_gz = manifest.get("sha256_gz")
    if expect_gz:
        got = _sha256_file(assembled)
        if got != expect_gz:
            raise RuntimeError("assembled gz checksum mismatch")
    _decompress_gz(assembled, dest)


def fetch(dest: Path, urls: list[str], force: bool = False) -> int:
    if _catalog_ready(dest) and not force:
        print(f"Open Library catalog already present ({dest}, {dest.stat().st_size / (1024**3):.2f} GiB)")
        return 0

    override = os.environ.get("LIBRARY_OL_CACHE_URL", "").strip()
    candidates = [override] if override else []
    candidates.extend(urls)

    with tempfile.TemporaryDirectory(prefix="ol-fetch-") as tmp:
        work = Path(tmp)
        last_err: Exception | None = None
        for url in candidates:
            if not url:
                continue
            try:
                if url.endswith(".manifest.json") or url.endswith("ol_catalog.manifest.json"):
                    if not override and not _url_exists(url):
                        print(f"skip missing {url}")
                        continue
                    _fetch_multipart(url, work, dest)
                    print("Open Library prebuilt catalog installed")
                    return 0
                if not override and not _url_exists(url):
                    print(f"skip missing {url}")
                    continue
                gz_path = work / "ol_catalog.db.gz"
                _download(url, gz_path)
                # Accept either gzip or raw sqlite (unlikely).
                magic = gz_path.read_bytes()[:2]
                if magic == b"\x1f\x8b":
                    _decompress_gz(gz_path, dest)
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(gz_path), str(dest))
                print("Open Library prebuilt catalog installed")
                return 0
            except Exception as e:
                last_err = e
                print(f"warn: failed {url}: {e}")
                continue
        if last_err:
            print(f"Open Library prebuilt download failed: {last_err}")
        else:
            print(
                "No prebuilt Open Library catalog asset found on the data-seed release yet.\n"
                "  Maintainers: run scripts/export_ol_catalog_seed.py on a host that has a built DB,\n"
                "  then upload parts to the GitHub Release tag data-seed.\n"
                "  Installers can still Build locally or Skip."
            )
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", default="", help="Destination ol_catalog.db path")
    parser.add_argument("--force", action="store_true", help="Re-download even if present")
    args = parser.parse_args()
    dest = _dest_path(args.dest or None)
    urls = [
        f"{RELEASE_BASE}/ol_catalog.manifest.json",
        f"{RELEASE_BASE}/ol_catalog.db.gz",
    ]
    return fetch(dest, urls, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
