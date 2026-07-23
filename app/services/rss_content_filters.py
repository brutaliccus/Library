"""Shared RSS/indexer content filters for adult, music, movie, and tiny-audio rejection.

Used by Knaben RSS/API, Torznab parse, cache upsert, and periodic prune so junk
never reaches Real-Debrid / TorBox preload.
"""
from __future__ import annotations

import re

# Audio packages under this size are almost always music singles / samples, not
# audiobooks. Applied to audiobook (and unknown-as-audio) media before cache/debrid.
SIZE_AUDIO_MUSIC_MAX = 10 * 1024 * 1024  # 10 MB

# Adult / porn — site names, scene tags, and clear NSFW title patterns.
# Avoid bare "adult" (Young Adult) and bare "sex" inside longer legit words.
_ADULT_TITLE = re.compile(
    r"(?:"
    # Major tube / studio sites and brands
    r"\b(?:"
    r"onlyfans|brazzers|bellesa|pornhub|xvideos|xhamster|xnxx|redtube|youporn|"
    r"spankbang|porntrex|xmoviesforyou|adulttime|reality\s*kings|bangbros|"
    r"naughty\s*america|digital\s*playground|evil\s*angel|vixen|blacked|"
    r"tushy|deeper|slayed|bellesafilms?|fake\s*taxi|fake\s*agent|"
    r"pornhd|hqporner|eporner|xvideos2?|chaturbate|manyvids|fansly|"
    r"motherless|imagefap|literotica|audiotits|girlfriend\s*experience|"
    r"javlibrary|javhd|r18\.com|caribbeancom|1pondo|heyzo|"
    r"pornhub\.com|xvideos\.com|xhamster\.com"
    r")\b"
    r"|"
    # Explicit tokens (word-boundary safe)
    r"\b(?:xxx+|xxxx+|porn|porno|hentai|nsfw|jav\b|censored\s+jav|uncensored\s+jav|"
    r"erotic\s+audiobook|erotica\s+audio|erotic\s+audio|sexy\s+stories|"
    r"adult\s+audio(?:book)?|nsfw\s+audio|gone\s*wild|rule\s*34|"
    r"creampie|gangbang|blowjob|handjob|deepthroat|facesitting|"
    r"step(?:mom|sis|brother|dad|daughter)|milf\b|teen\s+porn|"
    r"lesbian\s+porn|gay\s+porn|bbw\s+porn|anal\s+sex|"
    r"hardcore\s+porn|softcore\s+porn|pornstar|camgirl|"
    r"nude\s+model|naked\s+women|sex\s+tape|sex\s+tape|"
    r"pussy|cock\s+suck|cumshot|bukkake|bdsm\s+porn|"
    r"fetish\s+porn|xxx\s+video|xxx\s+dvd"
    r")\b"
    r"|"
    # Filename / tagging styles common on adult torrents
    r"\[(?:xxx|porn|adult|nsfw|hentai)\]"
    r"|\((?:xxx|porn|adult|nsfw|hentai)\)"
    r")"
    ,
    re.IGNORECASE,
)

# Music — albums, discographies, bitrate packs (not full-cast / narrated audiobooks).
_MUSIC_TITLE = re.compile(
    r"(?:"
    r"\b(?:"
    r"discography|unplugged|vinyl\s+rip|bootleg|mixtape|"
    r"cd\s*rip|web\s*rip\s+flac|album\s+rip|"
    r"greatest\s+hits|live\s+at\s+|concert\s+recording|"
    r"studio\s+album|full\s+album|complete\s+album|"
    r"single\s+(?:flac|mp3|aac|wav|alac)|"
    r"(?:flac|mp3|aac|wav|alac)\s+(?:album|collection|discography|single)|"
    r"(?:album|collection|discography)\s+(?:flac|mp3|aac|wav|alac)|"
    r"(?:ost|soundtrack)\s+(?:flac|mp3|aac|wav)|"
    r"\d{2,3}\s*kbps|"
    r"320\s*kbps|256\s*kbps|192\s*kbps|128\s*kbps|"
    r"v0\s*vbr|v2\s*vbr|lossy\s+web|"
    r"hip[- ]?hop\s+album|rap\s+album|rock\s+album|"
    r"spotify\s+rip|tidal\s+rip|deezer\s+rip|"
    r"va\s*[-–]\s*.+\s+flac"  # Various Artists compilations
    r")\b"
    r"|"
    r"\[(?:flac|mp3|aac|album|ost|soundtrack)\]"
    r"|\.(?:flac|alac)\b"  # container tags in titles (audiobooks use m4b/mp3)
    r")"
    ,
    re.IGNORECASE,
)

