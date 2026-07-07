import asyncio
import httpx
from app.config import get_settings

async def main():
    s = get_settings()
    async with httpx.AsyncClient() as c:
        for cid in (95, 97, 98):
            info = await c.get(
                f"{s.kavita_url}/api/Book/{cid}/book-info",
                headers={"x-api-key": s.kavita_api_key},
                timeout=30,
            )
            print(f"chapter {cid} book-info:", info.status_code, info.text[:200] if info.status_code != 200 else info.json())
            for p in range(3):
                r = await c.get(
                    f"{s.kavita_url}/api/Book/{cid}/book-page",
                    params={"page": p},
                    headers={"x-api-key": s.kavita_api_key},
                    timeout=30,
                )
                print(f"  page {p}: {r.status_code} ({len(r.text)} bytes)")

asyncio.run(main())
