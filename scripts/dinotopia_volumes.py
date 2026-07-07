import asyncio
import httpx
from app.config import get_settings

async def main():
    s = get_settings()
    async with httpx.AsyncClient() as c:
        for sid in (87, 89):
            r = await c.get(
                f"{s.kavita_url}/api/Series/volumes",
                params={"seriesId": sid},
                headers={"x-api-key": s.kavita_api_key},
                timeout=30,
            )
            print(f"series {sid}:", r.status_code)
            print(r.json())

asyncio.run(main())
