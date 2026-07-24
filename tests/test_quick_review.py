"""Unit tests for Quick Admin Review clue merge and path helpers."""

from __future__ import annotations

from pathlib import Path

from app.services.quick_review import (
    _enrich_selected_for_apply,
    _folder_title_hint,
    _looks_like_junk_title,
    list_staging_targets,
    merge_clues_with_catalog,
    resolve_apply_edit_mode,
    resolve_target_path,
)


def test_looks_like_junk_title_tape_and_chapter():
    assert _looks_like_junk_title("Tape1")
    assert _looks_like_junk_title("tape_01")
    assert _looks_like_junk_title("Chapter 03")
    assert _looks_like_junk_title("Part 2")
    assert _looks_like_junk_title("01")
    assert not _looks_like_junk_title("Timeline")
    assert not _looks_like_junk_title("The Gunslinger")


def test_folder_title_hint_strips_req_prefix():
    assert _folder_title_hint(Path("/audiobooks/.unorganized/req_9_Timeline")) == "Timeline"
    assert "Dark" in _folder_title_hint(Path("/x/req_12_The_Dark_Tower"))


def test_merge_prefers_catalog_over_tape1_filename():
    loaded = {
        "queries": ["Tape1"],
        "metadata": {
            "title": "Tape1",
            "raw_title": "Tape1",
            "author": "",
            "series": "",
            "sequence": "",
            "narrator": "",
        },
        "is_grouped": False,
        "group_search": {},
    }
    merged = merge_clues_with_catalog(
        loaded,
        request_title="Timeline",
        request_author="Michael Crichton",
        folder_hint="Timeline",
    )
    assert merged["clues"]["title"] == "Timeline"
    assert merged["clues"]["author"] == "Michael Crichton"
    assert merged["clues"]["query"].lower().startswith("timeline")
    assert "michael crichton" in merged["clues"]["query"].lower()
    assert not merged["clues"]["query"].lower().startswith("tape1")


def test_merge_keeps_good_loaded_title():
    loaded = {
        "queries": ["The Gunslinger Stephen King"],
        "metadata": {
            "title": "The Gunslinger",
            "author": "Stephen King",
            "series": "The Dark Tower",
            "sequence": "1",
            "narrator": "George Guidall",
        },
    }
    merged = merge_clues_with_catalog(
        loaded,
        request_title="Something Else",
        request_author="Other",
        folder_hint="req junk",
    )
    assert merged["clues"]["title"] == "The Gunslinger"
    assert merged["clues"]["author"] == "Stephen King"


def test_list_staging_targets_and_resolve(tmp_path):
    staging = tmp_path / "req_9_Timeline"
    staging.mkdir()
    (staging / "Tape1.mp3").write_bytes(b"x")
    targets = list_staging_targets(staging)
    assert len(targets) == 1
    assert targets[0]["file_count"] == 1
    assert targets[0]["relative_path"] == ""

    local, lf = resolve_target_path(staging, "")
    assert local == staging.resolve()
    assert "req_9_Timeline" in lf.replace("\\", "/")


def test_resolve_apply_edit_mode_forces_full_when_replace_cover():
    selected = {
        "recommended_edit_mode": "series_only",
        "allowed_edit_modes": ["full", "series_only"],
    }
    assert (
        resolve_apply_edit_mode(selected, edit_mode="series_only", replace_cover=True)
        == "full"
    )
    assert (
        resolve_apply_edit_mode(selected, edit_mode="series_only", replace_cover=False)
        == "series_only"
    )


def test_enrich_selected_injects_top_level_cover_into_full_mode():
    selected = {
        "cover_url": "https://images.example/cover.jpg",
        "chosen_metadata_by_mode": {
            "full": {"title": "Timeline", "author": "Crichton", "cover_url": ""},
            "series_only": {"series": "X", "cover_url": ""},
        },
    }
    enriched, override = _enrich_selected_for_apply(
        selected, edit_mode="full", replace_cover=True
    )
    assert enriched["chosen_metadata_by_mode"]["full"]["cover_url"] == (
        "https://images.example/cover.jpg"
    )
    assert override.get("cover_url") == "https://images.example/cover.jpg"
