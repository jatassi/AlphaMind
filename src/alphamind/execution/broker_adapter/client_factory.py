"""Paper / live ``AlpacaClientFactory`` (story 01 / ALP-378).

Resolves the operator-selected execution mode (``paper`` / ``live``) into
:class:`ResolvedCredentials` by reading the env-var names referenced by the
mode's :class:`alphamind.config.models.venue.AlpacaCredentials` block, then
mints fresh ``alpaca-py`` REST / websocket clients on demand. The factory
itself is stateless beyond the resolved credentials so a single instance can
be shared across the adapter's lifetime.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from alpaca.trading.client import TradingClient
from alpaca.trading.stream import TradingStream

from alphamind.config.models.venue import AlpacaCredentials, VenueConfig

ExecutionMode = Literal["paper", "live"]


@dataclass(frozen=True)
class ResolvedCredentials:
    """Resolved API key/secret + URLs for the selected execution mode.

    Frozen so the factory can hand out the same record without callers
    accidentally rotating credentials in place.
    """

    mode: ExecutionMode
    api_key: str
    api_secret: str
    rest_url: str
    ws_url: str


class AlpacaClientFactory:
    """Builds ``TradingClient`` / ``TradingStream`` instances for ``mode``.

    Resolves API credentials from environment variables named in
    ``VenueConfig.alpaca.<mode>``; raises :class:`RuntimeError` naming any
    env var that is unset or empty. One factory per adapter lifetime.
    """

    def __init__(self, venue: VenueConfig, mode: ExecutionMode) -> None:
        creds_block: AlpacaCredentials = (
            venue.alpaca.paper if mode == "paper" else venue.alpaca.live
        )
        api_key = os.environ.get(creds_block.api_key_env, "")
        api_secret = os.environ.get(creds_block.api_secret_env, "")
        missing = [
            name
            for name, value in (
                (creds_block.api_key_env, api_key),
                (creds_block.api_secret_env, api_secret),
            )
            if not value
        ]
        if missing:
            msg = (
                f"Alpaca {mode} credentials not set: environment variable(s) "
                f"{missing} are unset or empty"
            )
            raise RuntimeError(msg)
        self._credentials = ResolvedCredentials(
            mode=mode,
            api_key=api_key,
            api_secret=api_secret,
            rest_url=creds_block.rest_url,
            ws_url=creds_block.ws_url,
        )

    @property
    def credentials(self) -> ResolvedCredentials:
        return self._credentials

    def build_trading_client(self) -> TradingClient:
        """Return a fresh ``TradingClient`` bound to the resolved credentials."""
        return TradingClient(
            api_key=self._credentials.api_key,
            secret_key=self._credentials.api_secret,
            paper=(self._credentials.mode == "paper"),
            url_override=self._credentials.rest_url,
        )

    def build_trading_stream(self) -> TradingStream:
        """Return a fresh ``TradingStream`` bound to the resolved credentials."""
        return TradingStream(
            api_key=self._credentials.api_key,
            secret_key=self._credentials.api_secret,
            paper=(self._credentials.mode == "paper"),
            url_override=self._credentials.ws_url,
        )
