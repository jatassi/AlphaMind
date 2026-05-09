"""Tests for the public surface of the broker_adapter package.

Story ALP-378 introduced the substrate symbols; subsequent wave-2 stories
add their own symbols. Each story extends the relevant frozenset below;
the union forms the expected public surface.
"""

from __future__ import annotations

_SUBSTRATE_SYMBOLS: frozenset[str] = frozenset(
    {
        "AlpacaClientFactory",
        "ExecutionMode",
        "GatewaySubmissionFailed",
        "PermanentRejection",
        "PermanentRejectionCode",
        "ResolvedCredentials",
        "Submitted",
        "SubmissionOutcome",
        "classify_alpaca_error",
        "is_transient",
        "submit_with_retry",
    }
)

_QUERIES_SYMBOLS: frozenset[str] = frozenset(
    {
        "AccountStateQueries",
        "ActivitySnapshot",
        "AssetSnapshot",
        "CalendarDay",
        "MarketClock",
        "OrderLegSnapshot",
        "OrderSnapshot",
        "PositionSnapshot",
        "TradeAccountSnapshot",
    }
)

_FILL_STREAM_SYMBOLS: frozenset[str] = frozenset(
    {
        "FillReport",
        "OrderStatus",
        "subscribe_trade_updates",
        "translate_trade_update",
    }
)

_REQUIRED_SYMBOLS: frozenset[str] = (
    _SUBSTRATE_SYMBOLS | _QUERIES_SYMBOLS | _FILL_STREAM_SYMBOLS
)


def test_public_surface_includes_all_required_symbols() -> None:
    import alphamind.execution.broker_adapter as adapter

    actual = set(adapter.__all__)
    missing = _REQUIRED_SYMBOLS - actual
    assert not missing, f"missing required symbols in __all__: {sorted(missing)}"

    for name in _REQUIRED_SYMBOLS:
        assert hasattr(adapter, name), f"missing public symbol: {name}"


def test_substrate_symbols_directly_importable() -> None:
    """Each substrate symbol can be imported directly from the package root."""
    from alphamind.execution.broker_adapter import (
        AlpacaClientFactory,
        ExecutionMode,
        GatewaySubmissionFailed,
        PermanentRejection,
        PermanentRejectionCode,
        ResolvedCredentials,
        SubmissionOutcome,
        Submitted,
        classify_alpaca_error,
        is_transient,
        submit_with_retry,
    )

    _ = (
        AlpacaClientFactory,
        ExecutionMode,
        GatewaySubmissionFailed,
        PermanentRejection,
        PermanentRejectionCode,
        ResolvedCredentials,
        SubmissionOutcome,
        Submitted,
        classify_alpaca_error,
        is_transient,
        submit_with_retry,
    )


def test_queries_symbols_directly_importable() -> None:
    """Each ALP-379 account-state-query symbol importable from package root."""
    from alphamind.execution.broker_adapter import (
        AccountStateQueries,
        ActivitySnapshot,
        AssetSnapshot,
        CalendarDay,
        MarketClock,
        OrderLegSnapshot,
        OrderSnapshot,
        PositionSnapshot,
        TradeAccountSnapshot,
    )

    _ = (
        AccountStateQueries,
        ActivitySnapshot,
        AssetSnapshot,
        CalendarDay,
        MarketClock,
        OrderLegSnapshot,
        OrderSnapshot,
        PositionSnapshot,
        TradeAccountSnapshot,
    )


def test_fill_stream_symbols_directly_importable() -> None:
    """Each fill-stream symbol from story 02f can be imported from the root."""
    from alphamind.execution.broker_adapter import (
        FillReport,
        OrderStatus,
        subscribe_trade_updates,
        translate_trade_update,
    )

    _ = (FillReport, OrderStatus, subscribe_trade_updates, translate_trade_update)
