"""Allowed UI theme ids (library default + optional user override)."""

THEME_IDS = ("ocean", "ember", "forest", "dusk")
DEFAULT_THEME = "ocean"


def normalize_theme(raw: str | None, *, allow_null: bool = False) -> str | None:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None if allow_null else DEFAULT_THEME
    tid = raw.strip().lower()
    if tid in ("default", "library", "auto"):
        return None if allow_null else DEFAULT_THEME
    if tid not in THEME_IDS:
        return None if allow_null else DEFAULT_THEME
    return tid
