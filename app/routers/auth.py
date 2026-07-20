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
from app.utils.email_norm import is_valid_email, normalize_email, username_from_email
from app.utils.themes import DEFAULT_THEME, THEME_IDS, normalize_theme
from app.services import push

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    """Prefer email; username kept for backward-compatible clients."""
    email: str | None = None
    username: str | None = None
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    role: str
    username: str
    email: str | None = None
    must_change_password: bool = False
    must_set_email: bool = False


class SetupRequest(BaseModel):
    email: str
    password: str
    # Optional display name; defaults from email local-part.
    username: str | None = None


class InviteSignupRequest(BaseModel):
    invite_code: str
    email: str
    password: str
    username: str | None = None


class InvitePreviewResponse(BaseModel):
    valid: bool = True
    code: str
    library_name: str
    cover_url: str | None = None


class RefreshRequest(BaseModel):
    refresh_token: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class SetEmailRequest(BaseModel):
    email: str
    password: str


class MeResponse(BaseModel):
    username: str
    email: str | None = None
    role: str
    must_change_password: bool = False
    must_set_email: bool = False


def _normalize_invite_code(raw: str) -> str:
    return (raw or "").strip().upper()


def _must_set_email(user: User) -> bool:
    return not is_valid_email(normalize_email(user.email))


def _token_response(user: User) -> TokenResponse:
    return TokenResponse(
        access_token=create_access_token(user.id, user.role),
        refresh_token=create_refresh_token(user.id),
        role=user.role,
        username=user.username,
        email=user.email,
        must_change_password=user.must_change_password,
        must_set_email=_must_set_email(user),
    )


async def _find_user_for_login(db: AsyncSession, email: str | None, username: str | None) -> User | None:
    em = normalize_email(email)
    un = (username or "").strip()
    # Allow typing a legacy username into the email field.
    if em and not is_valid_email(em) and not un:
        un = em
        em = ""

    if em and is_valid_email(em):
        user = (
            await db.execute(select(User).where(User.email == em))
        ).scalar_one_or_none()
        if user:
            return user
        # Legacy: some installs used email as username before the email column.
        user = (
            await db.execute(select(User).where(User.username == em))
        ).scalar_one_or_none()
        if user:
            return user

    if un:
        # Case-insensitive username match (SQLite / Postgres).
        from sqlalchemy import func as sa_func

        user = (
            await db.execute(
                select(User).where(sa_func.lower(User.username) == un.lower())
            )
        ).scalar_one_or_none()
        if user:
            return user
    return None


