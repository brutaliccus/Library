import asyncio, json
import httpx
from app.config import get_settings

settings = get_settings()

async def main():
    params = [
        ("query", "dungeon crawler carl"),
        ("apikey", settings.prowlarr_api_key),
        ("limit", "50"),
    ]
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{settings.prowlarr_url}/api/v1/search",
            params=params,
            timeout=120,
        )
        resp.raise_for_status()
        raw = resp.json()

    for item in raw:
        indexer = item.get("indexer", "")
        if "abtorrent" in indexer.lower() or "ab " in indexer.lower():
            print(json.dumps({
                "title": item.get("title", "")[:100],
                "indexer": indexer,
                "guid": (item.get("guid", "") or "")[:120],
                "magnetUrl": (item.get("magnetUrl") or "")[:120],
                "downloadUrl": (item.get("downloadUrl") or "")[:120],
                "infoHash": item.get("infoHash"),
                "indexerId": item.get("indexerId"),
            }, indent=2))
            print("---")

asyncio.run(main())
