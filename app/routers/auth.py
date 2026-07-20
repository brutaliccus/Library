from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import User, LibraryGroup
from app.utils.auth import (
    hash_password,
    verify_password,
    create_access_token,
    create_refresh_token,
    get_current_user,
)
from app.services import push

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    role: str
    username: str
    must_change_password: bool = False


class SetupRequest(BaseModel):
    username: str
    password: str


class InviteSignupRequest(BaseModel):
    invite_code: str
    username: str
    password: str


class InvitePreviewResponse(BaseModel):
    valid: bool = True
    code: str
    library_name: str


class RefreshRequest(BaseModel):
    refresh_token: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class MeResponse(BaseModel):
    username: str
    role: str
    must_change_password: bool = False


def _normalize_invite_code(raw: str) -> str:
    return (raw or "").strip().upper()


def _token_response(user: User) -> TokenResponse:
    return TokenResponse(
        access_token=create_access_token(user.id, user.role),
        refresh_token=create_refresh_token(user.id),
        role=user.role,
        username=user.username,
        must_change_password=user.must_change_password,
    )


async def _library_for_invite(code: str, db: AsyncSession) -> LibraryGroup:
    normalized = _normalize_invite_code(code)
    if not normalized or len(normalized) < 6:
        raise HTTPException(status_code=400, detail="Invalid invite code")
    group = (
        await db.execute(select(LibraryGroup).where(LibraryGroup.invite_code == normalized))
    ).scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Invalid invite code")
    return group


@router.get("/me", response_model=MeResponse)
async def me(user: User = Depends(get_current_user)):
    return MeResponse(
        username=user.username,
        role=user.role,
        must_change_password=user.must_change_password,
    )


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.username == body.username))
    user = result.scalar_one_or_none()
    if not user or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is disabled")

    return _token_response(user)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(body: RefreshRequest, db: AsyncSession = Depends(get_db)):
    from jose import JWTError, jwt
    from app.config import get_settings

    settings = get_settings()
    try:
        payload = jwt.decode(body.refresh_token, settings.secret_key, algorithms=["HS256"])
        if payload.get("type") != "refresh":
            raise HTTPException(status_code=401, detail="Invalid token type")
        user_id = int(payload["sub"])
    except (JWTError, KeyError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or disabled")

    return TokenResponse(
        access_token=create_access_token(user.id, user.role),
        refresh_token=create_refresh_token(user.id),
        role=user.role,
        username=user.username,
        must_change_password=user.must_change_password,
    )


@router.get("/setup-required")
async def check_setup(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(func.count(User.id)))
    count = result.scalar()
    return {"setup_required": count == 0}


@router.post("/setup", response_model=TokenResponse)
async def initial_setup(body: SetupRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(func.count(User.id)))
    if result.scalar() > 0:
        raise HTTPException(status_code=400, detail="Setup already completed")

    username = (body.username or "").strip()
    password = body.password or ""
    if len(username) < 2 or len(username) > 64:
        raise HTTPException(status_code=400, detail="Username must be 2–64 characters")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    user = User(
        username=username,
        hashed_password=hash_password(password),
        role="admin",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    return _token_response(user)


@router.get("/invite/{code}", response_model=InvitePreviewResponse)
async def preview_invite(code: str, db: AsyncSession = Depends(get_db)):
    """Public: validate an invite code and return the library name for the join screen."""
    group = await _library_for_invite(code, db)
    return InvitePreviewResponse(
        valid=True,
        code=group.invite_code,
        library_name=group.name,
    )


@router.post("/signup-with-invite", response_model=TokenResponse)
async def signup_with_invite(
    body: InviteSignupRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Create an account + join a library in one step (shared invite link flow)."""
    group = await _library_for_invite(body.invite_code, db)

    username = (body.username or "").strip()
    password = body.password or ""
    if len(username) < 2 or len(username) > 64:
        raise HTTPException(status_code=400, detail="Username must be 2–64 characters")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    existing = await db.execute(select(User).where(User.username == username))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="This username is already taken")

    user = User(
        username=username,
        hashed_password=hash_password(password),
        role="user",
        must_change_password=False,
        library_group_id=group.id,
        library_role="member",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    background_tasks.add_task(
        push.notify_admins_background,
        {
            "type": "invite_signup",
            "title": "New member joined",
            "body": f"{username} joined {group.name} via invite",
            "url": "/admin?tab=users",
        },
    )

    return _token_response(user)


class UserSettingsResponse(BaseModel):
    private_mode: bool = False
    preferred_debrid: str = "rd"
    available_debrid_providers: list[str] = []


class UpdateSettingsRequest(BaseModel):
    private_mode: bool | None = None
    preferred_debrid: str | None = None


async def _settings_response(user: User) -> UserSettingsResponse:
    from app.services import debrid, debrid_tokens
    # Providers reflect the user's library-group keys (env fallback for default group)
    await debrid_tokens.apply_tokens_for_user_id(user.id)
    return UserSettingsResponse(
        private_mode=user.private_mode,
        preferred_debrid=getattr(user, "preferred_debrid", "rd") or "rd",
        available_debrid_providers=debrid.available_providers(),
    )


@router.get("/settings", response_model=UserSettingsResponse)
async def get_settings_endpoint(user: User = Depends(get_current_user)):
    return await _settings_response(user)


@router.put("/settings", response_model=UserSettingsResponse)
async def update_settings(
    body: UpdateSettingsRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if body.private_mode is not None:
        user.private_mode = body.private_mode
    if body.preferred_debrid is not None:
        from app.services import debrid
        user.preferred_debrid = debrid.normalize_provider(body.preferred_debrid)
    await db.commit()
    return await _settings_response(user)


@router.post("/change-password")
async def change_password(
    body: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not verify_password(body.current_password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    user.hashed_password = hash_password(body.new_password)
    user.must_change_password = False
    await db.commit()
    return {"message": "Password updated"}
