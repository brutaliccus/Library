"""Library-owned Chapter Forge .m4b remux (ffmpeg stream copy)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.chapter_embed import (
    can_embed_chapters,
    chapters_from_libraforge_sidecar,
    chapters_from_run_report,
    embed_chapters_into_audio,
    normalize_chapters_for_embed,
    write_ffmetadata,
)


class WriteFfmetadataTests(unittest.TestCase):
    def test_builds_chapter_blocks(self):
        text = write_ffmetadata(
            [
                {"title": "One", "start": 0.0, "end": 60.0},
                {"title": "Two=Special; #hash", "start": 60.0, "end": 120.5},
            ]
        )
        self.assertIn(";FFMETADATA1", text)
        self.assertIn("START=0", text)
        self.assertIn("END=60000", text)
        self.assertIn("START=60000", text)
        self.assertIn("END=120500", text)
        self.assertIn("title=Two\\=Special\\; \\#hash", text)


class NormalizeChaptersTests(unittest.TestCase):
    def test_fills_end_from_next_start(self):
        rows = normalize_chapters_for_embed(
            [
                {"title": "A", "start": 0.0},
                {"title": "B", "start_ms": 60000, "length_ms": 30000},
            ]
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["end"], 60.0)
        self.assertEqual(rows[1]["start"], 60.0)
        self.assertEqual(rows[1]["end"], 90.0)


class ChaptersFromReportTests(unittest.TestCase):
    def test_reads_nested_chaptering_result(self):
        report = {
            "stats": {
                "chapters": 2,
                "chaptering_result": {
                    "chapters": [
                        {"title": "Intro", "start": 0.0, "end": 10.0},
                        {"title": "One", "start": 10.0, "end": 20.0},
                    ]
                },
            }
        }
        rows = chapters_from_run_report(report)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["title"], "Intro")
        self.assertEqual(rows[1]["end"], 20.0)


class SidecarLoadTests(unittest.TestCase):
    def test_reads_chapter_forge_block(self):
        with tempfile.TemporaryDirectory() as root:
            audio = Path(root) / "Book.m4b"
            audio.write_bytes(b"x" * 100)
            sidecar = Path(root) / "libraforge.json"
            sidecar.write_text(
                json.dumps(
                    {
                        "chapter_forge": {
                            "chapters": [
                                {"title": "Ch 1", "start": 0.0, "end": 5.0},
                                {"title": "Ch 2", "start": 5.0, "end": 10.0},
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            rows = chapters_from_libraforge_sidecar(audio)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["title"], "Ch 1")


class EmbedChaptersTests(unittest.TestCase):
    def test_can_embed_only_mp4_family_files(self):
        with tempfile.TemporaryDirectory() as root:
            m4b = Path(root) / "book.m4b"
            mp3 = Path(root) / "book.mp3"
            m4b.write_bytes(b"x" * 2000)
            mp3.write_bytes(b"x" * 2000)
            self.assertTrue(can_embed_chapters(m4b))
            self.assertFalse(can_embed_chapters(mp3))

    def test_embed_invokes_ffmpeg_stream_copy_and_replaces(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "book.m4b"
            source.write_bytes(b"ORIGINAL" + b"\0" * 2000)
            chapters = [
                {"title": "Ch 1", "start": 0.0, "end": 10.0},
                {"title": "Ch 2", "start": 10.0, "end": 20.0},
            ]

            def fake_run(cmd, **kwargs):
                out = Path(cmd[-1])
                out.write_bytes(b"REWRITTEN" + b"\0" * 2000)

                class Result:
                    returncode = 0
                    stdout = ""
                    stderr = ""

                return Result()

            with patch("app.services.chapter_embed.ffmpeg_available", return_value=True), patch(
                "app.services.chapter_embed.subprocess.run", side_effect=fake_run
            ) as mock_run:
                embed_chapters_into_audio(source, chapters, duration=20.0)

            self.assertTrue(mock_run.called)
            cmd = mock_run.call_args[0][0]
            self.assertEqual(cmd[0], "ffmpeg")
            self.assertIn("-map_chapters", cmd)
            self.assertIn("1", cmd)
            self.assertIn("-c", cmd)
            self.assertIn("copy", cmd)
            self.assertEqual(source.read_bytes()[:9], b"REWRITTEN")


if __name__ == "__main__":
    unittest.main()
