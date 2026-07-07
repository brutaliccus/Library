import asyncio, json
from app.services.audiobookshelf import start_playback_session
from app.config import get_settings

settings = get_settings()

async def main():
    session = await start_playback_session("17f09c32-0833-4e8d-9f17-5d8056bbfe16")
    if not session:
        print("No session!")
        return
    tracks = session.get("audioTracks", [])
    print(f"Got {len(tracks)} tracks")
    for t in tracks[:3]:
        print(json.dumps({
            "index": t.get("index"),
            "contentUrl": t.get("contentUrl", "")[:200],
            "mimeType": t.get("mimeType"),
            "duration": t.get("duration"),
            "startOffset": t.get("startOffset"),
        }, indent=2))

asyncio.run(main())
