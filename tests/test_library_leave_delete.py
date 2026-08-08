"""Leave keeps empty libraries; delete requires owner password."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.routers import libraries as libraries_router


def _user(*, group_id=1, role="owner", user_id=1):
    return SimpleNamespace(
        id=user_id,
        library_group_id=group_id,
        library_role=role,
        hashed_password="hashed",
    )


def test_leave_does_not_delete_empty_group():
    user = _user()
    db = AsyncMock()
    db.commit = AsyncMock()

    async def _run():
        with patch.object(libraries_router, "_ensure_can_leave", new=AsyncMock()):
            return await libraries_router.leave_group(user=user, db=db)

    result = asyncio.run(_run())
    assert result == {"status": "ok", "library": None}
    assert user.library_group_id is None
    db.commit.assert_awaited()
    db.delete.assert_not_called()


def test_delete_requires_correct_password():
    user = _user()
    group = SimpleNamespace(id=1, owner_user_id=1, cover_path=None)
    db = AsyncMock()

    async def _run():
        with (
            patch.object(libraries_router, "_get_group", new=AsyncMock(return_value=group)),
            patch("app.utils.auth.verify_password", return_value=False),
        ):
            await libraries_router.delete_group(
                body=libraries_router.DeleteGroupRequest(password="wrong"),
                user=user,
                db=db,
            )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(_run())
    assert exc.value.status_code == 400


def test_delete_removes_group_and_detaches_members():
    user = _user()
    member = _user(group_id=1, role="member", user_id=2)
    group = SimpleNamespace(id=1, owner_user_id=1, cover_path=None)
    db = AsyncMock()
    db.commit = AsyncMock()
    db.delete = AsyncMock()

    members_result = MagicMock()
    members_result.scalars.return_value.all.return_value = [user, member]
    db.execute = AsyncMock(return_value=members_result)

    async def _run():
        with (
            patch.object(libraries_router, "_get_group", new=AsyncMock(return_value=group)),
            patch("app.utils.auth.verify_password", return_value=True),
        ):
            return await libraries_router.delete_group(
                body=libraries_router.DeleteGroupRequest(password="secret"),
                user=user,
                db=db,
            )

    result = asyncio.run(_run())
    assert result == {"status": "ok", "deletedLibraryId": 1}
    assert user.library_group_id is None
    assert member.library_group_id is None
    db.delete.assert_awaited_with(group)
    db.commit.assert_awaited()
