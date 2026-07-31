from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status, BackgroundTasks
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
    # True when this user may upload owned audiobooks (admin always, else setting).
    allow_user_audiobook_upload: bool = False
    # True when this user may create public book share links (admins always).
    can_share_books: bool = False


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
    from app.services import library_ingest

    can_upload = await library_ingest.user_may_upload_owned(user)
    can_share = (user.role or "").lower() == "admin" or bool(
        getattr(user, "can_share_books", False)
    )
    return MeResponse(
        username=user.username,
        email=user.email,
        role=user.role,
        must_change_password=user.must_change_password,
        must_set_email=_must_set_email(user),
        allow_user_audiobook_upload=can_upload,
        can_share_books=can_share,
    )


@router.post("/heartbeat")
async def heartbeat(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Lightweight presence ping from the SPA while the tab is open/focused."""
    now = datetime.now(timezone.utc)
    user.last_seen_at = now
    try:
        from app.services.admin_whitelist import client_ip_from_request

        ip = client_ip_from_request(request)
        if ip and ip != (user.last_client_ip or ""):
            user.last_client_ip = ip
    except Exception:
        pass
    await db.commit()
    return {"ok": True, "last_seen_at": now.isoformat()}


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
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
    user.last_seen_at = datetime.now(timezone.utc)
    try:
        from app.services.admin_whitelist import client_ip_from_request

        ip = client_ip_from_request(request)
        if ip:
            user.last_client_ip = ip
    except Exception:
        pass
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
    default_playback_rate: float = 1.0


class UpdateSettingsRequest(BaseModel):
    private_mode: bool | None = None
    preferred_debrid: str | None = None
    # Pass null / "default" to clear personal override
    theme: str | None = None
    clear_theme: bool = False
    default_playback_rate: float | None = None

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
            lib_theme = (
                normalize_theme(getattr(group, "default_theme", None), allow_custom=False)
                or DEFAULT_THEME
            )
    user_theme = normalize_theme(getattr(user, "theme", None), allow_null=True)
    effective = user_theme or lib_theme
    rate = float(getattr(user, "default_playback_rate", 1.0) or 1.0)
    rate = max(0.5, min(3.0, rate))
    return UserSettingsResponse(
        private_mode=user.private_mode,
        preferred_debrid=getattr(user, "preferred_debrid", "rd") or "rd",
        available_debrid_providers=debrid.available_providers(),
        theme=user_theme,
        library_default_theme=lib_theme,
        effective_theme=effective,
        available_themes=list(THEME_IDS),
        default_playback_rate=rate,
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
    if body.default_playback_rate is not None:
        user.default_playback_rate = max(0.5, min(3.0, float(body.default_playback_rate)))
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


# --------------- Ereader / OPDS ---------------


class EreaderSendBody(BaseModel):
    series_id: int
    chapter_id: int
    title: str = ""
    author: str = ""
    cover_url: str = ""


async def _ereader_payload(user: User, db: AsyncSession) -> dict:
    from app.services import opds as opds_svc

    token = await opds_svc.ensure_user_opds_token(db, user)
    short = await opds_svc.ensure_user_opds_short_code(db, user)
    base = await opds_svc.public_app_base()
    # Prefer short /o/{code} URL for ereader typing; keep long path as alternate.
    root = f"{base}/o/{short}" if base and short else ""
    legacy_root = f"{base}/api/opds/{token}" if base and token else ""
    items = await opds_svc.list_shelf_items(db, user.id)
    return {
        "opdsUrl": root,
        "opdsUrlLegacy": legacy_root,
        "shortCode": short,
        "shelfUrl": f"{root}/shelf" if root else "",
        "libraryUrl": f"{root}/library" if root else "",
        "tokenConfigured": bool(token and short),
        "appUrlConfigured": bool(base),
        "shelfCount": len(items),
        "shelf": [
            {
                "id": it.id,
                "seriesId": it.kavita_series_id,
                "chapterId": it.kavita_chapter_id,
                "title": it.title,
                "author": it.author,
                "coverUrl": it.cover_url,
                "downloadUrl": f"{root}/download/{it.kavita_chapter_id}" if root else "",
                "addedAt": it.added_at.isoformat() if it.added_at else None,
            }
            for it in items
        ],
        "instructions": {
            "koreader": (
                "KOReader: Search → OPDS catalog → Add → paste your OPDS URL. "
                "Open “Send to ereader” (or All ebooks) and download."
            ),
            "moonreader": (
                "Moon+ Reader: Net Library → New catalog → paste OPDS URL (leave login blank)."
            ),
            "kindle": (
                "Kindle does not use OPDS. Use Send to Kindle email or copy the EPUB "
                "download link from an ebook’s Send to ereader action."
            ),
        },
    }


@router.get("/ereader")
async def get_ereader_settings(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Personal OPDS catalog URL + ereader shelf for connecting devices."""
    return await _ereader_payload(user, db)


@router.post("/ereader/rotate-token")
async def rotate_ereader_token(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Invalidate the current OPDS URL and issue a new token."""
    from app.services import opds as opds_svc

    await opds_svc.rotate_user_opds_token(db, user)
    return await _ereader_payload(user, db)


@router.post("/ereader/shelf")
async def add_ereader_shelf_item(
    body: EreaderSendBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Add (or bump) an ebook on the user's Send to ereader OPDS shelf."""
    from app.services import opds as opds_svc

    if body.series_id <= 0 or body.chapter_id <= 0:
        raise HTTPException(status_code=400, detail="series_id and chapter_id are required")
    await opds_svc.ensure_user_opds_token(db, user)
    item = await opds_svc.add_shelf_item(
        db,
        user=user,
        series_id=body.series_id,
        chapter_id=body.chapter_id,
        title=body.title,
        author=body.author,
        cover_url=body.cover_url,
    )
    payload = await _ereader_payload(user, db)
    download_url = next(
        (s["downloadUrl"] for s in payload["shelf"] if s["id"] == item.id),
        "",
    )
    return {
        **payload,
        "added": {
            "id": item.id,
            "seriesId": item.kavita_series_id,
            "chapterId": item.kavita_chapter_id,
            "title": item.title,
            "downloadUrl": download_url,
        },
    }


@router.delete("/ereader/shelf/{item_id}")
async def delete_ereader_shelf_item(
    item_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.services import opds as opds_svc

    ok = await opds_svc.remove_shelf_item(db, user.id, item_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Shelf item not found")
    return await _ereader_payload(user, db)
