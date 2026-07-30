import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.database import init_db
from app.routers import auth, search, requests, admin, books, stream, library, libraries, push, mobile, share
from app.services.pipeline import resume_interrupted_downloads
from app.services.indexer_scraper import start_scraper, stop_scraper

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)
settings = get_settings()

_shelf_refresh_task: asyncio.Task | None = None
_ol_dumps_check_task: asyncio.Task | None = None
_ol_scheduled_build_task: asyncio.Task | None = None
_invite_rotation_task: asyncio.Task | None = None


async def _invite_rotation_loop() -> None:
    """Rotate library invite codes when older than the configured interval."""
    await asyncio.sleep(45)
    while True:
        try:
            from app.services.invite_rotation import rotate_due_invites

            n = await rotate_due_invites()
            if n:
                logger.info("Invite rotation: rotated %s library group(s)", n)
        except Exception as e:
            logger.warning("Invite rotation loop error: %s", e)
        await asyncio.sleep(60)


async def _daily_shelf_refresh_loop() -> None:
    """Ensure trending / new-releases rebuild at least once per UTC day."""
    # First pass shortly after boot so cold starts don't wait for a visitor.
    # Force once so cover-enrichment fixes replace same-day snapshots that still
    # hold blank Open Library stub URLs.
    await asyncio.sleep(15)
    first = True
    while True:
        try:
            from app.routers.books import refresh_daily_shelves

            await refresh_daily_shelves(force=first)
            first = False
        except Exception as e:
            logger.warning("Daily shelf refresh loop error: %s", e)
        # Check hourly; rebuild only when the UTC day rolled over.
        await asyncio.sleep(3600)


async def _ol_dumps_check_loop() -> None:
    """Lightweight daily probe for newer Open Library dumps (notify only)."""
    await asyncio.sleep(90)
    while True:
        try:
            from app.services import ol_catalog_build

            await ol_catalog_build.check_for_updates(force=True, notify=True)
        except Exception as e:
            logger.warning("OL dumps check loop error: %s", e)
        # Once per day is enough — dumps publish monthly.
        await asyncio.sleep(24 * 3600)


async def _ol_scheduled_build_loop() -> None:
    """Fire a previously scheduled OL catalog update when due (no auto-schedule)."""
    await asyncio.sleep(20)
    while True:
        try:
            from app.services import ol_catalog_build

            await ol_catalog_build.tick_scheduled_build()
        except Exception as e:
            logger.warning("OL scheduled build loop error: %s", e)
        await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _shelf_refresh_task, _ol_dumps_check_task, _ol_scheduled_build_task
    global _invite_rotation_task
    logger.info("Starting up -- initializing database")
    await init_db()
    try:
        from app.services.instance_settings import apply_runtime_overrides

        await apply_runtime_overrides()
    except Exception as e:
        logger.warning("Runtime config overrides skipped: %s", e)
    try:
        from app.services import audiobookshelf as abs_svc

        await abs_svc.ensure_metadata_hardening()
    except Exception as e:
        logger.warning("ABS metadata hardening skipped: %s", e)
    await resume_interrupted_downloads()
    try:
        from app.services.library_sweep import resume_running_sweep_on_startup

        await resume_running_sweep_on_startup()
    except Exception as e:
        logger.warning("Library Sweep resume skipped: %s", e)
    start_scraper()
    _shelf_refresh_task = asyncio.create_task(_daily_shelf_refresh_loop())
    _ol_dumps_check_task = asyncio.create_task(_ol_dumps_check_loop())
    _ol_scheduled_build_task = asyncio.create_task(_ol_scheduled_build_loop())
    _invite_rotation_task = asyncio.create_task(_invite_rotation_loop())
    yield
    if _shelf_refresh_task and not _shelf_refresh_task.done():
        _shelf_refresh_task.cancel()
    if _ol_dumps_check_task and not _ol_dumps_check_task.done():
        _ol_dumps_check_task.cancel()
    if _ol_scheduled_build_task and not _ol_scheduled_build_task.done():
        _ol_scheduled_build_task.cancel()
    if _invite_rotation_task and not _invite_rotation_task.done():
        _invite_rotation_task.cancel()
    stop_scraper()
    logger.info("Shutting down")


app = FastAPI(
    title="Audiobook Request System",
    description="Search and request audiobooks for your Audiobookshelf library",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        settings.app_url,
        "http://localhost:5173",
        # Capacitor Android WebView (androidScheme: https) + iOS
        "https://localhost",
        "capacitor://localhost",
        "ionic://localhost",
    ],
    allow_origin_regex=r"https://.*\.ts\.net",  # Tailscale Funnel URLs
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(search.router)
app.include_router(requests.router)
app.include_router(admin.router)
app.include_router(books.router)
app.include_router(stream.router)
app.include_router(library.router)
app.include_router(libraries.router)
app.include_router(push.router)
app.include_router(mobile.router)
app.include_router(share.router)


@app.get("/api/health")
async def health():
    return {"status": "ok"}


static_dir = Path(__file__).parent.parent / "static"
assets_dir = static_dir / "assets"
index_path = static_dir / "index.html"

# App shell + SW must not be cached by the browser or updates never reach PWAs.
_NO_STORE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache",
}


def _static_file_response(path: Path) -> FileResponse:
    headers = _NO_STORE_HEADERS if path.name in ("index.html", "sw.js", "manifest.json") else None
    return FileResponse(path, headers=headers)


if assets_dir.is_dir() and index_path.is_file():
    app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    @app.get("/{full_path:path}")
    async def spa_fallback(request: Request, full_path: str):
        file_path = static_dir / full_path
        if full_path and file_path.is_file():
            return _static_file_response(file_path)
        return _static_file_response(index_path)
