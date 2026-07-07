import asyncio
import httpx

async def main():
    url = "http://prowlarr:9696/5/download?apikey=dd297d8fa47c4ce58ffdce2009d29703&link=d0dBZ2JNMnYxZ3lFSzJzWDdueHlOMFNCcU5YRmMzZnF&file=Matt+Dinniman+-+Dungeon+Crawler+Carl+-+01+-+Dungeon+Crawler+Carl"
    async with httpx.AsyncClient(follow_redirects=False) as client:
        resp = await client.get(url, timeout=60)
        print(f"Status: {resp.status_code}")
        print(f"Headers: {dict(resp.headers)}")
        ct = resp.headers.get("content-type", "")
        print(f"Content-Type: {ct}")
        body = resp.content
        print(f"Body length: {len(body)}")
        if resp.status_code >= 400:
            print(f"Error body: {body[:500]}")
        elif ct == "application/x-bittorrent" or b"announce" in body[:200]:
            print("Got torrent file!")
            print(f"First 100 bytes: {body[:100]}")
        elif body[:7] == b"magnet:":
            print(f"Got magnet: {body[:200]}")
        elif resp.is_redirect:
            print(f"Redirect to: {resp.headers.get('location', '')[:200]}")
        else:
            print(f"Unknown body: {body[:300]}")

asyncio.run(main())
