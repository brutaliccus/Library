"""OpenRouter LLM assist — multi-book, prune, ASIN, ebook, usage parsers/paths."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import llm_assist, openrouter
from app.services.openrouter import (
    AsinSuggestion,
    BookIdentification,
    BookSplitGroup,
    BookSplitPlan,
    FilePruneAction,
    FilePrunePlan,
    parse_asin_suggestion,
    parse_prune_plan,
    parse_split_plan,
)


def test_parse_split_plan():
    plan = parse_split_plan(
        {
            "books": [
                {"title": "Book One", "author": "A", "paths": ["Book One"], "confidence": 0.9},
                {"title": "Book Two", "author": "A", "paths": ["Book Two"], "confidence": 0.88},
            ],
            "confidence": 0.9,
            "folder_based": True,
            "rationale": "Clear folders",
        }
    )
    assert plan is not None
    assert len(plan.books) == 2
    assert plan.folder_based is True
    assert plan.confidence == pytest.approx(0.9)


def test_parse_split_plan_rejects_single():
    assert parse_split_plan({"books": [{"title": "Only", "paths": ["a"]}], "confidence": 0.9}) is None


def test_parse_prune_plan_and_safe_flag():
    plan = parse_prune_plan(
        {
            "actions": [
                {"path": "mp3/ch01.mp3", "action": "delete", "reason": "prefer AAC duplicate"},
                {"path": "AAC/ch01.m4a", "action": "keep", "reason": "better format"},
            ],
            "confidence": 0.95,
        }
    )
    assert plan is not None
    deletes = [a for a in plan.actions if a.action == "delete"]
    assert len(deletes) == 1
    assert deletes[0].safe_duplicate is True


def test_parse_asin_suggestion():
    hit = parse_asin_suggestion(
        '{"asin":"B019NNU7XE","title":"The Gunslinger","confidence":0.9,"rationale":"ok"}'
    )
    assert hit is not None
    assert hit.asin == "B019NNU7XE"
    assert parse_asin_suggestion('{"asin":"not-an-asin","confidence":0.9}') is None


def test_detect_multi_book_vs_dual_format(tmp_path: Path):
    staging = tmp_path / "pack"
    (staging / "Book A").mkdir(parents=True)
    (staging / "Book B").mkdir(parents=True)
    (staging / "Book A" / "a.mp3").write_bytes(b"x")
    (staging / "Book B" / "b.mp3").write_bytes(b"y")
    assert llm_assist.detect_likely_multi_book(staging) is True
    assert llm_assist.detect_dual_format(staging) is False

    dual = tmp_path / "dual"
    (dual / "mp3").mkdir(parents=True)
    (dual / "AAC").mkdir(parents=True)
    (dual / "mp3" / "ch.mp3").write_bytes(b"x")
    (dual / "AAC" / "ch.m4a").write_bytes(b"y")
    assert llm_assist.detect_likely_multi_book(dual) is False
    assert llm_assist.detect_dual_format(dual) is True


def test_flat_multi_m4b_full_series_detected(tmp_path: Path):
    """ACOTAR-style: 5 complete m4bs in one folder, title says full series."""
    staging = tmp_path / "acotar"
    staging.mkdir()
    names = [
        "A Court of Thorns and Roses.m4b",
        "A Court of Mist and Fury.m4b",
        "A Court of Wings and Ruin.m4b",
        "A Court of Frost and Starlight.m4b",
        "A Court of Silver Flames.m4b",
    ]
    for name in names:
        (staging / name).write_bytes(b"x")

    # Folder-parent heuristic alone misses flat packs.
    assert llm_assist.detect_likely_multi_book(staging) is False

    hit = llm_assist.analyze_flat_multi_book(
        staging,
        title="A Court of Thorns and Roses - Full Series",
    )
    assert hit["likely"] is True
    assert len(hit["files"]) == 5
    assert hit["confidence"] >= 0.75

    plan = llm_assist.build_split_plan_from_flat_audio(
        staging,
        hit["files"],
        default_author="Sarah J. Maas",
        confidence=hit["confidence"],
        rationale=hit["rationale"],
    )
    assert plan is not None
    assert len(plan["books"]) == 5
    assert all(len(b["paths"]) == 1 and b["paths"][0].endswith(".m4b") for b in plan["books"])
    parsed = parse_split_plan(plan)
    assert parsed is not None
    assert len(parsed.books) == 5


def test_flat_m4b_chapter_parts_not_multi_book(tmp_path: Path):
    """Same title with pt 1/2/3 is one book, even if files are .m4b."""
    staging = tmp_path / "parts"
    staging.mkdir()
    for name in (
        "The Final Empire pt 1.m4b",
        "The Final Empire pt 2.m4b",
        "The Final Empire pt 3.m4b",
    ):
        (staging / name).write_bytes(b"x")

    hit = llm_assist.analyze_flat_multi_book(
        staging,
        title="Mistborn 1 - The Final Empire",
    )
    assert hit["likely"] is False


def test_title_looks_like_series_pack():
    assert llm_assist.title_looks_like_series_pack("ACOTAR Full Series") is True
    assert llm_assist.title_looks_like_series_pack("Mistborn Complete Series") is True
    assert llm_assist.title_looks_like_series_pack("Books 1-5 Box Set") is True
    assert llm_assist.title_looks_like_series_pack("A Court of Thorns and Roses") is False


def test_apply_file_prune_path_safe_and_never_sole(tmp_path: Path):
    staging = tmp_path / "st"
    staging.mkdir()
    (staging / "keep.m4a").write_bytes(b"a")
    (staging / "drop.mp3").write_bytes(b"b")
    plan = FilePrunePlan(
        actions=(
            FilePruneAction("drop.mp3", "delete", "duplicate", safe_duplicate=True),
            FilePruneAction("keep.m4a", "keep", ""),
        ),
        confidence=0.95,
    )
    deleted = llm_assist.apply_file_prune(staging, plan, only_safe_duplicates=True)
    assert deleted == ["drop.mp3"]
    assert (staging / "keep.m4a").is_file()
    assert not (staging / "drop.mp3").exists()

    # Sole remaining audio must not be deleted
    plan2 = FilePrunePlan(
        actions=(FilePruneAction("keep.m4a", "delete", "oops", safe_duplicate=True),),
        confidence=1.0,
    )
    assert llm_assist.apply_file_prune(staging, plan2, only_safe_duplicates=True) == []
    assert (staging / "keep.m4a").is_file()


def test_apply_file_prune_blocks_traversal(tmp_path: Path):
    staging = tmp_path / "st"
    staging.mkdir()
    (staging / "ok.mp3").write_bytes(b"x")
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"y")
    plan = FilePrunePlan(
        actions=(FilePruneAction("../outside.mp3", "delete", "x", safe_duplicate=True),),
        confidence=1.0,
    )
    assert llm_assist.apply_file_prune(staging, plan) == []
    assert outside.is_file()


def test_assist_sidecar_roundtrip(tmp_path: Path):
    staging = tmp_path / "s"
    staging.mkdir()
    llm_assist.write_assist(staging, {"file_prune_status": "proposed"})
    data = llm_assist.read_assist(staging)
    assert data["file_prune_status"] == "proposed"
    assert "updated_at" in data


def test_maybe_handle_multi_book_quarantines_low_conf(tmp_path: Path, monkeypatch):
    staging = tmp_path / "pack"
    (staging / "A").mkdir(parents=True)
    (staging / "B").mkdir(parents=True)
    (staging / "A" / "a.mp3").write_bytes(b"x")
    (staging / "B" / "b.mp3").write_bytes(b"y")

    monkeypatch.setattr(openrouter, "is_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(openrouter, "get_confidence_threshold", AsyncMock(return_value=0.85))
    monkeypatch.setattr(
        openrouter,
        "propose_multi_book_split",
        AsyncMock(
            return_value=BookSplitPlan(
                books=(
                    BookSplitGroup("A", "Auth", ("A",), 0.5),
                    BookSplitGroup("B", "Auth", ("B",), 0.5),
                ),
                confidence=0.5,
                folder_based=True,
                rationale="guess",
            )
        ),
    )

    async def _run():
        with (
            patch(
                "app.services.llm_assist._set_quarantine",
                new=AsyncMock(),
            ) as q,
            patch(
                "app.services.llm_assist._load_release_files_for_request",
                new=AsyncMock(return_value=[]),
            ),
        ):
            result = await llm_assist.maybe_handle_multi_book(
                1,
                staging=staging,
                user_id=1,
                title="Pack",
                author="Auth",
            )
            return result, q

    result, q = asyncio.run(_run())
    assert result == "quarantined"
    q.assert_awaited_once()
    assert llm_assist.read_assist(staging)["multi_book_status"] == "needs_review"


def test_maybe_recover_asin_stamps_when_verified(tmp_path: Path, monkeypatch):
    staging = tmp_path / "book"
    staging.mkdir()
    (staging / "book.m4b").write_bytes(b"x")

    monkeypatch.setattr(openrouter, "is_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(openrouter, "get_confidence_threshold", AsyncMock(return_value=0.85))
    monkeypatch.setattr(
        openrouter,
        "suggest_asin",
        AsyncMock(
            return_value=AsinSuggestion(
                asin="B019NNU7XE",
                title="The Gunslinger",
                author="Stephen King",
                confidence=0.92,
            )
        ),
    )

    async def _run():
        with (
            patch(
                "app.services.llm_assist.extract_asin_from_staging",
                return_value="",
            ),
            patch(
                "app.services.llm_assist.verify_asin_with_libraforge",
                new=AsyncMock(return_value=True),
            ),
        ):
            return await llm_assist.maybe_recover_asin(
                3,
                staging=staging,
                title="The Gunslinger",
                author="Stephen King",
            )

    asin = asyncio.run(_run())
    assert asin == "B019NNU7XE"
    meta = json.loads((staging / "metadata.json").read_text(encoding="utf-8"))
    assert meta["asin"] == "B019NNU7XE"


def test_maybe_recover_asin_no_stamp_when_unverified(tmp_path: Path, monkeypatch):
    staging = tmp_path / "book"
    staging.mkdir()
    (staging / "book.m4b").write_bytes(b"x")

    monkeypatch.setattr(openrouter, "is_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(openrouter, "get_confidence_threshold", AsyncMock(return_value=0.85))
    monkeypatch.setattr(
        openrouter,
        "suggest_asin",
        AsyncMock(
            return_value=AsinSuggestion(asin="B019NNU7XE", confidence=0.95)
        ),
    )

    async def _run():
        with (
            patch(
                "app.services.llm_assist.extract_asin_from_staging",
                return_value="",
            ),
            patch(
                "app.services.llm_assist.verify_asin_with_libraforge",
                new=AsyncMock(return_value=False),
            ),
        ):
            return await llm_assist.maybe_recover_asin(
                4,
                staging=staging,
                title="Something",
                author="Someone",
            )

    assert asyncio.run(_run()) is None
    assert not (staging / "metadata.json").exists()
    assert llm_assist.read_assist(staging)["asin_status"] == "unverified"


def test_ebook_llm_identify_retry_high_confidence(tmp_path: Path, monkeypatch):
    from app.services.ebook_pipeline import EbookMeta, _ebook_llm_identify_retry

    staging = tmp_path / "eb"
    staging.mkdir()
    (staging / "book.epub").write_bytes(b"x")

    monkeypatch.setattr(openrouter, "is_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(openrouter, "get_confidence_threshold", AsyncMock(return_value=0.85))
    monkeypatch.setattr(
        "app.services.llm_assist.ebook_identify_assist",
        AsyncMock(
            return_value=BookIdentification(
                title="Dune",
                author="Frank Herbert",
                confidence=0.93,
                rationale="clear",
            )
        ),
    )

    prior = EbookMeta(title="x", author="y", score=0.2, reason="No match")

    async def _run():
        with patch(
            "app.services.ebook_pipeline.identify_ebook_metadata",
            new=AsyncMock(
                return_value=EbookMeta(
                    title="Dune",
                    author="Frank Herbert",
                    score=0.95,
                    source="hardcover",
                    reason="ok",
                )
            ),
        ):
            return await _ebook_llm_identify_retry(
                staging=staging,
                title_hint="Dune",
                author_hint="Herbert",
                google_volume_id=None,
                prior_reason="No match",
                prior_meta=prior,
            )

    meta = asyncio.run(_run())
    assert meta is not None
    assert meta.title == "Dune"
    assert meta.score >= 0.9


@pytest.fixture(autouse=True)
def _clear_openrouter_credit_soft_disable():
    openrouter.clear_credits_exhausted()
    yield
    openrouter.clear_credits_exhausted()


def test_is_credit_error_detects_402_and_messages():
    assert openrouter.is_credit_error(402, "") is True
    assert openrouter.is_credit_error(429, "rate limit exceeded") is False
    assert openrouter.is_credit_error(429, "Insufficient credits") is True
    assert openrouter.is_credit_error(403, "Payment required: out of credits") is True
    assert openrouter.is_credit_error(500, "internal error") is False


def test_credits_exhausted_disables_like_toggle_off(monkeypatch):
    monkeypatch.setattr(
        "app.services.instance_settings.get_effective_bool",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(openrouter, "get_api_key", AsyncMock(return_value="sk-test"))

    assert asyncio.run(openrouter.is_enabled()) is True
    openrouter.mark_credits_exhausted("test")
    # Same as toggle off — callers skip LLM entirely.
    assert openrouter.credits_exhausted() is True
    assert asyncio.run(openrouter.is_enabled()) is False


def test_chat_json_402_soft_disables_and_returns_none(monkeypatch):
    monkeypatch.setattr(openrouter, "is_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(openrouter, "get_api_key", AsyncMock(return_value="sk-test"))
    monkeypatch.setattr(openrouter, "get_model", AsyncMock(return_value="openai/gpt-4o-mini"))

    mock_resp = MagicMock()
    mock_resp.status_code = 402
    mock_resp.text = '{"error":{"message":"Insufficient credits"}}'

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=mock_resp)

    async def _run():
        with patch("app.services.openrouter.httpx.AsyncClient", return_value=mock_client):
            return await openrouter._chat_json("sys", {"x": 1}, log_label="test")

    assert asyncio.run(_run()) is None
    assert openrouter.credits_exhausted() is True


def test_identify_book_skips_when_credits_exhausted(monkeypatch):
    openrouter.mark_credits_exhausted("no credits")
    monkeypatch.setattr(
        "app.services.instance_settings.get_effective_bool",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(openrouter, "get_api_key", AsyncMock(return_value="sk-test"))

    post = AsyncMock()
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = post

    async def _run():
        with patch("app.services.openrouter.httpx.AsyncClient", return_value=mock_client):
            return await openrouter.identify_book({"request_title": "Dune"})

    assert asyncio.run(_run()) is None
    post.assert_not_called()


def test_llm_metadata_assist_credits_exhausted_same_as_disabled(tmp_path: Path, monkeypatch):
    """Credits out ⇒ same as toggle off: prior_reason only, no Forge retry, no credit note."""
    from app.services.forge_pipeline import _llm_metadata_assist_retry

    staging = tmp_path / "req"
    staging.mkdir()
    (staging / "book.m4b").write_bytes(b"x")

    openrouter.mark_credits_exhausted("402")
    monkeypatch.setattr(
        "app.services.instance_settings.get_effective_bool",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(openrouter, "get_api_key", AsyncMock(return_value="sk-test"))

    identify = AsyncMock(
        return_value=BookIdentification(title="X", author="Y", confidence=0.99)
    )
    monkeypatch.setattr(openrouter, "identify_book", identify)

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
                9,
                staging=staging,
                user_id=1,
                prior_reason="score too low",
            )
            return status, reason, run_once

    status, reason, run_once = asyncio.run(_run())
    assert status == "fail"
    assert reason == "score too low"
    assert "credit" not in reason.lower()
    run_once.assert_not_awaited()
    identify.assert_not_awaited()


def test_fetch_key_usage_parses(monkeypatch):
    monkeypatch.setattr(openrouter, "get_api_key", AsyncMock(return_value="sk-test"))

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "data": {
            "label": "sk-or-v1-abcdefg...890",
            "usage": 25.5,
            "usage_daily": 1.0,
            "usage_weekly": 5.0,
            "usage_monthly": 20.0,
            "limit": 100,
            "limit_remaining": 74.5,
            "limit_reset": "monthly",
            "is_free_tier": False,
        }
    }

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_resp)

    async def _run():
        with patch("app.services.openrouter.httpx.AsyncClient", return_value=mock_client):
            return await openrouter.fetch_key_usage()

    usage = asyncio.run(_run())
    assert usage.limit_remaining == pytest.approx(74.5)
    assert usage.usage_monthly == pytest.approx(20.0)
    assert usage.error == ""
    assert openrouter.credits_exhausted() is False
    d = usage.to_dict()
    assert d["limitRemaining"] == pytest.approx(74.5)
    assert "sk-test" not in json.dumps(d)


def test_fetch_key_usage_zero_remaining_soft_disables(monkeypatch):
    monkeypatch.setattr(openrouter, "get_api_key", AsyncMock(return_value="sk-test"))

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "data": {
            "label": "sk-or-v1-x",
            "usage": 100.0,
            "limit": 100.0,
            "limit_remaining": 0.0,
            "limit_reset": "monthly",
        }
    }
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_resp)

    async def _run():
        with patch("app.services.openrouter.httpx.AsyncClient", return_value=mock_client):
            return await openrouter.fetch_key_usage()

    usage = asyncio.run(_run())
    assert usage.limit_remaining == pytest.approx(0.0)
    assert openrouter.credits_exhausted() is True
    assert usage.to_dict()["creditsExhausted"] is True
