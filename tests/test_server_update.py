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


def test_docker_status_code_treats_zero_as_success():
    """Regression: ``StatusCode: 0`` must not become 1 via ``x or 1``."""
    assert server_update._docker_status_code({"StatusCode": 0}) == 0
    assert server_update._docker_status_code({"StatusCode": 1}) == 1
    assert server_update._docker_status_code({}) == 1
    assert server_update._docker_status_code(None) == 1


def test_probe_accepts_env_host_root_when_docker_wait_zero(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Host path need not exist in the app container; Docker wait 0 is enough."""
    monkeypatch.setenv("LIBRARY_HOST_ROOT", "/opt/library")
    server_update._probe_ok_cache.clear()

    class _Resp:
        def __init__(self, status_code: int, payload: dict | None = None, content: bytes = b"{}"):
            self.status_code = status_code
            self._payload = payload or {}
            self.content = content
            self.text = content.decode("utf-8", "replace")

        def json(self):
            return self._payload

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url: str, **kwargs):
            if url.endswith("/containers/create"):
                return _Resp(201, {"Id": "abc123"}, b'{"Id":"abc123"}')
            if url.endswith("/start"):
                return _Resp(204, {}, b"")
            if url.endswith("/wait"):
                # Critical: StatusCode 0 must count as success
                return _Resp(200, {"StatusCode": 0}, b'{"StatusCode":0}')
            return _Resp(404, {}, b"")

        async def get(self, url: str, **kwargs):
            return _Resp(200, {}, b"{}")

        async def delete(self, url: str, **kwargs):
            return _Resp(204, {}, b"")

    async def _run():
        with (
            patch.object(server_update.docker_control, "socket_available", return_value=True),
            patch.object(server_update, "_ensure_image", AsyncMock()),
            patch.object(server_update, "_docker_client", return_value=_Client()),
            patch.object(server_update, "_cleanup_container", AsyncMock()),
        ):
            # Local Path must not gate acceptance
            assert not (tmp_path / "opt" / "library").exists()
            ok, detail = await server_update._probe_host_root("/opt/library")
            assert ok is True
            assert detail == "ok"
            out = await server_update.resolve_validated_host_root()
            assert out["hostRoot"] == "/opt/library"
            assert out["source"] == "env"
            assert out["error"] is None

    asyncio.run(_run())


def test_launch_detached_update_does_not_await_or_cleanup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Regression: awaiting/force-deleting the sidecar killed compose mid-recreate."""
    monkeypatch.setattr(server_update, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server_update, "JOB_FILE", tmp_path / "job.json")
    monkeypatch.setattr(server_update, "JOB_LOG", tmp_path / "job.log")
    (tmp_path / "job.json").write_text("{}", encoding="utf-8")
    (tmp_path / "job.log").write_text("", encoding="utf-8")

    cleanup = AsyncMock()

    async def _run():
        with (
            patch.object(
                server_update,
                "_start_update_container",
                AsyncMock(return_value="deadbeefcafebabe"),
            ),
            patch.object(server_update, "_cleanup_container", cleanup),
        ):
            cid = await server_update._launch_detached_update("/opt/library")
        assert cid == "deadbeefcafebabe"
        job = json.loads((tmp_path / "job.json").read_text(encoding="utf-8"))
        assert job["detached"] is True
        assert job["containerId"] == "deadbeefcafebabe"
        assert job["running"] is True
        cleanup.assert_not_called()

    asyncio.run(_run())


def test_start_apply_clears_stale_job_log_on_validation_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr(server_update, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server_update, "JOB_FILE", tmp_path / "job.json")
    monkeypatch.setattr(server_update, "JOB_LOG", tmp_path / "job.log")
    (tmp_path / "job.log").write_text(
        "[admin_server_update] started 2026-08-06T17:12:32Z\nold failure\n",
        encoding="utf-8",
    )
    _write = tmp_path / "job.json"
    _write.write_text(
        json.dumps(
            {
                "phase": "failed",
                "running": False,
                "error": "old",
                "startedAt": "2026-08-06T17:12:32Z",
            }
        ),
        encoding="utf-8",
    )

    async def _run():
        with (
            patch.object(server_update, "is_apply_running", return_value=False),
            patch.object(server_update, "_update_sidecar_running", AsyncMock(return_value=False)),
            patch.object(server_update.docker_control, "socket_available", return_value=True),
            patch.object(
                server_update,
                "resolve_validated_host_root",
                AsyncMock(return_value={"hostRoot": None, "error": "no valid host"}),
            ),
            pytest.raises(RuntimeError, match="no valid host"),
        ):
            await server_update.start_apply()
        job = await server_update.get_job()
        assert job["phase"] == "failed"
        assert "no valid host" in (job.get("error") or "")
        assert "17:12:32" not in (job.get("logTail") or "")
        assert "no valid host" in (job.get("logTail") or "")

    asyncio.run(_run())