"""Per-request debrid token resolution.

Users belong to a LibraryGroup that can carry its own Real-Debrid/Torbox API
keys. Entry points (stream resolve, library resolve, proxy refresh, download
pipeline) call apply_tokens_for_user_id() before touching a debrid client; the
clients read the tokens from contextvars, falling back to the process Settings
(env + Admin Config runtime overrides).

Server-default tokens also live in ``app_settings`` (Admin → Config). Those are
resolved via ``instance_settings.get_effective`` and written into the context so
provider pick still sees TorBox when the library group has empty keys (common
after tokens were moved to instance config).

contextvars propagate into asyncio.create_task(), so background resolvers
spawned from a request inherit the requesting user's tokens automatically.
"""

import logging
from contextvars import ContextVar

from sqlalchemy import or_, select

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_rd_token: ContextVar[str] = ContextVar("rd_token", default="")
_torbox_token: ContextVar[str] = ContextVar("torbox_token", default="")


def rd_token() -> str:
    return _rd_token.get() or settings.real_debrid_api_token


def torbox_token() -> str:
    return _torbox_token.get() or settings.torbox_api_token


def set_tokens(rd: str = "", torbox: str = "") -> None:
    _rd_token.set(rd or "")
    _torbox_token.set(torbox or "")


def clear_tokens() -> None:
    set_tokens("", "")


async def _effective_server_tokens() -> tuple[str, str]:
    """Server-default RD/TorBox from app_settings DB, with env Settings fallback."""
    try:
        from app.services import instance_settings

        rd = (await instance_settings.get_effective("config.real_debrid_api_token") or "").strip()
        tb = (await instance_settings.get_effective("config.torbox_api_token") or "").strip()
        return rd, tb
    except Exception as e:
        logger.warning("effective server debrid tokens failed: %s", e)
        return (
            (settings.real_debrid_api_token or "").strip(),
            (settings.torbox_api_token or "").strip(),
        )


async def apply_server_debrid_tokens() -> None:
    """Resolve debrid tokens for background jobs (scraper cache enrichment).

    Prefers Admin Config / env server defaults; fills any gaps from library
    groups so Torbox/RD keys stored only on a group still enrich the cache.
    """
    clear_tokens()
    rd, torbox = await _effective_server_tokens()
    if rd and torbox:
        set_tokens(rd, torbox)
        return
    try:
        from app.database import async_session
        from app.models import LibraryGroup

        async with async_session() as db:
            groups = (
                await db.execute(
                    select(LibraryGroup)
                    .where(
                        or_(
                            LibraryGroup.real_debrid_api_token != "",
                            LibraryGroup.torbox_api_token != "",
                        )
                    )
                    .order_by(LibraryGroup.id.asc())
                )
            ).scalars().all()
            for group in groups:
                if not rd and group.real_debrid_api_token:
                    rd = (group.real_debrid_api_token or "").strip()
                if not torbox and group.torbox_api_token:
                    torbox = (group.torbox_api_token or "").strip()
                if rd and torbox:
                    break
        set_tokens(rd, torbox)
    except Exception as e:
        logger.warning("apply_server_debrid_tokens failed: %s", e)
        set_tokens(rd, torbox)


async def apply_tokens_for_user_id(user_id: int | None) -> None:
    """Load group tokens (when set) into context, else server-default tokens.

    Empty group keys must not wipe Admin Config / env TorBox — otherwise
    preferred=TorBox is reported as "not configured" and Real-Debrid always wins.
    """
    clear_tokens()
    server_rd, server_tb = await _effective_server_tokens()
    if not user_id:
        set_tokens(server_rd, server_tb)
        return
    try:
        from app.database import async_session
        from app.models import LibraryGroup, User

        async with async_session() as db:
            user = (
                await db.execute(select(User).where(User.id == user_id))
            ).scalar_one_or_none()
            if not user or not user.library_group_id:
                set_tokens(server_rd, server_tb)
                return
            group = (
                await db.execute(
                    select(LibraryGroup).where(LibraryGroup.id == user.library_group_id)
                )
            ).scalar_one_or_none()
            if not group:
                set_tokens(server_rd, server_tb)
                return
            rd = (group.real_debrid_api_token or "").strip() or server_rd
            tb = (group.torbox_api_token or "").strip() or server_tb
            set_tokens(rd, tb)
    except Exception as e:
        logger.warning("Failed to load debrid tokens for user %s: %s", user_id, e)
        set_tokens(server_rd, server_tb)
