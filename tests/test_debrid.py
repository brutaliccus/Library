"""Unit tests for the debrid provider abstraction and Torbox client helpers."""

from app.services import debrid, torbox


# ---------------- provider picking ----------------

def test_pick_prefers_cached_provider(monkeypatch):
    monkeypatch.setattr(debrid, "available_providers", lambda: ["rd", "torbox"])
    cached = {"rd": set(), "torbox": {"abc"}}
    assert debrid.pick_provider("ABC", cached, preferred="rd") == "torbox"


def test_pick_prefers_preferred_when_both_cached(monkeypatch):
    monkeypatch.setattr(debrid, "available_providers", lambda: ["rd", "torbox"])
    cached = {"rd": {"abc"}, "torbox": {"abc"}}
    assert debrid.pick_provider("abc", cached, preferred="torbox") == "torbox"
    assert debrid.pick_provider("abc", cached, preferred="rd") == "rd"


def test_pick_falls_back_to_preference_when_uncached(monkeypatch):
    monkeypatch.setattr(debrid, "available_providers", lambda: ["rd", "torbox"])
    cached = {"rd": set(), "torbox": set()}
    assert debrid.pick_provider("abc", cached, preferred="torbox") == "torbox"
    assert debrid.pick_provider("abc", cached, preferred="rd") == "rd"


def test_normalize_provider():
    assert debrid.normalize_provider("torbox") == "torbox"
    assert debrid.normalize_provider("RD") == "rd"
    assert debrid.normalize_provider("") == "rd"
    assert debrid.normalize_provider(None) == "rd"
    assert debrid.normalize_provider("bogus") == "rd"


# ---------------- torbox pseudo-links ----------------

def test_pseudo_link_roundtrip():
    link = torbox.make_pseudo_link(42, 7, "Chapter 01.mp3")
    assert link.startswith("torbox://")
    parsed = torbox.parse_pseudo_link(link)
    assert parsed == ("42", "7", "Chapter 01.mp3")


def test_parse_pseudo_link_rejects_other_urls():
    assert torbox.parse_pseudo_link("https://example.com/x.mp3") is None
    assert torbox.parse_pseudo_link("") is None


def test_link_filename_uses_pseudo_link_name():
    link = torbox.make_pseudo_link(1, 2, "book.m4b")
    # Torbox CDN URLs often lack the filename entirely
    assert debrid.link_filename(link, "https://cdn.torbox.app/dl?x=1") == "book.m4b"


def test_link_filename_falls_back_to_url():
    url = "https://rd.example/dl/book.m4b?token=x"
    assert debrid.link_filename(url, url) == "book.m4b"


# ---------------- torbox info normalization ----------------

def test_normalize_info_finished_torrent():
    raw = {
        "id": 99,
        "name": "Some Book",
        "hash": "ABCDEF",
        "download_finished": True,
        "download_present": True,
        "download_state": "uploading",
        "progress": 1,
        "files": [
            {"id": 0, "name": "folder/book.m4b", "size": 12345},
            {"id": 1, "name": "folder/cover.jpg", "size": 100},
        ],
    }
    info = torbox._normalize_info(raw)
    assert info["status"] == "downloaded"
    assert info["progress"] == 100
    assert info["hash"] == "abcdef"
    assert len(info["links"]) == 2
    parsed = torbox.parse_pseudo_link(info["links"][0])
    assert parsed == ("99", "0", "book.m4b")


def test_normalize_info_downloading_torrent():
    raw = {
        "id": 5,
        "download_finished": False,
        "download_present": False,
        "download_state": "downloading",
        "progress": 0.42,
        "files": [],
    }
    info = torbox._normalize_info(raw)
    assert info["status"] == "downloading"
    assert info["progress"] == 42
    assert info["links"] == []
