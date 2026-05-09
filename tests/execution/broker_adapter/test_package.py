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

_EQUITY_SYMBOLS: frozenset[str] = frozenset(
    {
        "EquitySubmission",
        "submit_equity_add",
        "submit_equity_close",
        "submit_equity_open",
    }
)

_OPTIONS_SYMBOLS: frozenset[str] = frozenset(
    {
        "OptionsSubmission",
        "build_occ_symbol",
        "submit_options_add",
        "submit_options_close",
        "submit_options_open",
    }
)

_MLEG_SYMBOLS: frozenset[str] = frozenset(
    {
        "MLEGLegAck",
        "MLEGSubmission",
        "submit_mleg_add",
        "submit_mleg_close",
        "submit_mleg_open",
    }
)

_MODIFY_SYMBOLS: frozenset[str] = frozenset(
    {
        "CancellationAck",
        "ReplaceFields",
        "ReplacementAck",
        "submit_cancel",
        "submit_replace",
    }
)

_RECOVERY_SYMBOLS: frozenset[str] = frozenset(
    {
        "order_snapshot_to_fill_reports",
        "recover_missed_fills_since",
    }
)

_REQUIRED_SYMBOLS: frozenset[str] = (
    _SUBSTRATE_SYMBOLS
    | _QUERIES_SYMBOLS
    | _FILL_STREAM_SYMBOLS
    | _EQUITY_SYMBOLS
    | _OPTIONS_SYMBOLS
    | _MLEG_SYMBOLS
    | _MODIFY_SYMBOLS
    | _RECOVERY_SYMBOLS
)


def test_public_surface_includes_all_required_symbols() -> None:
    import alphamind.execution.broker_adapter as adapter

    actual = set(adapter.__all__)
    missing = _REQUIRED_SYMBOLS - actual
    assert not missing, f"missing required symbols in __all__: {sorted(missing)}"

    for name in _REQUIRED_SYMBOLS:
        assert hasattr(adapter, name), f"missing public symbol: {name}"


def test_substrate_symbols_directly_importable() -> None:
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
    from alphamind.execution.broker_adapter import (
        FillReport,
        OrderStatus,
        subscribe_trade_updates,
        translate_trade_update,
    )

    _ = (FillReport, OrderStatus, subscribe_trade_updates, translate_trade_update)


def test_equity_symbols_directly_importable() -> None:
    from alphamind.execution.broker_adapter import (
        EquitySubmission,
        submit_equity_add,
        submit_equity_close,
        submit_equity_open,
    )

    _ = (EquitySubmission, submit_equity_add, submit_equity_close, submit_equity_open)


def test_options_symbols_directly_importable() -> None:
    from alphamind.execution.broker_adapter import (
        OptionsSubmission,
        build_occ_symbol,
        submit_options_add,
        submit_options_close,
        submit_options_open,
    )

    _ = (
        OptionsSubmission,
        build_occ_symbol,
        submit_options_add,
        submit_options_close,
        submit_options_open,
    )


def test_mleg_symbols_directly_importable() -> None:
    from alphamind.execution.broker_adapter import (
        MLEGLegAck,
        MLEGSubmission,
        submit_mleg_add,
        submit_mleg_close,
        submit_mleg_open,
    )

    _ = (MLEGLegAck, MLEGSubmission, submit_mleg_add, submit_mleg_close, submit_mleg_open)


def test_modify_symbols_directly_importable() -> None:
    from alphamind.execution.broker_adapter import (
        CancellationAck,
        ReplaceFields,
        ReplacementAck,
        submit_cancel,
        submit_replace,
    )

    _ = (CancellationAck, ReplaceFields, ReplacementAck, submit_cancel, submit_replace)


def test_recovery_symbols_directly_importable() -> None:
    from alphamind.execution.broker_adapter import (
        order_snapshot_to_fill_reports,
        recover_missed_fills_since,
    )

    _ = (order_snapshot_to_fill_reports, recover_missed_fills_since)
