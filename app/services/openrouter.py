"""OpenRouter chat completions — LLM assist for Metadata Forge quarantine recovery.

When automated Metadata Forge fails to match/apply, an optional OpenRouter call
can suggest title/author/series/ASIN. High-confidence suggestions seed staging
hints and retry Metadata Forge once; otherwise the request quarantines as usual.

API key: Admin → Integrations (DB) with env ``OPENROUTER_API_KEY`` fallback.
Assist is **off by default** — no calls without enable + key.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

API_URL = "https://openrouter.ai/api/v1/chat/completions"
API_KEY_SETTING = "integrations.openrouter_api_key"
ENABLED_SETTING = "integrations.openrouter_enabled"
MODEL_SETTING = "integrations.openrouter_model"
CONFIDENCE_SETTING = "integrations.openrouter_confidence_threshold"

DEFAULT_MODEL = "openai/gpt-4o-mini"
DEFAULT_CONFIDENCE = 0.85
# Soft-fail budget — never block the download pipeline on LLM latency.
REQUEST_TIMEOUT_SECONDS = 45.0

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


@dataclass(frozen=True)
class BookIdentification:
    title: str
    author: str
    series: str = ""
    asin: str = ""
    confidence: float = 0.0
    rationale: str = ""


async def get_api_key() -> str:
    """Admin override first, then env ``OPENROUTER_API_KEY``."""
    from app.services import app_settings

    env_key = (getattr(settings, "openrouter_api_key", "") or "").strip()
    return (await app_settings.get_setting(API_KEY_SETTING, default=env_key)).strip()


async def is_enabled() -> bool:
    """True only when the toggle is on **and** an API key is available."""
    from app.services import instance_settings as inst

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
        return float(getattr(settings, "openrouter_confidence_threshold", DEFAULT_CONFIDENCE) or DEFAULT_CONFIDENCE)
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_CONFIDENCE
    return max(0.0, min(1.0, value))


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
    asin = str(data.get("asin") or data.get("ASIN") or "").strip().upper()
    if asin in ("", "NULL", "NONE", "N/A", "UNKNOWN"):
        asin = ""

    conf_raw = data.get("confidence", 0)
    try:
        confidence = float(conf_raw)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    rationale = str(data.get("rationale") or data.get("reason") or "").strip()[:500]
    return BookIdentification(
        title=title,
        author=author,
        series=series,
        asin=asin,
        confidence=confidence,
        rationale=rationale,
    )


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
        # Model sometimes wraps JSON in prose — grab the outermost object.
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None


def _build_prompt(context: dict[str, Any]) -> list[dict[str, str]]:
    system = (
        "You identify audiobooks/ebooks from incomplete download metadata. "
        "Reply with a single JSON object only (no markdown) using keys: "
        "title (string), author (string), series (string, may be empty), "
        "asin (Audible ASIN like B0XXXXXXXX or empty string), "
        "confidence (number 0-1), rationale (short string). "
        "Prefer well-known published titles. If unsure, lower confidence. "
        "Never invent an ASIN you are not reasonably sure about."
    )
    user = json.dumps(context, indent=2, ensure_ascii=False, default=str)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


async def identify_book(context: dict[str, Any]) -> BookIdentification | None:
    """Call OpenRouter; return identification or None on any soft-failure.

    Never raises for network/API/parse errors — caller should quarantine.
    """
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
        "messages": _build_prompt(context),
        "temperature": 0.1,
        "max_tokens": 600,
        "response_format": {"type": "json_object"},
    }

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            resp = await client.post(API_URL, headers=headers, json=payload)
    except httpx.TimeoutException:
        logger.warning("OpenRouter identify timed out after %.0fs", REQUEST_TIMEOUT_SECONDS)
        return None
    except httpx.HTTPError as e:
        logger.warning("OpenRouter identify HTTP error: %s", e)
        return None

    if resp.status_code >= 400:
        logger.warning(
            "OpenRouter identify failed %s: %s",
            resp.status_code,
            (resp.text or "")[:300],
        )
        return None

    try:
        body = resp.json()
    except json.JSONDecodeError:
        logger.warning("OpenRouter identify returned non-JSON body")
        return None

    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        logger.warning("OpenRouter identify missing choices: %s", str(body)[:300])
        return None

    if isinstance(content, list):
        # Some providers return content parts.
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text") or ""))
            elif isinstance(part, str):
                parts.append(part)
        content = "".join(parts)

    identification = parse_identification(content if isinstance(content, str) else None)
    if identification:
        logger.info(
            "OpenRouter identify: title=%r author=%r asin=%r confidence=%.2f",
            identification.title,
            identification.author,
            identification.asin or "",
            identification.confidence,
        )
    else:
        logger.warning("OpenRouter identify could not parse response: %s", str(content)[:300])
    return identification
