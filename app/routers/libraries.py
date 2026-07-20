"""Library groups: shared debrid-API-key pools with invite codes.

Every user belongs to a group. The group's Real-Debrid/Torbox keys are used
for that user's streaming and downloads (empty keys = server env fallback,
which is how the original/default library keeps working). Downloaded books in
ABS/Kavita stay shared across all groups.

Roles: owner (manages keys, members, invite code) > admin (sees/shares the
invite code) > member.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import LibraryGroup, User, _invite_code
from app.utils.auth import get_current_user
from app.services import debrid_tokens, real_debrid, torbox

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/libraries", tags=["libraries"])


class CreateGroupRequest(BaseModel):
    name: str
    real_debrid_api_token: str = ""
    torbox_api_token: str = ""


class JoinGroupRequest(BaseModel):
    invite_code: str


class UpdateTokensRequest(BaseModel):
    real_debrid_api_token: str | None = None
    torbox_api_token: str | None = None


class MemberRoleRequest(BaseModel):
    library_role: str  # "admin" | "member"


async def _get_group(user: User, db: AsyncSession) -> LibraryGroup | None:
    if not user.library_group_id:
        return None
    return (
        await db.execute(select(LibraryGroup).where(LibraryGroup.id == user.library_group_id))
    ).scalar_one_or_none()


def _require_owner(user: User):
    if user.library_role != "owner":
        raise HTTPException(status_code=403, detail="Only the library owner can do this")


def _token_sources(group: LibraryGroup) -> dict[str, str]:
    """Per-provider key source: group (library DB), server (.env), or none."""
    from app.config import get_settings

    env = get_settings()
    rd = "group" if group.real_debrid_api_token else ("server" if env.real_debrid_api_token else "none")
    tb = "group" if group.torbox_api_token else ("server" if env.torbox_api_token else "none")
    return {"rd": rd, "torbox": tb}


async def _public_app_base() -> str:
    """Canonical public origin for invite links (APP_URL / Admin → Config → App URL)."""
    base = ""
    try:
        from app.services import instance_settings

        base = (await instance_settings.get_effective("config.app_url")).strip()
    except Exception:
        base = ""
    if not base:
        from app.config import get_settings

        base = (get_settings().app_url or "").strip()
    return base.rstrip("/")


def _invite_link_for(code: str, base: str) -> str | None:
    code = (code or "").strip().upper()
    if not code or not base:
        return None
    if "library.example.com" in base.lower():
        # Placeholder APP_URL — still return a link shape so clients don't fall
        # back to a bare code; ops should set APP_URL to the real public host.
        pass
    return f"{base}/join/{code}"


async def _serialize_group(group: LibraryGroup, user: User, db: AsyncSession) -> dict:
    can_invite = user.library_role in ("owner", "admin")
    sources = _token_sources(group)
    invite_code = group.invite_code if can_invite else None
    invite_link = None
    if invite_code:
        invite_link = _invite_link_for(invite_code, await _public_app_base())
    out: dict = {
        "id": group.id,
        "name": group.name,
        "role": user.library_role,
        "isOwner": group.owner_user_id == user.id,
        "canManageKeys": user.library_role == "owner",
        "hasRdToken": sources["rd"] != "none",
        "hasTorboxToken": sources["torbox"] != "none",
        "rdKeySource": sources["rd"],
        "torboxKeySource": sources["torbox"],
        "usesServerKeys": sources["rd"] == "server" or sources["torbox"] == "server",
        "inviteCode": invite_code,
        "inviteLink": invite_link,
    }
    if can_invite:
        members = (
            await db.execute(
                select(User)
                .where(User.library_group_id == group.id)
                .order_by(User.created_at)
            )
        ).scalars().all()
        out["members"] = [
            {
                "id": m.id,
                "username": m.username,
                "libraryRole": m.library_role,
                "isOwner": m.id == group.owner_user_id,
            }
            for m in members
        ]
    return out


@router.get("/me")
async def my_library_group(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The user's library group, or {"library": null} when onboarding is needed."""
    group = await _get_group(user, db)
    if not group:
        return {"library": None}
    return {"library": await _serialize_group(group, user, db)}


async def _validate_tokens(rd_token: str, torbox_token: str) -> None:
    """Reject obviously-bad API keys up front so users don't onboard into a broken library."""
    if rd_token:
        debrid_tokens.set_tokens(rd=rd_token)
        try:
            await real_debrid.get_user_info()
        except Exception:
            raise HTTPException(status_code=400, detail="Real-Debrid API key was rejected by Real-Debrid")
        finally:
            debrid_tokens.clear_tokens()
    if torbox_token:
        debrid_tokens.set_tokens(torbox=torbox_token)
        try:
            await torbox.get_user_info()
        except Exception:
            raise HTTPException(status_code=400, detail="Torbox API key was rejected by Torbox")
        finally:
            debrid_tokens.clear_tokens()


