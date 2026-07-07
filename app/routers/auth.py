from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import User, AccountRequest
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


class AccountRequestCreate(BaseModel):
    username: str
    email: str | None = None
    reason: str | None = None


class AccountRequestStatus(BaseModel):
    status: str
    username: str
    deny_reason: str | None = None
    temp_password: str | None = None


class SetupRequest(BaseModel):
    username: str
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class MeResponse(BaseModel):
    username: str
    role: str
    must_change_password: bool = False


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

    return TokenResponse(
        access_token=create_access_token(user.id, user.role),
        refresh_token=create_refresh_token(user.id),
        role=user.role,
        username=user.username,
        must_change_password=user.must_change_password,
    )


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

    user = User(
        username=body.username,
        hashed_password=hash_password(body.password),
        role="admin",
    )
    db.add(user)
    await db.commit()

    return TokenResponse(
        access_token=create_access_token(user.id, user.role),
        refresh_token=create_refresh_token(user.id),
        role=user.role,
        username=user.username,
    )


@router.post("/request-account")
async def request_account(
    body: AccountRequestCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    existing = await db.execute(
        select(AccountRequest).where(
            AccountRequest.username == body.username,
            AccountRequest.status == "pending",
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="A pending request for this username already exists")

    user_exists = await db.execute(select(User).where(User.username == body.username))
    if user_exists.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="This username is already taken")

    req = AccountRequest(
        username=body.username,
        email=body.email,
        reason=body.reason,
    )
    db.add(req)
    await db.commit()

    background_tasks.add_task(
        push.notify_admins_background,
        {
            "type": "account_request",
            "title": "New Account Request",
            "body": f"{body.username} wants to join" + (f": {body.reason}" if body.reason else ""),
            "url": "/admin?tab=approvals",
        },
    )

    return {"token": req.token, "message": "Account request submitted. You'll be notified when it's reviewed."}


@router.get("/account-status/{token}", response_model=AccountRequestStatus)
async def check_account_status(token: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(AccountRequest).where(AccountRequest.token == token))
    req = result.scalar_one_or_none()
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")

    return AccountRequestStatus(
        status=req.status,
        username=req.username,
        deny_reason=req.deny_reason,
        temp_password=req.temp_password if req.status == "approved" else None,
    )


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

    # The temp password has served its purpose — stop storing it in plaintext.
    reqs = (
        await db.execute(
            select(AccountRequest).where(
                AccountRequest.username == user.username,
                AccountRequest.temp_password.is_not(None),
            )
        )
    ).scalars().all()
    for r in reqs:
        r.temp_password = None

    await db.commit()
    return {"message": "Password updated"}
