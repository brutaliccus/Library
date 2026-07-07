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
    app_url: str = "https://library.freiverse.com"

    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7

    prowlarr_url: str = "http://prowlarr:9696"
    prowlarr_api_key: str = ""
    # Max seconds to wait for Prowlarr to query all indexers (each indexer can add latency).
    prowlarr_search_timeout: int = 90
    # Max results per Prowlarr API call (global search is one cap for all indexers combined).
    prowlarr_search_limit: int = 250
    # Dedicated AudioBook Bay-only searches (own cap so ABB is not crowded out).
    prowlarr_abb_search_limit: int = 150
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
    scraper_queries_per_job: int = 8
    scraper_query_delay_seconds: int = 2
    scraper_prowlarr_timeout: int = 120
    scraper_prowlarr_concurrency: int = 2
    scraper_search_retries: int = 2
    scraper_debrid_batch_size: int = 300
    scraper_debrid_interval_hours: int = 1
    scraper_prune_stale_days: int = 30
    scraper_match_batch_size: int = 200

    real_debrid_api_token: str = ""
    torbox_api_token: str = ""

    abs_url: str = "http://localhost:13378"
    abs_api_key: str = ""
    abs_library_id: str = ""

    kavita_url: str = "http://localhost:5000"
    kavita_api_key: str = ""
    kavita_library_id: int = 0

    google_books_api_key: str = ""
    nyt_api_key: str = ""  # NYT Books API for real bestsellers (optional, free at developer.nytimes.com)
    aa_account_id: str = ""
    flaresolverr_url: str = ""

    audiobook_dir: str = "/audiobooks"
    ebook_dir: str = "/ebooks"

    vapid_private_key: str = ""
    vapid_public_key: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @model_validator(mode="after")
    def fix_docker_urls(self) -> "Settings":
        # In Docker, localhost can't reach host services; use host.docker.internal
        if os.path.exists("/.dockerenv"):
            self.abs_url = _host_for_docker(self.abs_url)
            self.kavita_url = _host_for_docker(self.kavita_url)
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