def _require_email_password(email_raw: str | None, password: str) -> str:
    email = normalize_email(email_raw)
    if not is_valid_email(email):
        raise HTTPException(status_code=400, detail="Enter a valid email address")
    if len(password or "") < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    return email


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
        email=user.email,
        role=user.role,
        must_change_password=user.must_change_password,
        must_set_email=_must_set_email(user),
    )


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)):
    # Prefer email; also accept username in either field for legacy accounts.
    user = await _find_user_for_login(db, body.email, body.username)
    if not user or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is disabled")
    # Soft-upgrade: attach email when the login identifier is a real email.
    em = normalize_email(body.email)
    if not user.email and is_valid_email(em):
        taken = (
            await db.execute(select(User.id).where(User.email == em, User.id != user.id))
        ).scalar_one_or_none()
        if taken is None:
            user.email = em
            await db.commit()
            await db.refresh(user)

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

    return _token_response(user)


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

    email = _require_email_password(body.email, body.password or "")
    username = (body.username or "").strip() or username_from_email(email)
    if len(username) < 2 or len(username) > 64:
        raise HTTPException(status_code=400, detail="Display name must be 2–64 characters")

    user = User(
        username=username,
        email=email,
        hashed_password=hash_password(body.password),
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
    cover = f"/api/libraries/{group.id}/cover" if group.cover_path else None
    return InvitePreviewResponse(
        valid=True,
        code=group.invite_code,
        library_name=group.name,
        cover_url=cover,
    )


@router.post("/signup-with-invite", response_model=TokenResponse)
async def signup_with_invite(
    body: InviteSignupRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Create an account + join a library in one step (shared invite link flow)."""
    group = await _library_for_invite(body.invite_code, db)

    email = _require_email_password(body.email, body.password or "")
    username = (body.username or "").strip() or username_from_email(email)
    if len(username) < 2 or len(username) > 64:
        raise HTTPException(status_code=400, detail="Display name must be 2–64 characters")

    existing_email = await db.execute(select(User).where(User.email == email))
    if existing_email.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="An account with this email already exists")

    existing = await db.execute(select(User).where(User.username == username))
    if existing.scalar_one_or_none():
        # Collision on display name — fall back to email as username.
        username = email[:64]
        existing2 = await db.execute(select(User).where(User.username == username))
        if existing2.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="An account with this email already exists")

    user = User(
        username=username,
        email=email,
        hashed_password=hash_password(body.password),
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
            "body": f"{email} joined {group.name} via invite",
            "url": "/admin?tab=users",
        },
    )

    return _token_response(user)


class UserSettingsResponse(BaseModel):
    private_mode: bool = False
    preferred_debrid: str = "rd"
    available_debrid_providers: list[str] = []
    # Personal override; null = follow library default
    theme: str | None = None
    library_default_theme: str = DEFAULT_THEME
    effective_theme: str = DEFAULT_THEME
    available_themes: list[str] = list(THEME_IDS)


class UpdateSettingsRequest(BaseModel):
    private_mode: bool | None = None
    preferred_debrid: str | None = None
    # Pass null / "default" to clear personal override
    theme: str | None = None
    clear_theme: bool = False

async def _settings_response(user: User, db: AsyncSession | None = None) -> UserSettingsResponse:
    from app.services import debrid, debrid_tokens
    from app.models import LibraryGroup

    await debrid_tokens.apply_tokens_for_user_id(user.id)
    lib_theme = DEFAULT_THEME
    if user.library_group_id and db is not None:
        group = (
            await db.execute(select(LibraryGroup).where(LibraryGroup.id == user.library_group_id))
        ).scalar_one_or_none()
        if group:
            lib_theme = normalize_theme(getattr(group, "default_theme", None)) or DEFAULT_THEME
    user_theme = normalize_theme(getattr(user, "theme", None), allow_null=True)
    effective = user_theme or lib_theme
    return UserSettingsResponse(
        private_mode=user.private_mode,
        preferred_debrid=getattr(user, "preferred_debrid", "rd") or "rd",
        available_debrid_providers=debrid.available_providers(),
        theme=user_theme,
        library_default_theme=lib_theme,
        effective_theme=effective,
        available_themes=list(THEME_IDS),
    )


@router.get("/settings", response_model=UserSettingsResponse)
async def get_settings_endpoint(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await _settings_response(user, db)


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
    if body.clear_theme:
        user.theme = None
    elif body.theme is not None:
        # Empty string or "default" clears override
        if not str(body.theme).strip() or str(body.theme).strip().lower() in ("default", "library", "auto"):
            user.theme = None
        else:
            tid = normalize_theme(body.theme, allow_null=True)
            if tid is None:
                raise HTTPException(status_code=400, detail="Unknown theme")
            user.theme = tid
    await db.commit()
    await db.refresh(user)
    return await _settings_response(user, db)


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


@router.post("/set-email", response_model=TokenResponse)
async def set_email(
    body: SetEmailRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Attach an email to a legacy username-only account (required for future logins)."""
    if not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Password is incorrect")
    if user.email and is_valid_email(normalize_email(user.email)):
        raise HTTPException(status_code=400, detail="This account already has an email")

    email = normalize_email(body.email)
    if not is_valid_email(email):
        raise HTTPException(status_code=400, detail="Enter a valid email address")
    taken = (
        await db.execute(select(User.id).where(User.email == email, User.id != user.id))
    ).scalar_one_or_none()
    if taken is not None:
        raise HTTPException(status_code=400, detail="An account with this email already exists")

    user.email = email
    await db.commit()
    await db.refresh(user)
    return _token_response(user)
