import asyncio
from app.routers.books import _apply_availability_filter
from app.services import google_books


async def main():
    r = await google_books.search_volumes("Wizard of Time Breedon", max_results=10)
    f = await _apply_availability_filter(r["books"], True)
    print(f"wizard search: google={len(r['books'])} available={len(f)}")
    for b in f[:5]:
        print(f"  - {b['title'][:60]}")

    r2 = await google_books.search_volumes("fantasy", max_results=15)
    f2 = await _apply_availability_filter(r2["books"], True)
    print(f"fantasy search: google={len(r2['books'])} available={len(f2)}")


asyncio.run(main())
