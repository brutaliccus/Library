"""OpenRouter LLM metadata assist — parse + forge retry/quarantine paths."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import openrouter
from app.services.forge_pipeline import (
    _apply_metadata_forge,
    _identity_fields_corroborate,
    _llm_metadata_assist_retry,
    collect_staging_llm_context,
    seed_staging_metadata_hints,
)
from app.services.openrouter import BookIdentification, parse_identification


def test_parse_identification_json():
    hit = parse_identification(
        '{"title":"The Gunslinger","author":"Stephen King","series":"The Dark Tower",'
        '"asin":"B019NNU7XE","confidence":0.92,"rationale":"Clear match"}'
    )
    assert hit is not None
    assert hit.title == "The Gunslinger"
    assert hit.author == "Stephen King"
    assert hit.series == "The Dark Tower"
    assert hit.asin == "B019NNU7XE"
    assert hit.confidence == pytest.approx(0.92)


def test_parse_identification_fenced_and_clamped():
    hit = parse_identification(
        'Here you go:\n```json\n{"title":"Dune","author":"Frank Herbert",'
        '"confidence":1.5,"asin":"none"}\n```'
    )
    assert hit is not None
    assert hit.title == "Dune"
    assert hit.confidence == 1.0
    assert hit.asin == ""


def test_parse_identification_rejects_empty():
    assert parse_identification("{}") is None
    assert parse_identification('{"confidence":0.9}') is None
    assert parse_identification(None) is None


def test_seed_hints_force_overwrites_and_asin(tmp_path: Path):
    staging = tmp_path / "req_1"
    staging.mkdir()
    (staging / "book.m4b").write_bytes(b"x")
    (staging / "metadata.json").write_text(
        json.dumps({"title": "Wrong Title", "author": "Wrong"}),
        encoding="utf-8",
    )
    seed_staging_metadata_hints(
        staging,
        title="The Gunslinger",
        author="Stephen King",
        asin="B019NNU7XE",
        series="The Dark Tower",
        force=True,
    )
    meta = json.loads((staging / "metadata.json").read_text(encoding="utf-8"))
    assert meta["title"] == "The Gunslinger"
    assert meta["author"] == "Stephen King"
    assert meta["asin"] == "B019NNU7XE"
    assert meta["series"] == "The Dark Tower"


def test_collect_staging_llm_context(tmp_path: Path):
    staging = tmp_path / "req_2"
    staging.mkdir()
    audio = staging / "ch01.mp3"
    audio.write_bytes(b"abc")
    (staging / "metadata.json").write_text(
        json.dumps({"title": "Partial", "asin": "B00TESTASIN"}),
        encoding="utf-8",
    )
    ctx = collect_staging_llm_context(staging)
    assert ctx["files"]
    assert ctx["files"][0]["path"] == "ch01.mp3"
    assert ctx["files"][0]["size"] == 3
    assert ctx["partial_tags"]["title"] == "Partial"


def test_llm_assist_high_confidence_retries_ok(tmp_path: Path, monkeypatch):
    staging = tmp_path / "req_hi"
    staging.mkdir()
    (staging / "book.m4b").write_bytes(b"x")

    monkeypatch.setattr(openrouter, "is_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(openrouter, "get_confidence_threshold", AsyncMock(return_value=0.85))
    monkeypatch.setattr(
        openrouter,
        "identify_book",
        AsyncMock(
            return_value=BookIdentification(
                title="The Gunslinger",
                author="Stephen King",
                asin="B019NNU7XE",
                confidence=0.91,
                rationale="Audible match",
            )
        ),
    )

    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.execute = AsyncMock(
        return_value=MagicMock(
            scalar_one_or_none=MagicMock(
                return_value=MagicMock(
                    title="Gunslinger",
                    author="King",
                    media_type="audiobook",
                )
            )
        )
    )

    pipeline = MagicMock()
    pipeline._update_status = AsyncMock()
    pipeline._is_cancelled = AsyncMock(return_value=False)

    async def _run():
        with (
            patch("app.services.forge_pipeline.async_session", return_value=session),
            patch("app.services.forge_pipeline._pipeline", return_value=pipeline),
            patch(
                "app.services.forge_pipeline._run_metadata_forge_once",
                new=AsyncMock(return_value=("ok", "")),
            ) as run_once,
        ):
            status, reason = await _llm_metadata_assist_retry(
                42,
                staging=staging,
                user_id=1,
                prior_reason="score too low",
            )
            return status, reason, run_once

    status, reason, run_once = asyncio.run(_run())
    assert status == "ok"
    assert reason == ""
    run_once.assert_awaited_once()
    meta = json.loads((staging / "metadata.json").read_text(encoding="utf-8"))
    assert meta["title"] == "The Gunslinger"
    assert meta["asin"] == "B019NNU7XE"


def test_llm_assist_low_confidence_quarantines(tmp_path: Path, monkeypatch):
    staging = tmp_path / "req_lo"
    staging.mkdir()
    (staging / "book.m4b").write_bytes(b"x")

    monkeypatch.setattr(openrouter, "is_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(openrouter, "get_confidence_threshold", AsyncMock(return_value=0.85))
    monkeypatch.setattr(
        openrouter,
        "identify_book",
        AsyncMock(
            return_value=BookIdentification(
                title="Maybe This",
                author="Someone",
                confidence=0.4,
                rationale="Guess",
            )
        ),
    )

    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.execute = AsyncMock(
        return_value=MagicMock(
            scalar_one_or_none=MagicMock(
                return_value=MagicMock(title="x", author="y", media_type="audiobook")
            )
        )
    )
    pipeline = MagicMock()
    pipeline._update_status = AsyncMock()

    async def _run():
        with (
            patch("app.services.forge_pipeline.async_session", return_value=session),
            patch("app.services.forge_pipeline._pipeline", return_value=pipeline),
            patch(
                "app.services.forge_pipeline._run_metadata_forge_once",
                new=AsyncMock(return_value=("ok", "")),
            ) as run_once,
        ):
            status, reason = await _llm_metadata_assist_retry(
                7,
                staging=staging,
                user_id=1,
                prior_reason="no match",
            )
            return status, reason, run_once

    status, reason, run_once = asyncio.run(_run())
    assert status == "fail"
    assert "0.40" in reason
    assert "below 0.85" in reason
    run_once.assert_not_awaited()
    assert not (staging / "metadata.json").exists()


def test_apply_metadata_forge_assist_then_quarantine(tmp_path: Path):
    staging = tmp_path / "req_q"
    staging.mkdir()

    async def _run():
        with (
            patch(
                "app.services.forge_pipeline._run_metadata_forge_once",
                new=AsyncMock(return_value=("fail", "no write evidence")),
            ),
            patch(
                "app.services.forge_pipeline._llm_metadata_assist_retry",
                new=AsyncMock(return_value=("fail", "no write evidence | AI assist failed")),
            ) as assist,
            patch(
                "app.services.forge_pipeline._set_quarantine",
                new=AsyncMock(),
            ) as quarantine,
        ):
            ok = await _apply_metadata_forge(
                9,
                staging=staging,
                user_id=1,
                allow_llm_assist=True,
            )
            return ok, assist, quarantine

    ok, assist, quarantine = asyncio.run(_run())
    assert ok is False
    assist.assert_awaited_once()
    quarantine.assert_awaited_once()
    assert "AI assist" in quarantine.await_args.args[1]


def test_apply_metadata_forge_skips_assist_when_disabled_flag(tmp_path: Path):
    staging = tmp_path / "req_no"
    staging.mkdir()

    async def _run():
        with (
            patch(
                "app.services.forge_pipeline._run_metadata_forge_once",
                new=AsyncMock(return_value=("fail", "score low")),
            ),
            patch(
                "app.services.forge_pipeline._llm_metadata_assist_retry",
                new=AsyncMock(),
            ) as assist,
            patch(
                "app.services.forge_pipeline._set_quarantine",
                new=AsyncMock(),
            ) as quarantine,
        ):
            ok = await _apply_metadata_forge(
                9,
                staging=staging,
                user_id=1,
                allow_llm_assist=False,
            )
            return ok, assist, quarantine

    ok, assist, quarantine = asyncio.run(_run())
    assert ok is False
    assist.assert_not_awaited()
    quarantine.assert_awaited_once_with(9, "score low", staging)


def test_identify_book_soft_fails_on_http_error(monkeypatch):
    monkeypatch.setattr(openrouter, "is_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(openrouter, "get_api_key", AsyncMock(return_value="sk-test"))
    monkeypatch.setattr(openrouter, "get_model", AsyncMock(return_value="openai/gpt-4o-mini"))

    mock_resp = MagicMock()
    mock_resp.status_code = 502
    mock_resp.text = "bad gateway"

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=mock_resp)

    async def _run():
        with patch("app.services.openrouter.httpx.AsyncClient", return_value=mock_client):
            return await openrouter.identify_book({"request_title": "Dune"})

    assert asyncio.run(_run()) is None


def test_identity_fields_corroborate_honeybites():
    hit = BookIdentification(
        title="Honeybites",
        author="I.S. Belle",
        series="Honeybloods",
        confidence=0.92,
    )
    assert _identity_fields_corroborate(
        hit,
        {
            "title": "Honeybites, Book 2",
            "authors": ["I.S. Belle"],
            "series": "Honeybloods",
            "score": 0.46,
        },
    )
    assert not _identity_fields_corroborate(
        hit,
        {
            "title": "Honeybloods",
            "authors": ["Someone Else"],
            "score": 0.46,
        },
    )


def test_llm_assist_corroborates_when_forge_retry_still_fails(tmp_path: Path, monkeypatch):
    """Missing-duration ~46% Forge scores should still apply after LLM confirm."""
    staging = tmp_path / "req_honey"
    staging.mkdir()
    (staging / "book.m4b").write_bytes(b"x")

    monkeypatch.setattr(openrouter, "is_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(openrouter, "get_confidence_threshold", AsyncMock(return_value=0.85))
    monkeypatch.setattr(
        openrouter,
        "identify_book",
        AsyncMock(
            return_value=BookIdentification(
                title="Honeybites",
                author="I.S. Belle",
                series="Honeybloods",
                confidence=0.93,
                rationale="Clear title+author match; duration unknown",
            )
        ),
    )

    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.execute = AsyncMock(
        return_value=MagicMock(
            scalar_one_or_none=MagicMock(
                return_value=MagicMock(
                    title="Honeybites, Book 2",
                    author="I.S. Belle",
                    media_type="audiobook",
                )
            )
        )
    )
    pipeline = MagicMock()
    pipeline._update_status = AsyncMock()
    pipeline._is_cancelled = AsyncMock(return_value=False)

    candidate = {
        "asin": "B0HONEYBIT2",
        "title": "Honeybites",
        "authors": ["I.S. Belle"],
        "series": "Honeybloods",
        "sequence": "2",
        "score": 0.46,
        "chosen_metadata": {
            "title": "Honeybites",
            "author": "I.S. Belle",
            "series": "Honeybloods",
            "asin": "B0HONEYBIT2",
        },
        "allowed_edit_modes": ["full"],
        "recommended_edit_mode": "full",
    }

    async def _fake_apply(**kwargs):
        marker = staging / "libraforge.json"
        marker.write_text(
            json.dumps({"marker": {"applied": True, "manually_applied": True}}),
            encoding="utf-8",
        )
        return {"ok": True}

    async def _run():
        with (
            patch("app.services.forge_pipeline.async_session", return_value=session),
            patch("app.services.forge_pipeline._pipeline", return_value=pipeline),
            patch(
                "app.services.forge_pipeline._run_metadata_forge_once",
                new=AsyncMock(
                    return_value=("fail", "skipped: score below minimum: 0.46 < 0.7")
                ),
            ),
            patch(
                "app.services.libraforge.manual_review_search",
                new=AsyncMock(return_value={"results": [candidate]}),
            ) as search,
            patch(
                "app.services.libraforge.manual_review_apply",
                new=AsyncMock(side_effect=_fake_apply),
            ) as apply,
        ):
            status, reason = await _llm_metadata_assist_retry(
                88,
                staging=staging,
                user_id=1,
                prior_reason="skipped: score below minimum: 0.46 < 0.7",
            )
            return status, reason, search, apply

    status, reason, search, apply = asyncio.run(_run())
    assert status == "ok"
    assert reason == ""
    search.assert_awaited_once()
    apply.assert_awaited_once()
    assert (staging / "libraforge.json").is_file()
