"""Check push subscription data in DB."""
import asyncio
from app.database import async_session
from app.models import PushSubscription
from sqlalchemy import select

async def main():
    async with async_session() as db:
        r = await db.execute(select(PushSubscription))
        for s in r.scalars().all():
            print("id:", s.id, "user:", s.user_id, "p256dh len:", len(s.p256dh), "auth len:", len(s.auth))
            print("  p256dh sample:", repr(s.p256dh[:50]) if s.p256dh else "None")

asyncio.run(main())
