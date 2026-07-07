import asyncio
import httpx
from app.config import get_settings

async def main():
    s = get_settings()
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f"{s.kavita_url}/api/Search/search",
            params={"queryString": "dinotopia", "includeChapterAndFiles": True},
            headers={"x-api-key": s.kavita_api_key},
            timeout=30,
        )
        print(r.status_code)
        data = r.json()
        for item in data if isinstance(data, list) else data.get("series", data.get("results", [])):
            print(item)

asyncio.run(main())
