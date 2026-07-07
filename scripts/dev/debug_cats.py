import httpx
import json

resp = httpx.get(
    "http://prowlarr:9696/api/v1/search",
    params={"query": "harry potter", "apikey": "dd297d8fa47c4ce58ffdce2009d29703", "limit": "30"},
    timeout=120,
)
for item in resp.json():
    cats = item.get("categories", [])
    cat_info = [(c.get("id"), c.get("name")) for c in cats]
    size_mb = item.get("size", 0) / 1024 / 1024
    title = item.get("title", "")[:90]
    print(f"{size_mb:>8.0f} MB | {cat_info} | {title}")
