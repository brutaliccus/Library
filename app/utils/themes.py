"""Allowed UI theme ids (library default + optional user override)."""

PRESET_THEME_IDS = ("ocean", "ember", "forest", "dusk")
# "custom" is personal-only (3 user-picked colors live in the client).
THEME_IDS = (*PRESET_THEME_IDS, "custom")
DEFAULT_THEME = "ocean"


def normalize_theme(
    raw: str | None,
    *,
    allow_null: bool = False,
    allow_custom: bool = True,
) -> str | None:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None if allow_null else DEFAULT_THEME
    tid = raw.strip().lower()
    if tid in ("default", "library", "auto"):
        return None if allow_null else DEFAULT_THEME
    allowed = THEME_IDS if allow_custom else PRESET_THEME_IDS
    if tid not in allowed:
        return None if allow_null else DEFAULT_THEME
    return tid
