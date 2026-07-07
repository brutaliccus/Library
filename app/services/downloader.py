import asyncio
import logging
import re
import shutil
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

ARCHIVE_SUFFIXES = {".rar", ".zip", ".7z", ".tar", ".gz", ".bz2", ".tgz", ".tbz2"}
KAVITA_CONVERT_TO_EPUB = {".mobi", ".azw", ".azw3"}
# Backwards-compatible alias used by convert helpers
KAVITA_UNSUPPORTED_EBOK = KAVITA_CONVERT_TO_EPUB


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    name = name.strip(". ")
    return name or "Unknown"


def _is_archive(path: Path) -> bool:
    name = path.name.lower()
    if name.endswith((".tar.gz", ".tar.bz2")):
        return True
    return path.suffix.lower() in ARCHIVE_SUFFIXES


async def extract_archive(archive_path: Path) -> bool:
    """Extract an archive in-place using 7z, then remove the archive file."""
    dest_dir = archive_path.parent
    logger.info(f"Extracting archive: {archive_path}")

    proc = await asyncio.create_subprocess_exec(
        "7z", "x", "-y", f"-o{dest_dir}", str(archive_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        logger.error(f"7z extraction failed (exit {proc.returncode}): {stderr.decode(errors='replace')}")
        return False

    logger.info(f"Extracted {archive_path.name}, removing archive")
    archive_path.unlink(missing_ok=True)

    extracted = list(dest_dir.iterdir())
    for item in extracted:
        if item.is_file() and _is_archive(item):
            logger.info(f"Found nested archive: {item.name}, extracting")
            await extract_archive(item)

    return True


def parse_torrent_name(title: str) -> tuple[str, str]:
    """Best-effort extraction of author and book title from a torrent name.

    Common patterns:
      - "Author - Title (year)" 
      - "Title by Author"
      - "Author_Title"
    Falls back to using the full title if no pattern matches.
    """
    title = title.strip()

    if " - " in title:
        parts = title.split(" - ", 1)
        author = parts[0].strip()
        book = re.sub(r"\(.*?\)|\[.*?\]", "", parts[1]).strip()
        return sanitize_filename(author), sanitize_filename(book)

    by_match = re.search(r"^(.+?)\s+by\s+(.+?)(?:\s*[\(\[]|$)", title, re.IGNORECASE)
    if by_match:
        return sanitize_filename(by_match.group(2)), sanitize_filename(by_match.group(1))

    cleaned = re.sub(r"\(.*?\)|\[.*?\]", "", title).strip()
    return "Unknown Author", sanitize_filename(cleaned)


def _looks_like_html(data: bytes) -> bool:
    """Detect HTML even when server lies about Content-Type."""
    if len(data) < 10:
        return False
    start = data[:2048].decode("utf-8", errors="ignore").strip().lower()
    return (
        start.startswith("<!doctype")
        or start.startswith("<html")
        or (start.startswith("<") and "<html" in start[:200])
        or "cloudflare" in start[:500]
        or "checking your browser" in start[:500]
    )


ProgressCallback = Callable[[int, int | None, float], Awaitable[None] | None]


async def download_file(
    url: str,
    dest_dir: Path,
    filename: str | None = None,
    on_progress: ProgressCallback | None = None,
) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(follow_redirects=True) as client:
        async with client.stream("GET", url, timeout=600) as resp:
            resp.raise_for_status()

            content_type = (resp.headers.get("content-type") or "").lower()
            if "text/html" in content_type:
                raise RuntimeError(f"Download URL returned HTML instead of file (Content-Type: {content_type})")

            total_bytes: int | None = None
            cl = resp.headers.get("content-length")
            if cl and cl.isdigit():
                total_bytes = int(cl)

            if not filename:
                cd = resp.headers.get("content-disposition", "")
                match = re.search(r'filename="?([^";\n]+)"?', cd)
                if match:
                    filename = sanitize_filename(match.group(1))
                else:
                    filename = sanitize_filename(str(resp.url).split("/")[-1].split("?")[0])

            dest_path = dest_dir / filename
            first = True
            bytes_done = 0
            last_report = 0.0
            last_bytes = 0
            speed_bps = 0.0

            with open(dest_path, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=1024 * 256):
                    if first:
                        first = False
                        if _looks_like_html(chunk):
                            raise RuntimeError("Download URL returned HTML instead of file (content sniff)")
                    f.write(chunk)
                    bytes_done += len(chunk)

                    if on_progress:
                        now = time.monotonic()
                        if now - last_report >= 0.4:
                            elapsed = now - last_report
                            if elapsed > 0:
                                speed_bps = (bytes_done - last_bytes) / elapsed
                            last_report = now
                            last_bytes = bytes_done
                            result = on_progress(bytes_done, total_bytes, speed_bps)
                            if asyncio.iscoroutine(result):
                                await result

            if on_progress:
                result = on_progress(bytes_done, total_bytes, 0.0)
                if asyncio.iscoroutine(result):
                    await result

    if _is_archive(dest_path):
        await extract_archive(dest_path)

    return dest_path


def _get_ebook_convert_path() -> str | None:
    """Return path to ebook-convert, or None if not found."""
    path = shutil.which("ebook-convert")
    if path:
        return path
    for candidate in ("/usr/bin/ebook-convert", "/usr/local/bin/ebook-convert"):
        if Path(candidate).exists():
            return candidate
    return None


async def convert_ebook_to_epub(path: Path, ebook_convert: str) -> Path | None:
    """Convert MOBI/AZW to EPUB for Kavita's HTML reader. Returns path to EPUB or None."""
    if path.suffix.lower() not in KAVITA_UNSUPPORTED_EBOK:
        return path
    epub_path = path.with_suffix(".epub")
    logger.info(f"Converting {path.name} to EPUB for Kavita")
    proc = await asyncio.create_subprocess_exec(
        ebook_convert,
        str(path),
        str(epub_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode(errors="replace")
        out = stdout.decode(errors="replace")
        logger.error(f"ebook-convert failed for {path.name} (exit {proc.returncode})")
        if err:
            logger.error(f"stderr: {err}")
        if out:
            logger.debug(f"stdout: {out}")
        return None
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        logger.warning(f"Could not remove original {path.name}: {e}")
    return epub_path


async def convert_ebooks_in_dir(dest_dir: Path) -> None:
    """Convert MOBI/AZW files in directory to EPUB for Kavita."""
    ebook_convert = _get_ebook_convert_path()
    if not ebook_convert:
        logger.warning("ebook-convert (Calibre) not found - skipping ebook conversion. Install calibre in the container.")
        return

    if not dest_dir.exists():
        logger.warning(f"Ebook dir does not exist: {dest_dir}")
        return

    to_convert = [f for f in dest_dir.rglob("*") if f.is_file() and f.suffix.lower() in KAVITA_CONVERT_TO_EPUB]
    if not to_convert:
        logger.debug(f"No MOBI/AZW files to convert in {dest_dir}")
        return

    logger.info(f"Found {len(to_convert)} file(s) to convert in {dest_dir}")
    for f in to_convert:
        result = await convert_ebook_to_epub(f, ebook_convert)
        if result:
            logger.info(f"Converted {f.name} -> {result.name}")
