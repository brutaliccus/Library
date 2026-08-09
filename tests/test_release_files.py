"""ABB / debrid release file lists for multi-book pack splitting."""

from __future__ import annotations

from pathlib import Path

from app.services.audiobookbay import parse_abb_file_list
from app.services.release_files import (
    build_split_plan_from_release_files,
    group_release_files_by_book,
    normalize_release_files,
    release_files_from_torrent_info,
)


ABB_HTML = """
<html><body>
<p>This is a Multifile Torrent</p>
<table>
<tr><td>Tracker:</td><td>udp://example</td></tr>
<tr>
  <td>1 Mistborn - The Final Empire - Graphic Audio</td>
  <td>Mistborn 1 - The Final Empire (1 of 3)</td>
  <td>MISTBORN0101P01.mp3</td>
  <td>132.67 MBs</td>
</tr>
<tr>
  <td>1 Mistborn - The Final Empire - Graphic Audio</td>
  <td>Mistborn 1 - The Final Empire (1 of 3)</td>
  <td>MISTBORN0101P02.mp3</td>
  <td>113.76 MBs</td>
</tr>
<tr>
  <td>Brandon Sanderson - Mistborn 02 - The Well of Ascension [Graphic Audio]</td>
  <td>Brandon Sanderson - Mistborn 02 - The Well of Ascension pt 1.mp3</td>
  <td>857.39 MBs</td>
</tr>
<tr>
  <td>Mistborn 4 - The Alloy of Law</td>
  <td>MISTBORN04P01.mp3</td>
  <td>129.89 MBs</td>
</tr>
</table>
</body></html>
"""


def test_parse_abb_file_list_groups_by_top_folder():
    files = parse_abb_file_list(ABB_HTML)
    assert len(files) == 4
    assert files[0]["path"].startswith("1 Mistborn - The Final Empire - Graphic Audio/")
    assert files[0]["name"] == "MISTBORN0101P01.mp3"
    groups = group_release_files_by_book(files)
    assert len(groups) == 3
    titles = {g["title"].lower() for g in groups}
    assert any("final empire" in t for t in titles)
    assert any("well of ascension" in t or "mistborn 02" in t for t in titles)


def test_build_split_plan_maps_flat_staging(tmp_path: Path):
    staging = tmp_path / "st"
    staging.mkdir()
    (staging / "MISTBORN0101P01.mp3").write_bytes(b"a")
    (staging / "MISTBORN0101P02.mp3").write_bytes(b"b")
    (staging / "Brandon Sanderson - Mistborn 02 - The Well of Ascension pt 1.mp3").write_bytes(b"c")
    (staging / "MISTBORN04P01.mp3").write_bytes(b"d")

    files = parse_abb_file_list(ABB_HTML)
    plan = build_split_plan_from_release_files(staging, files, default_author="Brandon Sanderson")
    assert plan is not None
    assert len(plan["books"]) == 3
    assert plan["confidence"] >= 0.9
    # Every book got at least one staging path
    assert all(b["paths"] for b in plan["books"])


def test_release_files_from_torrent_info():
    info = {
        "files": [
            {"path": "/Book A/part1.mp3", "bytes": 10},
            {"path": "/Book B/part1.mp3", "bytes": 11},
        ]
    }
    files = release_files_from_torrent_info(info)
    assert [f["path"] for f in files] == ["Book A/part1.mp3", "Book B/part1.mp3"]
    assert len(group_release_files_by_book(files)) == 2


def test_normalize_release_files_dedupes():
    rows = normalize_release_files(
        [
            {"path": "A/a.mp3", "size_bytes": 1},
            {"path": "A/a.mp3", "size_bytes": 2},
            "B/b.mp3",
        ]
    )
    assert len(rows) == 2