async def _ensure_can_leave(user: User, db: AsyncSession) -> None:
    """Owners can't abandon a group that still has other members."""
    if not user.library_group_id or user.library_role != "owner":
        return
    others = (
        await db.execute(
            select(User.id)
            .where(User.library_group_id == user.library_group_id)
            .where(User.id != user.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if others is not None:
        raise HTTPException(
            status_code=400,
            detail="You own a library with other members. Promote a new owner or remove members first.",
        )


@router.post("/create")
async def create_group(
    body: CreateGroupRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a library group with your own API keys and become its owner."""
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Library name is required")
    if not body.real_debrid_api_token.strip() and not body.torbox_api_token.strip():
        raise HTTPException(
            status_code=400,
            detail="Provide a Real-Debrid or Torbox API key (at least one)",
        )
    await _ensure_can_leave(user, db)
    await _validate_tokens(body.real_debrid_api_token.strip(), body.torbox_api_token.strip())

    old_group_id = user.library_group_id
    group = LibraryGroup(
        name=name,
        owner_user_id=user.id,
        real_debrid_api_token=body.real_debrid_api_token.strip(),
        torbox_api_token=body.torbox_api_token.strip(),
    )
    db.add(group)
    await db.flush()
    user.library_group_id = group.id
    user.library_role = "owner"
    await db.commit()
    await _cleanup_empty_group(old_group_id, db)
    return {"library": await _serialize_group(group, user, db)}


@router.post("/join")
async def join_group(
    body: JoinGroupRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Join an existing library group with an invite code (uses its API keys)."""
    code = body.invite_code.strip().upper()
    if not code:
        raise HTTPException(status_code=400, detail="Invite code is required")
    group = (
        await db.execute(select(LibraryGroup).where(LibraryGroup.invite_code == code))
    ).scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Invalid invite code")
    if group.id == user.library_group_id:
        raise HTTPException(status_code=400, detail="You're already in this library")
    await _ensure_can_leave(user, db)

    old_group_id = user.library_group_id
    user.library_group_id = group.id
    user.library_role = "member"
    await db.commit()
    await _cleanup_empty_group(old_group_id, db)
    return {"library": await _serialize_group(group, user, db)}


async def _cleanup_empty_group(group_id: int | None, db: AsyncSession) -> None:
    """Delete a group that no longer has any members (owner moved away)."""
    if not group_id:
        return
    remaining = (
        await db.execute(select(User.id).where(User.library_group_id == group_id).limit(1))
    ).scalar_one_or_none()
    if remaining is None:
        group = (
            await db.execute(select(LibraryGroup).where(LibraryGroup.id == group_id))
        ).scalar_one_or_none()
        if group:
            await db.delete(group)
            await db.commit()


@router.put("/tokens")
async def update_tokens(
    body: UpdateTokensRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update the group's API keys (owner only)."""
    _require_owner(user)
    group = await _get_group(user, db)
    if not group:
        raise HTTPException(status_code=404, detail="You're not in a library")

    rd = body.real_debrid_api_token.strip() if body.real_debrid_api_token is not None else None
    tb = body.torbox_api_token.strip() if body.torbox_api_token is not None else None
    await _validate_tokens(rd or "", tb or "")

    if rd is not None:
        group.real_debrid_api_token = rd
    if tb is not None:
        group.torbox_api_token = tb
    await db.commit()
    return {"library": await _serialize_group(group, user, db)}


@router.post("/regenerate-invite")
async def regenerate_invite(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Rotate the invite code (owner only) — old code stops working."""
    _require_owner(user)
    group = await _get_group(user, db)
    if not group:
        raise HTTPException(status_code=404, detail="You're not in a library")
    group.invite_code = _invite_code()
    await db.commit()
    base = await _public_app_base()
    return {
        "inviteCode": group.invite_code,
        "inviteLink": _invite_link_for(group.invite_code, base),
    }


@router.post("/members/{member_id}/role")
async def set_member_role(
    member_id: int,
    body: MemberRoleRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Promote a member to admin (can invite others) or demote back (owner only)."""
    _require_owner(user)
    if body.library_role not in ("admin", "member"):
        raise HTTPException(status_code=400, detail="Role must be 'admin' or 'member'")
    group = await _get_group(user, db)
    if not group:
        raise HTTPException(status_code=404, detail="You're not in a library")

    member = (
        await db.execute(select(User).where(User.id == member_id))
    ).scalar_one_or_none()
    if not member or member.library_group_id != group.id:
        raise HTTPException(status_code=404, detail="Member not found in your library")
    if member.id == group.owner_user_id:
        raise HTTPException(status_code=400, detail="The owner's role can't be changed")

    member.library_role = body.library_role
    await db.commit()
    return {"status": "ok", "memberId": member.id, "libraryRole": member.library_role}
