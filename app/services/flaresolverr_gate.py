"""Global FlareSolverr concurrency gate for the Pi.

ABB scrapes are already serial, but Anna's Archive (and other callers) can open
parallel Chromium tabs. Multiple headless Chromiums on a 4-core Pi routinely
push load above 10 and thrash swap. Cap all Flare traffic to one in-flight job.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

# One Chromium session at a time across the whole process.
_GATE = asyncio.Semaphore(1)


@asynccontextmanager
async def flaresolverr_slot() -> AsyncIterator[None]:
    async with _GATE:
        yield
