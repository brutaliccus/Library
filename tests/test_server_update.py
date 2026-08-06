"""Tests for Admin server stack update status/check helpers."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.services import server_update


def test_short_sha():
    assert server_update._short_sha("abcdef1234567890") == "abcdef1"
    assert server_update._short_sha(None) is None


def test_local_from_revision_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rev = tmp_path / "install_revision.json"
    rev.write_text(
        json.dumps(
            {
                "sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "shortSha": "aaaaaaa",
                "branch": "main",
                "message": "test",
                "committedAt": "2026-08-01T12:00:00Z",
                "source": "test",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(server_update, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server_update, "REVISION_FILE", rev)
    monkeypatch.setattr(server_update, "JOB_FILE", tmp_path / "job.json")
    monkeypatch.setattr(server_update, "JOB_LOG", tmp_path / "job.log")

    async def _run():
        local = await server_update.get_local_version()
        assert local["sha"].startswith("aaa")
        assert local["shortSha"] == "aaaaaaa"
        assert local["branch"] == "main"

    asyncio.run(_run())


def test_local_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(server_update, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server_update, "REVISION_FILE", tmp_path / "missing.json")
    monkeypatch.setattr(server_update, "HOST_MOUNT", tmp_path / "no-mount")
    monkeypatch.setenv("GITHUB_SHA", "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")

    async def _run():
        with patch.object(server_update, "_local_from_git_dir", AsyncMock(return_value=None)):
            local = await server_update.get_local_version()
        assert local["sha"].startswith("bbb")
        assert local["source"] == "env"

    asyncio.run(_run())


def test_check_up_to_date(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    sha = "cccccccccccccccccccccccccccccccccccccccc"
    rev = tmp_path / "install_revision.json"
    rev.write_text(json.dumps({"sha": sha, "shortSha": "ccccccc"}), encoding="utf-8")
    monkeypatch.setattr(server_update, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server_update, "REVISION_FILE", rev)
    monkeypatch.setattr(server_update, "JOB_FILE", tmp_path / "job.json")
    monkeypatch.setattr(server_update, "JOB_LOG", tmp_path / "job.log")

    async def _run():
        with (
            patch.object(
                server_update,
                "discover_host_root",
                AsyncMock(return_value={"hostRoot": "/opt/library", "source": "env", "error": None}),
            ),
            patch.object(server_update.docker_control, "socket_available", return_value=True),
            patch.object(server_update, "_github_repo", AsyncMock(return_value="brutaliccus/Library")),
            patch.object(
                server_update,
                "_github_tip",
                AsyncMock(
                    return_value={
                        "sha": sha,
                        "shortSha": "ccccccc",
                        "branch": "main",
                        "message": "tip",
                        "committedAt": "2026-08-06T00:00:00Z",
                        "source": "github_api",
                    }
                ),
            ),
        ):
            out = await server_update.check_for_updates()
        assert out["state"] == "up_to_date"
        assert out["remote"]["sha"] == sha

    asyncio.run(_run())


def test_check_update_available(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    local = "dddddddddddddddddddddddddddddddddddddddd"
    remote = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    rev = tmp_path / "install_revision.json"
    rev.write_text(json.dumps({"sha": local, "shortSha": "ddddddd"}), encoding="utf-8")
    monkeypatch.setattr(server_update, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server_update, "REVISION_FILE", rev)
    monkeypatch.setattr(server_update, "JOB_FILE", tmp_path / "job.json")
    monkeypatch.setattr(server_update, "JOB_LOG", tmp_path / "job.log")

    async def _run():
        with (
            patch.object(
                server_update,
                "discover_host_root",
                AsyncMock(return_value={"hostRoot": "/opt/library", "source": "env", "error": None}),
            ),
            patch.object(server_update.docker_control, "socket_available", return_value=True),
            patch.object(server_update, "_github_repo", AsyncMock(return_value="brutaliccus/Library")),
            patch.object(
                server_update,
                "_github_tip",
                AsyncMock(
                    return_value={
                        "sha": remote,
                        "shortSha": "eeeeeee",
                        "branch": "main",
                        "message": "newer",
                        "committedAt": "2026-08-06T00:00:00Z",
                        "source": "github_api",
                    }
                ),
            ),
            patch.object(
                server_update,
                "_github_compare",
                AsyncMock(
                    return_value={
                        "status": "ahead",
                        "aheadBy": 3,
                        "behindBy": 0,
                        "totalCommits": 3,
                    }
                ),
            ),
        ):
            out = await server_update.check_for_updates()
        assert out["state"] == "update_available"
        assert out["compare"]["commitsBehind"] == 3

    asyncio.run(_run())


def test_check_failed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(server_update, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server_update, "REVISION_FILE", tmp_path / "missing.json")
    monkeypatch.setattr(server_update, "JOB_FILE", tmp_path / "job.json")
    monkeypatch.setattr(server_update, "JOB_LOG", tmp_path / "job.log")

    async def _run():
        with (
            patch.object(
                server_update,
                "discover_host_root",
                AsyncMock(return_value={"hostRoot": None, "source": None, "error": "no root"}),
            ),
            patch.object(server_update.docker_control, "socket_available", return_value=False),
            patch.object(server_update, "_github_repo", AsyncMock(return_value="brutaliccus/Library")),
            patch.object(server_update, "get_local_version", AsyncMock(return_value={"sha": None})),
            patch.object(server_update, "_github_tip", AsyncMock(side_effect=RuntimeError("boom"))),
        ):
            out = await server_update.check_for_updates()
        assert out["state"] == "check_failed"
        assert "boom" in (out.get("error") or "")

    asyncio.run(_run())


def test_start_apply_requires_host_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(server_update, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server_update, "JOB_FILE", tmp_path / "job.json")
    monkeypatch.setattr(server_update, "JOB_LOG", tmp_path / "job.log")

    async def _run():
        with (
            patch.object(server_update, "is_apply_running", return_value=False),
            patch.object(
                server_update,
                "discover_host_root",
                AsyncMock(return_value={"hostRoot": None, "error": "missing"}),
            ),
            pytest.raises(RuntimeError, match="missing"),
        ):
            await server_update.start_apply()

    asyncio.run(_run())