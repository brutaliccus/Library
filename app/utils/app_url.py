"""Normalize public App URL values (APP_URL / config.app_url)."""

from __future__ import annotations

import re

_SCHEME_RE = re.compile(r"^(https?)://", re.IGNORECASE)
# Collapse accidental duplicated schemes: https://https://host -> https://host
_DUP_SCHEME_RE = re.compile(r"^(https?://)+(https?://)", re.IGNORECASE)


def normalize_app_url(raw: str | None, *, strip_trailing_slash: bool = True) -> str:
    """Clean a public App URL for storage and link building.

    - Strips surrounding whitespace
    - Collapses duplicated http(s) schemes (``https://https://x`` -> ``https://x``)
    - Ensures an http/https scheme (defaults to https when missing)
    - Optionally strips a single trailing slash (default True; matches invite/link helpers)
    """
    value = (raw or "").strip()
    if not value:
        return ""

    # Repeated scheme prefixes from paste / env mistakes.
    while True:
        m = _DUP_SCHEME_RE.match(value)
        if not m:
            break
        # Keep the last scheme token matched by group 2.
        value = m.group(2) + value[m.end() :]

    # Strip any remaining stacked schemes left as bare text.
    while True:
        lower = value.lower()
        if lower.startswith("https://https://") or lower.startswith("http://http://"):
            # Drop the first scheme entirely.
            value = value.split("://", 1)[1]
            continue
        if lower.startswith("https://http://"):
            value = "http://" + value[len("https://http://") :]
            continue
        if lower.startswith("http://https://"):
            value = "https://" + value[len("http://https://") :]
            continue
        break

    if not _SCHEME_RE.match(value):
        # Bare host or host:port - prefer https for public URLs.
        value = f"https://{value.lstrip('/')}"

    if strip_trailing_slash and len(value) > len("https://x") and value.endswith("/"):
        value = value.rstrip("/")

    return value