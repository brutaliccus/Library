import asyncio
import httpx
from app.config import get_settings

async def main():
    s = get_settings()
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{s.kavita_url}/api/Book/97/book-info", headers={"x-api-key": s.kavita_api_key})
        print("info", r.status_code, r.json() if r.status_code == 200 else r.text)
        from app.services.kavita import get_chapter_file_path
        path = await get_chapter_file_path(97)
        print("path", path, path.stat().st_size if path and path.exists() else None)
        r2 = await c.get("http://127.0.0.1:8080/api/library/reader/97/pdf")
        print("pdf endpoint", r2.status_code, r2.headers.get("content-type"), len(r2.content))

asyncio.run(main())
