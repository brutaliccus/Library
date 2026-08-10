"""OpenRouter chat completions — LLM assist for forge / ebook quarantine recovery.

Features (all behind Admin → Integrations toggle, default off; no key → no calls):
- Metadata identify → seed hints → Metadata Forge retry
- Multi-book pack split proposals
- File prune (keep vs delete) suggestions
- ASIN recovery suggestions
- Ebook identify (mirror audiobook retry)
- Key usage / credit limit for Integrations UI

API key: Admin → Integrations (DB) with env ``OPENROUTER_API_KEY`` fallback.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

API_URL = "https://openrouter.ai/api/v1/chat/completions"
KEY_URL = "https://openrouter.ai/api/v1/key"
API_KEY_SETTING = "integrations.openrouter_api_key"
ENABLED_SETTING = "integrations.openrouter_enabled"
MODEL_SETTING = "integrations.openrouter_model"
CONFIDENCE_SETTING = "integrations.openrouter_confidence_threshold"

DEFAULT_MODEL = "openai/gpt-4o-mini"
DEFAULT_CONFIDENCE = 0.85
# Soft-fail budget — never block the download pipeline on LLM latency.
REQUEST_TIMEOUT_SECONDS = 45.0
# After credit/quota errors, treat assist as disabled until this TTL (or credits return).
CREDITS_SOFT_DISABLE_SECONDS = 3600.0

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)
_ASIN_RE = re.compile(r"^(?:B[\dA-Z]{9}|\d{10})$", re.IGNORECASE)
_CREDIT_ERROR_RE = re.compile(
    r"(?:credit|quota|balance|billing|payment|insufficient\s+funds|"
    r"out\s+of\s+credits|limit\s+(?:remaining\s+)?(?:is\s+)?(?:0|zero)|"
    r"exceeded\s+your\s+(?:credit\s+)?limit|requires?\s+more\s+credits|"
    r"can\s+only\s+afford|402\b)",
    re.IGNORECASE,
)

# Monotonic deadline: while set, ``is_enabled()`` is False (same as toggle off).
_credits_exhausted_until: float = 0.0


@dataclass(frozen=True)
class BookIdentification:
    title: str
    author: str
    series: str = ""
    asin: str = ""
    confidence: float = 0.0
    rationale: str = ""


@dataclass(frozen=True)
class BookSplitGroup:
    title: str
    author: str = ""
    paths: tuple[str, ...] = ()
    confidence: float = 0.0


@dataclass(frozen=True)
class BookSplitPlan:
    books: tuple[BookSplitGroup, ...]
    confidence: float = 0.0
    rationale: str = ""
    folder_based: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "books": [
                {
                    "title": b.title,
                    "author": b.author,
                    "paths": list(b.paths),
                    "confidence": b.confidence,
                }
                for b in self.books
            ],
            "confidence": self.confidence,
            "rationale": self.rationale,
            "folder_based": self.folder_based,
        }


@dataclass(frozen=True)
class FilePruneAction:
    path: str
    action: str  # keep | delete
    reason: str = ""
    safe_duplicate: bool = False


@dataclass(frozen=True)
class FilePrunePlan:
    actions: tuple[FilePruneAction, ...]
    confidence: float = 0.0
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "actions": [asdict(a) for a in self.actions],
            "confidence": self.confidence,
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class AsinSuggestion:
    asin: str
    title: str = ""
    author: str = ""
    confidence: float = 0.0
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class KeyUsage:
    """Fields from ``GET /api/v1/key`` (per-key usage + optional credit cap)."""

    label: str = ""
    usage: float | None = None
    usage_daily: float | None = None
    usage_weekly: float | None = None
    usage_monthly: float | None = None
    limit: float | None = None
    limit_remaining: float | None = None
    limit_reset: str | None = None
    is_free_tier: bool | None = None
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "usage": self.usage,
            "usageDaily": self.usage_daily,
            "usageWeekly": self.usage_weekly,
            "usageMonthly": self.usage_monthly,
            "limit": self.limit,
            "limitRemaining": self.limit_remaining,
            "limitReset": self.limit_reset,
            "isFreeTier": self.is_free_tier,
            "error": self.error or None,
            "creditsExhausted": credits_exhausted(),
        }


def credits_exhausted() -> bool:
    """True while credit/quota soft-disable is active (assist behaves as toggle-off)."""
    return time.monotonic() < _credits_exhausted_until


def mark_credits_exhausted(reason: str = "") -> None:
    """Soft-disable LLM assist — same as toggle off until TTL or credits return."""
    global _credits_exhausted_until
    _credits_exhausted_until = time.monotonic() + CREDITS_SOFT_DISABLE_SECONDS
    logger.info(
        "OpenRouter credits/quota exhausted — LLM assist skipped "
        "(same as disabled) for %.0fs%s",
        CREDITS_SOFT_DISABLE_SECONDS,
        f": {reason[:160]}" if reason else "",
    )


def clear_credits_exhausted() -> None:
    """Clear soft-disable after successful usage/chat shows credits available."""
    global _credits_exhausted_until
    if _credits_exhausted_until:
        logger.info("OpenRouter credits available again — LLM assist re-enabled")
    _credits_exhausted_until = 0.0


def is_credit_error(status_code: int, body: str = "") -> bool:
    """Detect payment/credit/quota exhaustion (not ordinary 5xx/parse errors)."""
    if status_code == 402:
        return True
    text = body or ""
    if _CREDIT_ERROR_RE.search(text):
        return True
    # 429 alone is often rate-limit; only treat as credits when the body says so.
    if status_code == 429 and _CREDIT_ERROR_RE.search(text):
        return True
    return False


def note_usage_credits(usage: KeyUsage) -> None:
    """Update soft-disable from ``GET /api/v1/key`` limit_remaining when present."""
    if usage.error:
        return
    remaining = usage.limit_remaining
    # null limit_remaining ⇒ unlimited / no per-key cap — do not soft-disable.
    if remaining is None:
        if credits_exhausted():
            # Usage succeeded; allow a re-try path to clear stale disable.
            clear_credits_exhausted()
        return
    if remaining <= 0:
        mark_credits_exhausted(f"limit_remaining={remaining}")
    else:
        clear_credits_exhausted()


async def get_api_key() -> str:
    """Admin override first, then env ``OPENROUTER_API_KEY``."""
    from app.services import app_settings

    env_key = (getattr(settings, "openrouter_api_key", "") or "").strip()
    return (await app_settings.get_setting(API_KEY_SETTING, default=env_key)).strip()


async def is_enabled() -> bool:
    """True only when toggle on, API key present, and credits not soft-exhausted.

    Credit exhaustion uses the **same code path as toggle off / no key**: callers
    skip LLM and continue the normal non-LLM pipeline (no special quarantine).
    """
    from app.services import instance_settings as inst

    if credits_exhausted():
        return False
    if not await inst.get_effective_bool(ENABLED_SETTING, False):
        return False
    return bool(await get_api_key())


async def get_model() -> str:
    from app.services import instance_settings as inst

    raw = (await inst.get_effective(MODEL_SETTING)).strip()
    return raw or DEFAULT_MODEL


async def get_confidence_threshold() -> float:
    from app.services import instance_settings as inst

    raw = (await inst.get_effective(CONFIDENCE_SETTING)).strip()
    if not raw:
        return float(
            getattr(settings, "openrouter_confidence_threshold", DEFAULT_CONFIDENCE)
            or DEFAULT_CONFIDENCE
        )
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_CONFIDENCE
    return max(0.0, min(1.0, value))


def _clamp_confidence(raw: Any) -> float:
    try:
        confidence = float(raw)
    except (TypeError, ValueError):
        confidence = 0.0
    return max(0.0, min(1.0, confidence))


def _normalize_asin(raw: Any) -> str:
    asin = str(raw or "").strip().upper()
    if asin in ("", "NULL", "NONE", "N/A", "UNKNOWN"):
        return ""
    if not _ASIN_RE.match(asin):
        return ""
    return asin


def _parse_json_object(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
    if not text:
        return None
    fence = _JSON_FENCE_RE.search(text)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None


def _message_content_to_str(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text") or ""))
            elif isinstance(part, str):
                parts.append(part)
        return "".join(parts)
    return None


async def _chat_json(
    system: str,
    context: dict[str, Any],
    *,
    max_tokens: int = 900,
    log_label: str = "assist",
) -> dict[str, Any] | None:
    """POST chat/completions expecting a JSON object. Soft-fails to None."""
    if not await is_enabled():
        return None

    api_key = await get_api_key()
    if not api_key:
        return None

    model = await get_model()
    app_url = (getattr(settings, "app_url", "") or "https://library.example.com").strip()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": app_url,
        "X-Title": "Library",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": json.dumps(context, indent=2, ensure_ascii=False, default=str),
            },
        ],
        "temperature": 0.1,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            resp = await client.post(API_URL, headers=headers, json=payload)
    except httpx.TimeoutException:
        logger.warning("OpenRouter %s timed out after %.0fs", log_label, REQUEST_TIMEOUT_SECONDS)
        return None
    except httpx.HTTPError as e:
        logger.warning("OpenRouter %s HTTP error: %s", log_label, e)
        return None

    if resp.status_code >= 400:
        body_text = (resp.text or "")[:300]
        # Never log Authorization / key material — body only.
        logger.warning(
            "OpenRouter %s failed %s: %s",
            log_label,
            resp.status_code,
            body_text,
        )
        if is_credit_error(resp.status_code, body_text):
            mark_credits_exhausted(f"HTTP {resp.status_code}")
        return None

    try:
        body = resp.json()
    except json.JSONDecodeError:
        logger.warning("OpenRouter %s returned non-JSON body", log_label)
        return None

    # Some error payloads return 200 with an error object.
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        err_msg = str(err.get("message") or err.get("code") or "")
        if is_credit_error(int(err.get("code") or 0) or 402, err_msg):
            mark_credits_exhausted(err_msg)
            return None

    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        logger.warning("OpenRouter %s missing choices: %s", log_label, str(body)[:300])
        return None

    text = _message_content_to_str(content)
    data = _parse_json_object(text or "")
    if not data:
        logger.warning("OpenRouter %s could not parse response: %s", log_label, str(content)[:300])
        return None

    # A successful completion proves credits work — clear soft-disable.
    clear_credits_exhausted()
    return data


def parse_identification(raw: str | dict[str, Any] | None) -> BookIdentification | None:
    """Parse model JSON into a BookIdentification, or None if unusable."""
    data: dict[str, Any] | None
    if isinstance(raw, dict):
        data = raw
    elif isinstance(raw, str):
        data = _parse_json_object(raw)
    else:
        return None
    if not data:
        return None

    title = str(data.get("title") or "").strip()
    author = str(data.get("author") or "").strip()
    if not title and not author:
        return None

    series = str(data.get("series") or "").strip()
    asin = _normalize_asin(data.get("asin") or data.get("ASIN"))
    confidence = _clamp_confidence(data.get("confidence", 0))
    rationale = str(data.get("rationale") or data.get("reason") or "").strip()[:500]
    return BookIdentification(
        title=title,
        author=author,
        series=series,
        asin=asin,
        confidence=confidence,
        rationale=rationale,
    )


def parse_split_plan(raw: str | dict[str, Any] | None) -> BookSplitPlan | None:
    data: dict[str, Any] | None
    if isinstance(raw, dict):
        data = raw
    elif isinstance(raw, str):
        data = _parse_json_object(raw)
    else:
        return None
    if not data:
        return None

    books_raw = data.get("books") or data.get("groups") or []
    if not isinstance(books_raw, list) or len(books_raw) < 2:
        return None

    books: list[BookSplitGroup] = []
    for item in books_raw:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        author = str(item.get("author") or "").strip()
        paths_raw = item.get("paths") or item.get("files") or item.get("folders") or []
        paths: list[str] = []
        if isinstance(paths_raw, list):
            for p in paths_raw:
                s = str(p or "").strip().replace("\\", "/")
                if s and ".." not in s.split("/"):
                    paths.append(s.lstrip("/"))
        conf = _clamp_confidence(item.get("confidence", data.get("confidence", 0)))
        if not paths:
            continue
        books.append(
            BookSplitGroup(
                title=title,
                author=author,
                paths=tuple(paths),
                confidence=conf,
            )
        )

    if len(books) < 2:
        return None

    overall = _clamp_confidence(data.get("confidence", 0))
    if overall <= 0:
        overall = min(b.confidence for b in books) if books else 0.0
    rationale = str(data.get("rationale") or data.get("reason") or "").strip()[:500]
    folder_based = bool(data.get("folder_based") or data.get("folderBased"))
    # Heuristic: every path is a single top-level folder name (no slash) → folder-based.
    if not folder_based:
        folder_based = all(
            "/" not in p and "\\" not in p for b in books for p in b.paths
        )
    return BookSplitPlan(
        books=tuple(books),
        confidence=overall,
        rationale=rationale,
        folder_based=folder_based,
    )


def parse_prune_plan(raw: str | dict[str, Any] | None) -> FilePrunePlan | None:
    data: dict[str, Any] | None
    if isinstance(raw, dict):
        data = raw
    elif isinstance(raw, str):
        data = _parse_json_object(raw)
    else:
        return None
    if not data:
        return None

    actions_raw = data.get("actions") or data.get("files") or []
    if not isinstance(actions_raw, list) or not actions_raw:
        return None

    actions: list[FilePruneAction] = []
    for item in actions_raw:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip().replace("\\", "/").lstrip("/")
        if not path or ".." in path.split("/"):
            continue
        action = str(item.get("action") or item.get("decision") or "").strip().lower()
        if action not in ("keep", "delete"):
            continue
        reason = str(item.get("reason") or "").strip()[:300]
        safe = bool(item.get("safe_duplicate") or item.get("safeDuplicate"))
        if action == "delete" and (
            "duplicate" in reason.lower()
            or "sample" in reason.lower()
            or "prefer" in reason.lower()
        ):
            safe = True
        actions.append(
            FilePruneAction(
                path=path,
                action=action,
                reason=reason,
                safe_duplicate=safe and action == "delete",
            )
        )

    if not actions:
        return None

    return FilePrunePlan(
        actions=tuple(actions),
        confidence=_clamp_confidence(data.get("confidence", 0)),
        rationale=str(data.get("rationale") or data.get("reason") or "").strip()[:500],
    )


def parse_asin_suggestion(raw: str | dict[str, Any] | None) -> AsinSuggestion | None:
    data: dict[str, Any] | None
    if isinstance(raw, dict):
        data = raw
    elif isinstance(raw, str):
        data = _parse_json_object(raw)
    else:
        return None
    if not data:
        return None

    asin = _normalize_asin(data.get("asin") or data.get("ASIN"))
    if not asin:
        return None
    return AsinSuggestion(
        asin=asin,
        title=str(data.get("title") or "").strip(),
        author=str(data.get("author") or "").strip(),
        confidence=_clamp_confidence(data.get("confidence", 0)),
        rationale=str(data.get("rationale") or data.get("reason") or "").strip()[:500],
    )


async def identify_book(context: dict[str, Any]) -> BookIdentification | None:
    """Call OpenRouter; return identification or None on any soft-failure."""
    system = (
        "You identify audiobooks/ebooks from incomplete download metadata. "
        "Reply with a single JSON object only (no markdown) using keys: "
        "title (string), author (string), series (string, may be empty), "
        "asin (Audible ASIN like B0XXXXXXXX or empty string), "
        "confidence (number 0-1), rationale (short string). "
        "Prefer well-known published titles. If unsure, lower confidence. "
        "Never invent an ASIN you are not reasonably sure about. "
        "IMPORTANT: scene rips often put the narrator in the author/artist tag "
        "and leave narrator empty. When title + duration strongly match a known "
        "edition, do NOT lower confidence just because the file author looks like "
        "a narrator — return the real book author (e.g. Michael Crichton) and "
        "treat the tag author as the narrator."
    )
    data = await _chat_json(system, context, max_tokens=600, log_label="identify")
    identification = parse_identification(data)
    if identification:
        logger.info(
            "OpenRouter identify: title=%r author=%r asin=%r confidence=%.2f",
            identification.title,
            identification.author,
            identification.asin or "",
            identification.confidence,
        )
    return identification


async def propose_multi_book_split(context: dict[str, Any]) -> BookSplitPlan | None:
    """Ask the model to split a multi-book pack into N books with path groups."""
    system = (
        "You analyze a downloaded audiobook torrent that may contain multiple books. "
        "Reply with a single JSON object only using keys: "
        "books (array of {title, author, paths, confidence}), "
        "confidence (0-1 overall), rationale (short), "
        "folder_based (boolean — true when each book maps cleanly to top-level folders). "
        "paths must be relative paths from the staging root (folders or files that belong "
        "to that book). Prefer folder groups when layout is clear. "
        "When release_files / release_groups are provided (AudioBookBay or debrid file "
        "list), treat those paths as ground truth for which basenames belong together — "
        "even if staging is flat with opaque names like MISTBORN0101P01.mp3. "
        "Decide carefully whether files are chapters/parts of ONE book vs SEPARATE books: "
        "multiple complete .m4b files with different titles are usually one book each; "
        "filenames with chapter/track/part/pt/(N of M) usually belong to one book; "
        "download titles containing full series, complete series, box set, bundle, "
        "omnibus, or books 1-N strongly suggest a multi-book pack — split those. "
        "If flat_multi_book.likely is true, prefer one book per listed file path. "
        "If this is a single book with dual formats (mp3/ + AAC/), return confidence 0 "
        "and books with fewer than 2 entries conceptually — use books: [] and "
        "rationale explaining dual-format. "
        "If unsure how to split, lower confidence."
    )
    data = await _chat_json(system, context, max_tokens=1200, log_label="multi-book")
    plan = parse_split_plan(data)
    if plan:
        logger.info(
            "OpenRouter multi-book: %d books confidence=%.2f folder_based=%s",
            len(plan.books),
            plan.confidence,
            plan.folder_based,
        )
    return plan


async def propose_file_prune(context: dict[str, Any]) -> FilePrunePlan | None:
    """Suggest keep vs delete for staging files (prefer AAC over mp3, drop samples)."""
    system = (
        "You review audiobook staging files and suggest keep vs delete. "
        "Reply with a single JSON object only using keys: "
        "actions (array of {path, action: keep|delete, reason, safe_duplicate: bool}), "
        "confidence (0-1), rationale (short). "
        "Prefer higher-quality formats (m4b/m4a/aac over mp3) when duplicates exist. "
        "Mark safe_duplicate=true only for clear format duplicates or sample/preview clips "
        "when another full copy remains. Never delete the sole remaining audio for a book. "
        "Paths are relative to staging. Only list files that need a decision."
    )
    data = await _chat_json(system, context, max_tokens=1200, log_label="file-prune")
    plan = parse_prune_plan(data)
    if plan:
        deletes = sum(1 for a in plan.actions if a.action == "delete")
        logger.info(
            "OpenRouter file-prune: %d actions (%d delete) confidence=%.2f",
            len(plan.actions),
            deletes,
            plan.confidence,
        )
    return plan


async def suggest_asin(context: dict[str, Any]) -> AsinSuggestion | None:
    """Suggest an Audible ASIN when title/author are known but ASIN is missing."""
    system = (
        "You recover Audible ASINs for audiobooks when title and author are known. "
        "Reply with a single JSON object only using keys: "
        "asin (B0XXXXXXXX or 10-digit ISBN-like Audible id, or empty), "
        "title, author, confidence (0-1), rationale (short). "
        "Only return an ASIN you are reasonably sure matches this edition/narration. "
        "If unsure, set asin to empty and low confidence."
    )
    data = await _chat_json(system, context, max_tokens=400, log_label="asin")
    suggestion = parse_asin_suggestion(data)
    if suggestion:
        logger.info(
            "OpenRouter ASIN suggest: asin=%s confidence=%.2f",
            suggestion.asin,
            suggestion.confidence,
        )
    return suggestion


async def fetch_key_usage() -> KeyUsage:
    """``GET /api/v1/key`` — per-key usage and optional credit limit. Soft-fails."""
    api_key = await get_api_key()
    if not api_key:
        return KeyUsage(error="No API key configured")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(KEY_URL, headers=headers)
    except httpx.TimeoutException:
        return KeyUsage(error="Usage request timed out")
    except httpx.HTTPError as e:
        logger.warning("OpenRouter key usage HTTP error: %s", e)
        return KeyUsage(error="Usage request failed")

    if resp.status_code >= 400:
        body_text = (resp.text or "")[:200]
        logger.warning(
            "OpenRouter key usage failed %s: %s",
            resp.status_code,
            body_text,
        )
        if is_credit_error(resp.status_code, body_text):
            mark_credits_exhausted(f"usage HTTP {resp.status_code}")
        return KeyUsage(error=f"OpenRouter returned {resp.status_code}")

    try:
        body = resp.json()
    except json.JSONDecodeError:
        return KeyUsage(error="Invalid usage response")

    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return KeyUsage(error="Unexpected usage payload")

    def _num(key: str) -> float | None:
        val = data.get(key)
        if val is None:
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    # OpenRouter returns a masked label like sk-or-v1-au7...890 — safe to show.
    label = str(data.get("label") or "").strip()
    if len(label) > 40:
        label = label[:20] + "…" + label[-8:]

    usage = KeyUsage(
        label=label,
        usage=_num("usage"),
        usage_daily=_num("usage_daily"),
        usage_weekly=_num("usage_weekly"),
        usage_monthly=_num("usage_monthly"),
        limit=_num("limit"),
        limit_remaining=_num("limit_remaining"),
        limit_reset=str(data.get("limit_reset") or "").strip() or None,
        is_free_tier=bool(data["is_free_tier"]) if "is_free_tier" in data else None,
    )
    note_usage_credits(usage)
    return usage
