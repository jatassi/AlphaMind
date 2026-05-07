"""API handlers — placeholder for HTTP-facing code."""
import asyncio
import httpx
from buggy_app.core.orders import *  # L5: star import


async def fetch_market_data(symbol: str) -> dict:
    """L17: httpx call without timeout."""
    async with httpx.AsyncClient() as client:
        response = await client.get(f"https://example.com/quotes/{symbol}")
    return response.json()


async def submit_async(symbol: str) -> None:
    """L16: create_task without a TaskGroup parent."""
    asyncio.create_task(fetch_market_data(symbol))
    asyncio.create_task(_send_email(symbol))


async def _send_email(symbol: str) -> None:
    """L19: async def with no await."""
    return None
