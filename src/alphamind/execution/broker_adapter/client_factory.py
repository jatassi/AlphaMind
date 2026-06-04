"""Paper / live ``AlpacaClientFactory`` (story 01 / ALP-378).

Resolves the operator-selected execution mode (``paper`` / ``live``) into
:class:`ResolvedCredentials` by reading the env-var names referenced by the
mode's :class:`alphamind.config.models.venue.AlpacaCredentials` block, then
mints fresh ``alpaca-py`` REST / websocket clients on demand. The factory
itself is stateless beyond the resolved credentials so a single instance can
be shared across the adapter's lifetime.
"""

from __future__ import annotations

import functools
import os
from dataclasses import dataclass
from typing import Any, Literal

from alpaca.common.rest import RESTClient
from alpaca.data.historical.corporate_actions import CorporateActionsClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.trading.client import TradingClient
from alpaca.trading.stream import TradingStream

from alphamind.config.models.venue import AlpacaCredentials, VenueConfig

ExecutionMode = Literal["paper", "live"]

# Socket-level timeout (seconds) installed on every minted REST client. alpaca-py
# issues blocking ``requests`` calls without a timeout, so a hung connection would
# block its worker thread forever. The query layer already bounds the *event
# loop* with ``asyncio.wait_for`` (the ALP-841 freeze fix); this timeout is the
# matching floor on the thread side so an orphaned worker eventually unwinds
# rather than leaking. Comfortably above ``queries._REST_TIMEOUT_SECONDS`` so the
# loop-level bound is the one that normally fires.
_SOCKET_TIMEOUT_SECONDS = 60.0


def _install_socket_timeout(client: RESTClient) -> None:
    """Default a connect/read timeout onto a REST client's ``requests`` session.

    alpaca-py's ``RESTClient._one_request`` calls ``self._session.request(...)``
    with no ``timeout``, so a stalled socket hangs the call indefinitely. We wrap
    the session's ``request`` to inject :data:`_SOCKET_TIMEOUT_SECONDS` whenever
    the caller did not pass one, bounding the blocking call at the transport
    layer. The query layer's ``asyncio.wait_for`` is the primary loop-liveness
    guarantee; this is the matching thread-side floor.
    """
    session: Any = client._session
    original_request = session.request

    @functools.wraps(original_request)
    def request_with_timeout(*args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", _SOCKET_TIMEOUT_SECONDS)
        return original_request(*args, **kwargs)

    session.request = request_with_timeout


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
        """Return a fresh ``TradingClient`` bound to the resolved credentials.

        Carries a socket-level timeout so a stalled connection cannot block its
        worker thread indefinitely (the thread-side floor under the query
        layer's event-loop ``wait_for`` bound — ALP-841).
        """
        client = TradingClient(
            api_key=self._credentials.api_key,
            secret_key=self._credentials.api_secret,
            paper=(self._credentials.mode == "paper"),
            url_override=self._credentials.rest_url,
        )
        _install_socket_timeout(client)
        return client

    def build_trading_stream(self) -> TradingStream:
        """Return a fresh ``TradingStream`` bound to the resolved credentials."""
        return TradingStream(
            api_key=self._credentials.api_key,
            secret_key=self._credentials.api_secret,
            paper=(self._credentials.mode == "paper"),
            url_override=self._credentials.ws_url,
        )

    def build_stock_data_client(self) -> StockHistoricalDataClient:
        """Return a fresh ``StockHistoricalDataClient`` for latest-quote snapshots.

        Used by the marketable-entry rewrite (ALP-738) to price a limit through
        the touch. The Stock Market Data API shares the Trading API key / secret
        (no separate market-data credential) and targets the documented Data API
        base URL, so we pass no ``url_override``.
        """
        client = StockHistoricalDataClient(
            api_key=self._credentials.api_key,
            secret_key=self._credentials.api_secret,
            raw_data=False,
        )
        _install_socket_timeout(client)
        return client

    def build_corporate_actions_client(self) -> CorporateActionsClient:
        """Return a fresh ``CorporateActionsClient`` bound to the resolved credentials.

        The v1beta1 Corporate Actions Market Data API uses the same API key /
        secret as the Trading API (no separate market-data credential), and
        targets the documented Data API base URL so we pass no
        ``url_override``.
        """
        client = CorporateActionsClient(
            api_key=self._credentials.api_key,
            secret_key=self._credentials.api_secret,
            raw_data=False,
        )
        _install_socket_timeout(client)
        return client
