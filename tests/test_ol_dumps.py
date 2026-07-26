"""Remote-changed detection for Open Library dumps (mocked HEAD)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.services import ol_dumps


def _fake_resp(*, etag=None, content_length=None, last_modified=None, url="https://example/x"):
    headers = {}
    if etag:
        headers["ETag"] = etag
    if content_length is not None:
        headers["Content-Length"] = str(content_length)
    if last_modified:
        headers["Last-Modified"] = last_modified
    resp = MagicMock()
    resp.headers = headers
    resp.geturl.return_value = url
    resp.read.return_value = b""
    return resp


def test_remote_differs_missing_local(tmp_path: Path):
    dest = tmp_path / "ol_dump_works.txt.gz"
    remote = {"etag": '"abc"', "content_length": 1000, "last_modified": "Mon, 01 Jan 2026 00:00:00 GMT"}
    assert ol_dumps.remote_differs_from_local(dest, remote) is True


def test_remote_same_etag_meta(tmp_path: Path):
    dest = tmp_path / "ol_dump_works.txt.gz"
    dest.write_bytes(b"x" * 5000)
    ol_dumps.save_meta(
        dest,
        {
            "etag": '"abc"',
            "content_length": 5000,
            "last_modified": "Mon, 01 Jan 2026 00:00:00 GMT",
            "source_url": "https://openlibrary.org/data/ol_dump_works_latest.txt.gz",
        },
    )
    remote = {
        "etag": '"abc"',
        "content_length": 5000,
        "last_modified": "Mon, 01 Jan 2026 00:00:00 GMT",
    }
    assert ol_dumps.remote_differs_from_local(dest, remote) is False


def test_remote_changed_etag(tmp_path: Path):
    dest = tmp_path / "ol_dump_works.txt.gz"
    dest.write_bytes(b"x" * 5000)
    ol_dumps.save_meta(
        dest,
        {
            "etag": '"old"',
            "content_length": 5000,
            "last_modified": "Mon, 01 Jan 2026 00:00:00 GMT",
            "source_url": "https://openlibrary.org/data/ol_dump_works_latest.txt.gz",
        },
    )
    remote = {
        "etag": '"new"',
        "content_length": 5000,
        "last_modified": "Mon, 01 Jan 2026 00:00:00 GMT",
    }
    assert ol_dumps.remote_differs_from_local(dest, remote) is True


def test_remote_changed_size_without_meta(tmp_path: Path):
    dest = tmp_path / "ol_dump_authors.txt.gz"
    dest.write_bytes(b"x" * 5000)
    remote = {"etag": None, "content_length": 9000, "last_modified": None}
    assert ol_dumps.remote_differs_from_local(dest, remote) is True


def test_remote_same_size_without_meta(tmp_path: Path):
    dest = tmp_path / "ol_dump_authors.txt.gz"
    dest.write_bytes(b"x" * 5000)
    remote = {"etag": None, "content_length": 5000, "last_modified": None}
    assert ol_dumps.remote_differs_from_local(dest, remote) is False


def test_check_dumps_reports_changed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    dumps = tmp_path / "dumps"
    dumps.mkdir()
    works = dumps / "ol_dump_works.txt.gz"
    authors = dumps / "ol_dump_authors.txt.gz"
    works.write_bytes(b"w" * 5000)
    authors.write_bytes(b"a" * 5000)
    ol_dumps.save_meta(
        works,
        {"etag": '"w1"', "content_length": 5000, "last_modified": "A", "source_url": "u"},
    )
    ol_dumps.save_meta(
        authors,
        {"etag": '"a1"', "content_length": 5000, "last_modified": "A", "source_url": "u"},
    )

    def fake_head(url: str, ua: str, timeout: float = 30.0):
        if "works" in url:
            return {"etag": '"w2"', "content_length": 5000, "last_modified": "B", "source_url": url}
        return {"etag": '"a1"', "content_length": 5000, "last_modified": "A", "source_url": url}

    monkeypatch.setattr(ol_dumps, "head_remote", fake_head)
    summary = ol_dumps.check_dumps(dumps, names=["authors", "works"], ua="Test/1.0")
    assert summary["update_available"] is True
    assert summary["changed"] == ["works"]
    assert "authors" in summary["unchanged"]


def test_check_dumps_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    dumps = tmp_path / "dumps"
    dumps.mkdir()
    for name in ("authors", "works"):
        p = dumps / f"ol_dump_{name}.txt.gz"
        p.write_bytes(b"x" * 4000)
        ol_dumps.save_meta(
            p,
            {"etag": f'"{name}"', "content_length": 4000, "last_modified": "L", "source_url": "u"},
        )

    monkeypatch.setattr(
        ol_dumps,
        "head_remote",
        lambda url, ua, timeout=30.0: {
            "etag": '"authors"' if "authors" in url else '"works"',
            "content_length": 4000,
            "last_modified": "L",
            "source_url": url,
        },
    )
    summary = ol_dumps.check_dumps(dumps, names=["authors", "works"], ua="Test/1.0")
    assert summary["update_available"] is False
    assert summary["changed"] == []


def test_should_redownload_force(tmp_path: Path):
    dest = tmp_path / "ol_dump_works.txt.gz"
    dest.write_bytes(b"x" * 5000)
    assert (
        ol_dumps.should_redownload(
            dest,
            "https://openlibrary.org/data/ol_dump_works_latest.txt.gz",
            "Test/1.0",
            force=True,
        )
        is True
    )


def test_head_remote_uses_head(monkeypatch: pytest.MonkeyPatch):
    resp = _fake_resp(etag='"e1"', content_length=123, last_modified="Tue, 01 Jul 2026 00:00:00 GMT")
    cm = MagicMock()
    cm.__enter__.return_value = resp
    cm.__exit__.return_value = False
    with patch("urllib.request.urlopen", return_value=cm) as urlopen:
        remote = ol_dumps.head_remote("https://openlibrary.org/data/ol_dump_works_latest.txt.gz", "UA")
    assert remote["etag"] == '"e1"'
    assert remote["content_length"] == 123
    req = urlopen.call_args[0][0]
    assert req.get_method() == "HEAD"


def test_check_for_updates_sets_flag_and_notifies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import asyncio

    from app.services import ol_catalog_build

    db = tmp_path / "ol_catalog.db"
    dumps = tmp_path / "dumps"
    dumps.mkdir()

    class _Settings:
        ol_catalog_db_path = str(db)
        ol_dumps_dir = str(dumps)
        open_library_user_agent = "Test/1.0"
        ol_catalog_include_editions = False

    monkeypatch.setattr(ol_catalog_build, "get_settings", lambda: _Settings())
    monkeypatch.setattr(
        "app.services.ol_dumps.check_dumps",
        lambda *a, **k: {
            "checked_at": 1.0,
            "dumps_dir": str(dumps),
            "names": ["authors", "works"],
            "changed": ["works"],
            "missing": [],
            "unchanged": ["authors"],
            "errors": {},
            "remotes": {},
            "update_available": True,
            "signature": "sig-new",
        },
    )
    notified: list[dict] = []

    async def _notify(payload):
        notified.append(payload)

    monkeypatch.setattr("app.services.push.notify_admins_background", _notify)

    status = asyncio.run(ol_catalog_build.check_for_updates(force=True, notify=True))
    assert status["new_dumps_available"] is True
    assert status["changed_dumps"] == ["works"]
    assert "New Open Library dumps available" in status["message"]
    assert len(notified) == 1
    assert notified[0]["type"] == "ol_dumps_available"

    # Same signature should not re-notify.
    status2 = asyncio.run(ol_catalog_build.check_for_updates(force=True, notify=True))
    assert status2["new_dumps_available"] is True
    assert len(notified) == 1
