import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.database import init_db
from app.routers import auth, search, requests, admin, books, stream, library, libraries, push
from app.services.pipeline import resume_interrupted_downloads
from app.services.indexer_scraper import start_scraper, stop_scraper

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up -- initializing database")
    await init_db()
    await resume_interrupted_downloads()
    start_scraper()
    yield
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
    allow_origins=[settings.app_url, "http://localhost:5173"],
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
