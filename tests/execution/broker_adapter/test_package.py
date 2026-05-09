"""Tests for the public surface of the broker_adapter package.

Story ALP-378 introduced the substrate symbols; story ALP-384 (this commit)
adds the fill-stream subscriber + translator + ``FillReport`` projection.
Sibling stories 02a-e contribute additional symbols to the same surface and
will extend this required set at merge time.
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

_FILL_STREAM_SYMBOLS: frozenset[str] = frozenset(
    {
        "FillReport",
        "OrderStatus",
        "subscribe_trade_updates",
        "translate_trade_update",
    }
)

_REQUIRED_SYMBOLS: frozenset[str] = _SUBSTRATE_SYMBOLS | _FILL_STREAM_SYMBOLS


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

    # Touch each binding so unused-import linting can't quietly drop one.
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


def test_fill_stream_symbols_directly_importable() -> None:
    """Each fill-stream symbol from story 02f can be imported from the root."""
    from alphamind.execution.broker_adapter import (
        FillReport,
        OrderStatus,
        subscribe_trade_updates,
        translate_trade_update,
    )

    _ = (FillReport, OrderStatus, subscribe_trade_updates, translate_trade_update)
