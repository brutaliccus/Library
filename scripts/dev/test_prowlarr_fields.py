import asyncio, json
import httpx
from app.config import get_settings

settings = get_settings()

async def main():
    params = [
        ("query", "dungeon crawler carl"),
        ("apikey", settings.prowlarr_api_key),
        ("limit", "5"),
    ]
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{settings.prowlarr_url}/api/v1/search",
            params=params,
            timeout=120,
        )
        resp.raise_for_status()
        raw = resp.json()

    for item in raw[:3]:
        print(json.dumps({
            "title": item.get("title", "")[:80],
            "indexer": item.get("indexer", ""),
            "guid": (item.get("guid", "") or "")[:80],
            "magnetUrl": (item.get("magnetUrl") or "")[:80],
            "downloadUrl": (item.get("downloadUrl") or "")[:80],
            "infoHash": item.get("infoHash"),
            "indexerId": item.get("indexerId"),
        }, indent=2))
        print("---")

asyncio.run(main())
