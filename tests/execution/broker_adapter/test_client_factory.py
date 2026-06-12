"""Tests for ``alphamind.execution.broker_adapter.client_factory``.

Story ALP-378 — paper/live ``AlpacaClientFactory`` resolving credentials from
``VenueConfig`` + environment variables and producing fresh
``TradingClient`` / ``TradingStream`` instances.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from alpaca.data.historical.corporate_actions import CorporateActionsClient
from alpaca.trading.client import TradingClient
from alpaca.trading.stream import TradingStream

from alphamind.config.models import VenueConfig


def _valid_venue_raw() -> dict[str, Any]:
    return {
        "alpaca": {
            "paper": {
                "rest_url": "https://paper-api.alpaca.markets",
                "ws_url": "wss://paper-api.alpaca.markets/stream",
                "api_key_env": "ALPACA_PAPER_KEY",
                "api_secret_env": "ALPACA_PAPER_SECRET",
            },
            "live": {
                "rest_url": "https://api.alpaca.markets",
                "ws_url": "wss://api.alpaca.markets/stream",
                "api_key_env": "ALPACA_LIVE_KEY",
                "api_secret_env": "ALPACA_LIVE_SECRET",
            },
            "rate_limit_per_minute": 200,
        },
        "session_hours": {
            "regular": {"open": "09:30", "close": "16:00"},
            "pre_market": {"open": "04:00", "close": "09:30"},
            "after_hours": {"open": "16:00", "close": "20:00"},
        },
    }


@pytest.fixture
def venue() -> VenueConfig:
    return VenueConfig.model_validate(_valid_venue_raw())


def test_paper_factory_reads_paper_env_vars(
    monkeypatch: pytest.MonkeyPatch, venue: VenueConfig
) -> None:
    from alphamind.execution.broker_adapter import AlpacaClientFactory

    monkeypatch.setenv("ALPACA_PAPER_KEY", "paper-key-value")
    monkeypatch.setenv("ALPACA_PAPER_SECRET", "paper-secret-value")
    # Ensure live vars don't bleed in.
    monkeypatch.delenv("ALPACA_LIVE_KEY", raising=False)
    monkeypatch.delenv("ALPACA_LIVE_SECRET", raising=False)

    factory = AlpacaClientFactory(venue, mode="paper")

    assert factory.credentials.mode == "paper"
    assert factory.credentials.api_key == "paper-key-value"
    assert factory.credentials.api_secret == "paper-secret-value"
    assert factory.credentials.rest_url == "https://paper-api.alpaca.markets"
    assert factory.credentials.ws_url == "wss://paper-api.alpaca.markets/stream"


def test_live_factory_reads_live_env_vars(
    monkeypatch: pytest.MonkeyPatch, venue: VenueConfig
) -> None:
    from alphamind.execution.broker_adapter import AlpacaClientFactory

    monkeypatch.setenv("ALPACA_LIVE_KEY", "live-key-value")
    monkeypatch.setenv("ALPACA_LIVE_SECRET", "live-secret-value")
    monkeypatch.delenv("ALPACA_PAPER_KEY", raising=False)
    monkeypatch.delenv("ALPACA_PAPER_SECRET", raising=False)

    factory = AlpacaClientFactory(venue, mode="live")

    assert factory.credentials.mode == "live"
    assert factory.credentials.api_key == "live-key-value"
    assert factory.credentials.api_secret == "live-secret-value"
    assert factory.credentials.rest_url == "https://api.alpaca.markets"


def test_missing_env_vars_raise_runtime_error_naming_them(
    monkeypatch: pytest.MonkeyPatch, venue: VenueConfig
) -> None:
    from alphamind.execution.broker_adapter import AlpacaClientFactory

    monkeypatch.delenv("ALPACA_PAPER_KEY", raising=False)
    monkeypatch.delenv("ALPACA_PAPER_SECRET", raising=False)

    with pytest.raises(RuntimeError) as excinfo:
        AlpacaClientFactory(venue, mode="paper")

    msg = str(excinfo.value)
    assert "ALPACA_PAPER_KEY" in msg
    assert "ALPACA_PAPER_SECRET" in msg


def test_empty_env_var_treated_as_missing(
    monkeypatch: pytest.MonkeyPatch, venue: VenueConfig
) -> None:
    from alphamind.execution.broker_adapter import AlpacaClientFactory

    monkeypatch.setenv("ALPACA_PAPER_KEY", "")
    monkeypatch.setenv("ALPACA_PAPER_SECRET", "secret-value")

    with pytest.raises(RuntimeError) as excinfo:
        AlpacaClientFactory(venue, mode="paper")

    msg = str(excinfo.value)
    assert "ALPACA_PAPER_KEY" in msg
    # ALPACA_PAPER_SECRET was set, so it should not be listed.
    assert "ALPACA_PAPER_SECRET" not in msg


def test_partial_missing_env_var_only_lists_missing_one(
    monkeypatch: pytest.MonkeyPatch, venue: VenueConfig
) -> None:
    from alphamind.execution.broker_adapter import AlpacaClientFactory

    monkeypatch.setenv("ALPACA_PAPER_KEY", "real-key")
    monkeypatch.delenv("ALPACA_PAPER_SECRET", raising=False)

    with pytest.raises(RuntimeError) as excinfo:
        AlpacaClientFactory(venue, mode="paper")

    msg = str(excinfo.value)
    assert "ALPACA_PAPER_SECRET" in msg
    assert "ALPACA_PAPER_KEY" not in msg


def test_build_trading_client_returns_paper_client(
    monkeypatch: pytest.MonkeyPatch, venue: VenueConfig
) -> None:
    from alphamind.execution.broker_adapter import AlpacaClientFactory

    monkeypatch.setenv("ALPACA_PAPER_KEY", "paper-key")
    monkeypatch.setenv("ALPACA_PAPER_SECRET", "paper-secret")

    factory = AlpacaClientFactory(venue, mode="paper")
    client = factory.build_trading_client()

    assert isinstance(client, TradingClient)
    # alpaca-py stores the resolved API key on the underlying REST instance.
    # Inspect via the documented `_api_key` attribute.
    assert cast(Any, client)._api_key == "paper-key"
    # The paper flag drives the URL — assert the resolved base URL is the paper
    # endpoint. (alpaca-py exposes the URL via the inherited RESTClient base.)
    base_url = getattr(client, "_base_url", None)
    if base_url is not None:
        assert "paper" in str(base_url)


def test_build_trading_client_returns_live_client_when_mode_live(
    monkeypatch: pytest.MonkeyPatch, venue: VenueConfig
) -> None:
    from alphamind.execution.broker_adapter import AlpacaClientFactory

    monkeypatch.setenv("ALPACA_LIVE_KEY", "live-key")
    monkeypatch.setenv("ALPACA_LIVE_SECRET", "live-secret")

    factory = AlpacaClientFactory(venue, mode="live")
    client = factory.build_trading_client()

    assert isinstance(client, TradingClient)
    assert cast(Any, client)._api_key == "live-key"
    base_url = getattr(client, "_base_url", None)
    if base_url is not None:
        assert "paper" not in str(base_url)


def test_build_trading_stream_returns_bounded_facade_over_vendor_stream(
    monkeypatch: pytest.MonkeyPatch, venue: VenueConfig
) -> None:
    """The factory hands out the async facade, never a bare vendor stream (ALP-946)."""
    from alphamind.execution.broker_adapter import AlpacaClientFactory
    from alphamind.execution.broker_adapter.bounded_streams import BoundedTradingStream

    monkeypatch.setenv("ALPACA_PAPER_KEY", "paper-key")
    monkeypatch.setenv("ALPACA_PAPER_SECRET", "paper-secret")

    factory = AlpacaClientFactory(venue, mode="paper")
    stream = factory.build_trading_stream()

    assert isinstance(stream, BoundedTradingStream)
    assert isinstance(stream._stream, TradingStream)


def test_build_corporate_actions_client_returns_paper_client(
    monkeypatch: pytest.MonkeyPatch, venue: VenueConfig
) -> None:
    """``build_corporate_actions_client`` mints a v1beta1 client bound to paper creds."""
    from alphamind.execution.broker_adapter import AlpacaClientFactory

    monkeypatch.setenv("ALPACA_PAPER_KEY", "paper-key")
    monkeypatch.setenv("ALPACA_PAPER_SECRET", "paper-secret")

    factory = AlpacaClientFactory(venue, mode="paper")
    client = factory.build_corporate_actions_client()

    assert isinstance(client, CorporateActionsClient)
    assert cast(Any, client)._api_key == "paper-key"


def test_build_corporate_actions_client_returns_live_client_when_mode_live(
    monkeypatch: pytest.MonkeyPatch, venue: VenueConfig
) -> None:
    """``build_corporate_actions_client`` honors live-mode credentials."""
    from alphamind.execution.broker_adapter import AlpacaClientFactory

    monkeypatch.setenv("ALPACA_LIVE_KEY", "live-key")
    monkeypatch.setenv("ALPACA_LIVE_SECRET", "live-secret")

    factory = AlpacaClientFactory(venue, mode="live")
    client = factory.build_corporate_actions_client()

    assert isinstance(client, CorporateActionsClient)
    assert cast(Any, client)._api_key == "live-key"


def test_install_socket_timeout_defaults_a_timeout_on_session_request() -> None:
    """The wrapper forwards a default ``timeout`` when the caller omits one.

    alpaca-py issues blocking ``requests`` calls with no ``timeout``; the factory
    wraps the session's ``request`` so a stalled socket cannot hang the worker
    thread forever (the thread-side floor under the query layer's event-loop
    ``wait_for`` bound — ALP-841 / ALP-850).
    """
    import requests

    from alphamind.execution.broker_adapter.client_factory import (
        _SOCKET_TIMEOUT_SECONDS,
        _install_socket_timeout,
    )

    captured: dict[str, Any] = {}

    class _RecordingSession(requests.Session):
        def request(self, *args: Any, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return "ok"

    client = cast(Any, type("FakeRest", (), {})())
    client._session = _RecordingSession()

    _install_socket_timeout(client)

    # Caller omits timeout → wrapper supplies the default.
    assert client._session.request("GET", "https://example/x") == "ok"
    assert captured["timeout"] == _SOCKET_TIMEOUT_SECONDS

    # Caller-supplied timeout is preserved (setdefault, not override).
    captured.clear()
    client._session.request("GET", "https://example/x", timeout=1.5)
    assert captured["timeout"] == 1.5


def test_build_trading_client_installs_socket_timeout_wrapper(
    monkeypatch: pytest.MonkeyPatch, venue: VenueConfig
) -> None:
    """``build_trading_client`` wraps the session's ``request`` (timeout floor).

    Behaviour of the wrapper itself is covered by
    ``test_install_socket_timeout_defaults_a_timeout_on_session_request``; this
    asserts the build path actually installs it (so a real client is protected).
    """
    from alphamind.execution.broker_adapter import AlpacaClientFactory

    monkeypatch.setenv("ALPACA_PAPER_KEY", "paper-key")
    monkeypatch.setenv("ALPACA_PAPER_SECRET", "paper-secret")

    factory = AlpacaClientFactory(venue, mode="paper")
    client = factory.build_trading_client()

    session = cast(Any, client)._session
    # An unwrapped requests.Session.request is the bound method on the class; the
    # factory replaces the *instance* attribute with a wrapping closure.
    assert "request" in vars(session), "factory did not install a per-instance request wrapper"


def test_factory_credentials_are_frozen(
    monkeypatch: pytest.MonkeyPatch, venue: VenueConfig
) -> None:
    from dataclasses import FrozenInstanceError

    from alphamind.execution.broker_adapter import AlpacaClientFactory

    monkeypatch.setenv("ALPACA_PAPER_KEY", "paper-key")
    monkeypatch.setenv("ALPACA_PAPER_SECRET", "paper-secret")

    factory = AlpacaClientFactory(venue, mode="paper")
    with pytest.raises(FrozenInstanceError):
        cast(Any, factory.credentials).api_key = "rotated"
