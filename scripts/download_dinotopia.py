import asyncio
import httpx

IDENT = "dinotopialandapa00gurn"
DEST_DIR = "/ebooks/Gurney, James/Dinotopia  a land apart from time"

async def main():
    async with httpx.AsyncClient(follow_redirects=True, timeout=600) as client:
        meta = await client.get(f"https://archive.org/metadata/{IDENT}")
        meta.raise_for_status()
        files = meta.json().get("files") or []
        pdfs = [f["name"] for f in files if isinstance(f, dict) and str(f.get("name", "")).lower().endswith(".pdf")]
        print("pdfs:", pdfs)
        preferred = next((n for n in pdfs if n.endswith("_jpg.pdf")), None)
        name = preferred or pdfs[0]
        url = f"https://archive.org/download/{IDENT}/{name}"
        dest = f"{DEST_DIR}/{name}"
        print("downloading", url, "->", dest)
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                async for chunk in resp.aiter_bytes(1024 * 256):
                    f.write(chunk)
        print("done", dest)

asyncio.run(main())
