"""Tests for the public surface of the broker_adapter package (story ALP-378)."""

from __future__ import annotations


def test_public_surface_re_exports_all_substrate_symbols() -> None:
    import alphamind.execution.broker_adapter as adapter

    expected = {
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
    actual = set(adapter.__all__)
    assert expected == actual

    for name in expected:
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
