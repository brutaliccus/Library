import os
from pydantic import model_validator
from pydantic_settings import BaseSettings
from functools import lru_cache


def _host_for_docker(url: str) -> str:
    """When running in Docker, localhost won't reach host services. Use Docker bridge gateway (172.17.0.1)."""
    if not url:
        return url
    lower = url.lower()
    if "localhost" in lower or "127.0.0.1" in lower:
        # 172.17.0.1 is the Docker bridge gateway = host (works on Linux/Raspberry Pi)
        return url.replace("localhost", "172.17.0.1").replace("127.0.0.1", "172.17.0.1")
    return url


class Settings(BaseSettings):
    secret_key: str = "change-me-in-production"
    database_url: str = "sqlite+aiosqlite:///data/app.db"
    app_url: str = "https://library.example.com"

    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7

    prowlarr_url: str = "http://prowlarr:9696"
    prowlarr_api_key: str = ""
    # Max seconds to wait for Prowlarr to query all indexers (each indexer can add latency).
    prowlarr_search_timeout: int = 90
    # Max results per Prowlarr API call (global search is one cap for all indexers combined).
    prowlarr_search_limit: int = 500
    # Dedicated AudioBook Bay-only searches (own cap so ABB is not crowded out).
    prowlarr_abb_search_limit: int = 150
    # Direct multi-page ABB scrape (bypasses Jackett's hardcoded 2-page cap).
    # MUST stay serial + delayed — parallel FlareSolverr tabs crash the Pi.
    abb_deep_search_enabled: bool = False
    abb_live_search_enabled: bool = False
    abb_deep_search_pages: int = 6
    abb_live_search_pages: int = 6
    abb_flaresolverr_timeout: float = 180.0
    abb_flaresolverr_disable_media: bool = True
    abb_mirror_max_tries: int = 3
    abb_mirror_retries: int = 3
    abb_flare_session_ttl: float = 600.0
    abb_resolve_hash_limit: int = 25
    abb_scraper_resolve_hash_limit: int = 12
    rd_gap_probe_batch: int = 20
    abb_author_crawl_enabled: bool = False
    abb_deep_page_delay_seconds: float = 2.5
    # Env seed for the admin "ABB RSS-only" toggle (Admin → Cache → Tuning is
    # the runtime source of truth). When True, no author/deep Flare crawl —
    # only Jackett recent-releases RSS. Live Jackett ABB search still works.
    abb_rss_only: bool = True
    # Background scraper uses fewer pages than live UI (same serial lock).
    abb_scraper_max_pages: int = 4
    # False = book downloads search ABB only (matches site search). True = query every Prowlarr indexer (noisy).
    prowlarr_all_indexers_for_books: bool = False
    # Optional comma-separated extra Prowlarr indexer names (ABB + Knaben are auto-detected).
    prowlarr_trusted_indexer_names: str = ""
    # Max torrent rows returned to the client after ranking (full pool is ranked first).
    search_results_max_return: int = 200
    # Max seconds to wait for ABS/Kavita when enriching indexer results with "in library" badges.
    search_library_enrich_timeout: float = 4.0

    # Background indexer scraper (DMM-style cache)
    scraper_enabled: bool = True
    scraper_interval_seconds: int = 30
    scraper_queries_per_job: int = 12
    scraper_query_delay_seconds: int = 2
    scraper_prowlarr_timeout: int = 120
    scraper_prowlarr_concurrency: int = 2
    scraper_search_retries: int = 2
    scraper_debrid_batch_size: int = 300
    scraper_debrid_interval_hours: int = 1
    scraper_prune_stale_days: int = 30
    scraper_match_batch_size: int = 200
    scraper_knaben_crawl_tasks_per_job: int = 8
    # Override scraper RSS cadence (jobs between recent-release polls). None = 1 when
    # abb_rss_only else 3.
    scraper_rss_every_n_jobs: int | None = None

    real_debrid_api_token: str = ""
    torbox_api_token: str = ""

    abs_url: str = "http://localhost:13378"
    abs_api_key: str = ""
    abs_library_id: str = ""

    kavita_url: str = "http://localhost:5000"
    kavita_api_key: str = ""
    kavita_library_id: int = 0

    google_books_api_key: str = ""
    # Max seconds to wait for Google Books before falling back to Open Library.
    google_books_search_timeout: float = 12.0
    google_books_max_429_retries: int = 1
    # Open Library requires a descriptive User-Agent; generic clients get rate-limited/IP-banned.
    open_library_user_agent: str = "LibrarySite/1.0 (+https://library.example.com)"

    # Local Open Library catalog built from the monthly data dumps (see
    # scripts/ol_import_dumps.py). Lives on the big external drive. When present
    # it replaces live Open Library API calls for torrent matching + book lookups,
    # so the scraper never hammers (and gets IP-banned by) openlibrary.org.
    ol_catalog_enabled: bool = True
    # Query DB lives on the fast SSD (mapped from ./data) so store searches are
    # snappy; the bulky raw dumps stay on the big external HDD.
    ol_catalog_db_path: str = "/app/data/ol_catalog.db"
    ol_dumps_dir: str = "/openlibrary/dumps"
    # Include the huge editions dump (~10 GB gz) only for ISBN lookups. Most torrent
    # release names lack ISBNs; title FTS on works is enough for catalog matching.
    ol_catalog_include_editions: bool = False
    max_ebook_bytes: int = 1_073_741_824  # 1 GiB — skip Knaben ebook packs / comic archives
    nyt_api_key: str = ""  # NYT Books API for real bestsellers (optional, free at developer.nytimes.com)
    # ISBNdb — larger commercial book metadata (~100M+ titles). Optional; Admin → Integrations.
    isbndb_api_key: str = ""
    # Hardcover GraphQL — ratings, series graphs, curated lists (no user-library sync).
    # Token from hardcover.app/account/api. Optional; Admin → Integrations.
    hardcover_api_key: str = ""
    aa_account_id: str = ""
    flaresolverr_url: str = ""

    # Jackett on the normal Docker bridge (not behind Mullvad).
    jackett_url: str = "http://audiobook-jackett:9117"
    jackett_api_key: str = ""
    jackett_abb_timeout: int = 180
    # HTTP/SOCKS proxy for ABB only (gluetun Mullvad). FlareSolverr ABB sessions
    # and ABB live/RSS paths use this — Knaben/Jackett/other traffic does not.
    abb_proxy_url: str = "http://gluetun:8888"
    # Optional env seed for Mullvad; prefer Admin → Integrations (stored in DB +
    # written to data/mullvad.env for gluetun). Never commit the real number.
    mullvad_account_number: str = ""

    audiobook_dir: str = "/audiobooks"
    ebook_dir: str = "/ebooks"

    vapid_private_key: str = ""
    vapid_public_key: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @model_validator(mode="after")
    def fix_docker_urls(self) -> "Settings":
        # Env abb_rss_only turns off background Flare scrapes only. Live Jackett
        # ABB search is separate; abb_live_search_enabled (Flare multi-page) stays
        # at its own env default (False) and is not force-cleared so tests/opt-in work.
        if self.abb_rss_only:
            self.abb_author_crawl_enabled = False
            self.abb_deep_search_enabled = False
        # In Docker, localhost can't reach host services; use host.docker.internal
        if os.path.exists("/.dockerenv"):
            self.abs_url = _host_for_docker(self.abs_url)
            self.kavita_url = _host_for_docker(self.kavita_url)
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
