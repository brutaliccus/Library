import asyncio
import httpx
from app.config import get_settings

async def main():
    s = get_settings()
    async with httpx.AsyncClient() as c:
        for cid in range(90, 105):
            try:
                r = await c.get(
                    f"{s.kavita_url}/api/Book/{cid}/book-page",
                    params={"page": 0},
                    headers={"x-api-key": s.kavita_api_key},
                    timeout=30,
                )
                if r.status_code == 200:
                    print(f"chapter {cid}: OK ({len(r.text)} bytes)")
                else:
                    print(f"chapter {cid}: {r.status_code}")
            except Exception as e:
                print(f"chapter {cid}: error {e}")

asyncio.run(main())
