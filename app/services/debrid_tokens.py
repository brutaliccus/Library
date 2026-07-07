"""Per-request debrid token resolution.

Users belong to a LibraryGroup that can carry its own Real-Debrid/Torbox API
keys. Entry points (stream resolve, library resolve, proxy refresh, download
pipeline) call apply_tokens_for_user() before touching a debrid client; the
clients read the tokens from contextvars, falling back to the server-wide env
tokens (which is what the default/original library uses).

contextvars propagate into asyncio.create_task(), so background resolvers
spawned from a request inherit the requesting user's tokens automatically.
"""

import logging
from contextvars import ContextVar

from sqlalchemy import select

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


async def apply_tokens_for_user_id(user_id: int | None) -> None:
    """Load the user's library-group tokens into the current context.
    Missing user/group or empty tokens -> env fallback stays in effect."""
    clear_tokens()
    if not user_id:
        return
    try:
        from app.database import async_session
        from app.models import LibraryGroup, User

        async with async_session() as db:
            user = (
                await db.execute(select(User).where(User.id == user_id))
            ).scalar_one_or_none()
            if not user or not user.library_group_id:
                return
            group = (
                await db.execute(
                    select(LibraryGroup).where(LibraryGroup.id == user.library_group_id)
                )
            ).scalar_one_or_none()
            if group:
                set_tokens(group.real_debrid_api_token, group.torbox_api_token)
    except Exception as e:
        logger.warning("Failed to load debrid tokens for user %s: %s", user_id, e)
