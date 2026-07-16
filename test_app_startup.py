import asyncio
from app.main import ensure_indexes

async def main():
    try:
        await ensure_indexes()
        print('ensure_indexes succeeded')
    except Exception as exc:
        print('ensure_indexes failed:', type(exc).__name__, exc)

asyncio.run(main())