# Movies / TV — video encode tags and scene naming.
_MOVIE_TV_TITLE = re.compile(
    r"(?:"
    r"\.(?:mp4|mkv|avi|wmv|flv|webm|ts|m2ts|mov|mpg|mpeg)\b"
    r"|\b(?:"
    r"1080p|720p|480p|2160p|4k|uhd|hdr10|dolby\s*vision|"
    r"x264|x265|h\.?264|h\.?265|hevc|xvid|divx|mpeg2|"
    r"bluray|blu-ray|brrip|bdrip|webrip|web-dl|hdtv|dvdrip|hdrip|"
    r"remux|truehd|dts-hd|atmos|aac5\.1|"
    r"\d{3,4}x\d{3,4}|"
    r"s\d{1,2}e\d{1,2}|season\s*\d+\s*(?:episode|ep)?|"
    r"complete\s+series|tv\s+mini\s+series|mini\s+series|"
    r"feature\s+film|full\s+movie|cam\s+rip|hdcam|ts\s+rip|"
    r"proper\s+bluray|repack\s+bluray"
    r")\b"
    r")"
    ,
    re.IGNORECASE,
)

# Software / cracks (keep with non-book gate).
_SOFTWARE_TITLE = re.compile(
    r"\.(?:exe|msi|iso)\b"
    r"|\b(?:pre-?activated|crack|keygen|ftuapps|nulled)\b",
    re.IGNORECASE,
)

# Positive audiobook signals — if present, don't treat FLAC/bitrate as music
# (some legitimate audiobooks ship as multi-file FLAC).
_AUDIOBOOK_SAFE = re.compile(
    r"\b(?:audiobook|m4b|unabridged|abridged|narrated\s+by|read\s+by|full[- ]cast)\b",
    re.IGNORECASE,
)


def title_looks_adult(title: str) -> bool:
    return bool(_ADULT_TITLE.search(title or ""))


def title_looks_like_music(title: str) -> bool:
    t = title or ""
    if _AUDIOBOOK_SAFE.search(t):
        return False
    return bool(_MUSIC_TITLE.search(t))


def title_looks_like_movie_or_tv(title: str) -> bool:
    t = title or ""
    if _AUDIOBOOK_SAFE.search(t):
        # Still reject obvious video containers even on "audiobook" spam titles
        if re.search(r"\.(?:mp4|mkv|avi|wmv|flv|webm|ts|m2ts)\b", t, re.I):
            return True
        if re.search(r"\b(?:1080p|720p|2160p|bluray|web-dl|webrip)\b", t, re.I):
            return True
        return False
    return bool(_MOVIE_TV_TITLE.search(t))


def title_looks_like_software(title: str) -> bool:
    return bool(_SOFTWARE_TITLE.search(title or ""))


def title_is_non_book(title: str) -> bool:
    """True when the title clearly is adult, music, movie/TV, or software."""
    t = title or ""
    if not t.strip():
        return False
    return (
        title_looks_adult(t)
        or title_looks_like_music(t)
        or title_looks_like_movie_or_tv(t)
        or title_looks_like_software(t)
    )


def is_too_small_for_audiobook(size_bytes: int | None, media_type: str | None = None) -> bool:
    """Reject tiny audio packages as music (not stored / not sent to debrid).

    Unknown size (0/None) is allowed through — many RSS items omit size.
    """
    if not size_bytes or size_bytes <= 0:
        return False
    mt = (media_type or "").lower()
    if mt and mt not in ("audiobook", "unknown", "audio", ""):
        return False
    return size_bytes < SIZE_AUDIO_MUSIC_MAX
