"""L21: mocking a third-party library directly."""
from unittest import mock
import asyncio
from buggy_app.api.handlers import fetch_market_data


def test_fetch():
    with mock.patch("httpx.AsyncClient.get") as patched:  # L21
        patched.return_value.json.return_value = {"price": 100.0}
        asyncio.run(fetch_market_data("AAPL"))
