import asyncio
import httpx
from app.config import get_settings
from app.services import kavita

async def main():
    s = get_settings()
    cid = 99
    async with httpx.AsyncClient() as c:
        info = await c.get(
            f"{s.kavita_url}/api/Book/{cid}/book-info",
            headers={"x-api-key": s.kavita_api_key},
            timeout=30,
        )
        print("book-info", info.status_code, info.text[:500])
        path = await kavita.get_chapter_file_path(cid)
        print("file", path, path.suffix if path else None, path.stat().st_size if path and path.exists() else None)
        pdf = await c.get(f"http://127.0.0.1:8080/api/library/reader/{cid}/pdf")
        print("pdf", pdf.status_code, pdf.headers.get("content-type"), len(pdf.content) if pdf.status_code == 200 else pdf.text[:200])
        page = await c.get(
            f"http://127.0.0.1:8080/api/library/reader/{cid}/book-page",
            params={"page": 0},
        )
        print("book-page", page.status_code, page.text[:200])

asyncio.run(main())
