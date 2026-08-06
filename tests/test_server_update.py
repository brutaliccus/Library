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
            patch.object(server_update.docker_control, "socket_available", return_value=True),
            patch.object(
                server_update,
                "resolve_validated_host_root",
                AsyncMock(return_value={"hostRoot": None, "error": "missing"}),
            ),
            pytest.raises(RuntimeError, match="missing"),
        ):
            await server_update.start_apply()

    asyncio.run(_run())


def test_looks_like_host_abs_path():
    assert server_update.looks_like_host_abs_path("/opt/library")
    assert server_update.looks_like_host_abs_path("C:\\dev\\Library")
    assert not server_update.looks_like_host_abs_path("library")
    assert not server_update.looks_like_host_abs_path("")
    assert not server_update.looks_like_host_abs_path(".")


def test_host_root_from_app_data_mount():
    assert server_update.host_root_from_app_data_mount("/opt/library/data") == "/opt/library"
    assert server_update.host_root_from_app_data_mount("/opt/library/data/") == "/opt/library"
    # Must not invent a bare /library from unrelated mounts
    assert server_update.host_root_from_app_data_mount("/library/data") == "/library"


def test_discover_prefers_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LIBRARY_HOST_ROOT", "/opt/library")

    async def _run():
        with patch.object(server_update.docker_control, "socket_available", return_value=False):
            out = await server_update.discover_host_root()
        assert out["hostRoot"] == "/opt/library"
        assert out["source"] == "env"
        assert out["error"] is None

    asyncio.run(_run())


def test_discover_rejects_relative_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LIBRARY_HOST_ROOT", "library")

    async def _run():
        with patch.object(server_update.docker_control, "socket_available", return_value=False):
            out = await server_update.discover_host_root()
        assert out["hostRoot"] is None
        assert "not an absolute host path" in (out.get("error") or "")

    asyncio.run(_run())


def test_discover_from_app_data_mount(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("LIBRARY_HOST_ROOT", raising=False)
    info = {
        "Config": {"Labels": {}},
        "Mounts": [
            {"Source": "/opt/library/data", "Destination": "/app/data"},
            {"Source": "/opt/library/media/audiobooks", "Destination": "/audiobooks"},
        ],
    }

    async def _run():
        with (
            patch.object(server_update.docker_control, "socket_available", return_value=True),
            patch.object(server_update.docker_control, "_inspect", AsyncMock(return_value=info)),
        ):
            out = await server_update.discover_host_root()
        assert out["hostRoot"] == "/opt/library"
        assert out["source"] == "mount_app_data"

    asyncio.run(_run())


def test_discover_compose_label_before_mount(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("LIBRARY_HOST_ROOT", raising=False)
    info = {
        "Config": {
            "Labels": {"com.docker.compose.project.working_dir": "/opt/library"},
        },
        "Mounts": [
            {"Source": "/library/data", "Destination": "/app/data"},
        ],
    }

    async def _run():
        with (
            patch.object(server_update.docker_control, "socket_available", return_value=True),
            patch.object(server_update.docker_control, "_inspect", AsyncMock(return_value=info)),
        ):
            out = await server_update.discover_host_root()
        assert out["hostRoot"] == "/opt/library"
        assert out["source"] == "compose_label"
        paths = [c["path"] for c in out["candidates"]]
        assert "/opt/library" in paths
        assert "/library" in paths  # mount candidate still listed, not chosen first

    asyncio.run(_run())


def test_resolve_validated_rejects_bare_library_without_markers(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("LIBRARY_HOST_ROOT", raising=False)

    async def _fake_collect():
        return [("mount_app_data", "/library")], []

    async def _run():
        with (
            patch.object(
                server_update,
                "_collect_host_root_candidates",
                AsyncMock(side_effect=_fake_collect),
            ),
            patch.object(
                server_update,
                "_probe_host_root",
                AsyncMock(return_value=(False, "missing .git and scripts/update_library.sh")),
            ),
        ):
            out = await server_update.resolve_validated_host_root()
        assert out["hostRoot"] is None
        assert "/library" in (out.get("error") or "")
        assert "missing .git" in (out.get("error") or "")

    asyncio.run(_run())


def test_resolve_validated_accepts_opt_library(monkeypatch: pytest.MonkeyPatch):
    async def _run():
        with (
            patch.object(
                server_update,
                "_collect_host_root_candidates",
                AsyncMock(return_value=([("env", "/opt/library")], [])),
            ),
            patch.object(
                server_update,
                "_probe_host_root",
                AsyncMock(return_value=(True, "ok")),
            ),
        ):
            out = await server_update.resolve_validated_host_root()
        assert out["hostRoot"] == "/opt/library"
        assert out["source"] == "env"
        assert out["error"] is None

    asyncio.run(_run())