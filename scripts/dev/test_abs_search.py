import asyncio, json
from app.services.audiobookshelf import search_library_with_ids
result = asyncio.run(search_library_with_ids("Dungeon Crawler Carl"))
print(json.dumps(result, indent=2))
