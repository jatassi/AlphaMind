"""Tests for ``alphamind.execution.broker_adapter.retry``.

Story ALP-378 — bounded exponential-backoff helper consuming
``ExecutionConfig.submission_retry_window_seconds`` and emitting typed
``Submitted`` / ``GatewaySubmissionFailed`` outcomes.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any, cast

import pytest

from alphamind.execution.broker_adapter import (
    GatewaySubmissionFailed,
    Submitted,
    submit_with_retry,
)


class _TransientError(Exception):
    """Synthetic transient — classified as retriable by the test classifier."""


class _PermanentError(Exception):
    """Synthetic permanent — classified as non-retriable."""


def _is_transient(exc: BaseException) -> bool:
    return isinstance(exc, _TransientError)


# ---------------------------------------------------------------------------
# Success paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_try_success_returns_submitted_with_attempt_count_one() -> None:
    calls: list[int] = []

    async def submit() -> str:
        calls.append(1)
        return "alpaca-order-id"

    outcome = await submit_with_retry(
        submit,
        window_seconds=5,
        transient_classifier=_is_transient,
    )

    assert isinstance(outcome, Submitted)
    assert outcome.payload == "alpaca-order-id"
    assert outcome.attempt_count == 1
    assert calls == [1]


@pytest.mark.asyncio
async def test_retries_on_transient_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """After two transient failures, third attempt returns the payload."""
    # Disable real sleeping so the test runs in microseconds.
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("alphamind.execution.broker_adapter.retry.asyncio.sleep", fake_sleep)

    attempts = [0]

    async def submit() -> str:
        attempts[0] += 1
        if attempts[0] < 3:
            raise _TransientError(f"transient #{attempts[0]}")
        return "alpaca-order-id"

    outcome = await submit_with_retry(
        submit,
        window_seconds=5,
        transient_classifier=_is_transient,
    )

    assert isinstance(outcome, Submitted)
    assert outcome.payload == "alpaca-order-id"
    assert outcome.attempt_count == 3
    # Two failures means two backoff sleeps before the successful attempt.
    assert len(sleeps) == 2
    # Backoff schedule per the helper: 0.5s, 1s on attempts 1 and 2.
    assert sleeps[0] == pytest.approx(0.5)
    assert sleeps[1] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_window_exhaustion_returns_gateway_submission_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persistent transient errors past the window return GatewaySubmissionFailed."""
    # Drive a synthetic clock so the deadline triggers deterministically.
    fake_now = [1000.0]

    def fake_monotonic() -> float:
        return fake_now[0]

    async def fake_sleep(seconds: float) -> None:
        fake_now[0] += seconds

    monkeypatch.setattr(
        "alphamind.execution.broker_adapter.retry.time.monotonic",
        fake_monotonic,
    )
    monkeypatch.setattr(
        "alphamind.execution.broker_adapter.retry.asyncio.sleep",
        fake_sleep,
    )

    attempts = [0]

    async def submit() -> str:
        attempts[0] += 1
        # Each failed attempt advances the clock a bit (simulating round-trip).
        fake_now[0] += 0.1
        raise _TransientError("persistently transient")

    outcome = await submit_with_retry(
        submit,
        window_seconds=2,
        transient_classifier=_is_transient,
    )

    assert isinstance(outcome, GatewaySubmissionFailed)
    assert outcome.attempt_count >= 2
    assert outcome.last_error_class == "_TransientError"
    assert "2" in outcome.reason  # references the window seconds


@pytest.mark.asyncio
async def test_permanent_rejection_reraises() -> None:
    """A permanent error is re-raised — caller translates to OMS rejection."""

    async def submit() -> str:
        raise _PermanentError("alpaca rejected: insufficient buying power")

    with pytest.raises(_PermanentError, match="insufficient buying power"):
        await submit_with_retry(
            submit,
            window_seconds=5,
            transient_classifier=_is_transient,
        )


@pytest.mark.asyncio
async def test_default_classifier_is_is_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    """When no classifier is passed, the helper falls back to ``is_transient``."""
    calls: list[BaseException] = []

    def fake_is_transient(exc: BaseException) -> bool:
        calls.append(exc)
        return True

    monkeypatch.setattr(
        "alphamind.execution.broker_adapter.retry.is_transient",
        fake_is_transient,
    )

    # Disable sleep to avoid burning seconds.
    async def fake_sleep(seconds: float) -> None:
        pass

    monkeypatch.setattr("alphamind.execution.broker_adapter.retry.asyncio.sleep", fake_sleep)

    attempts = [0]

    async def submit() -> str:
        attempts[0] += 1
        if attempts[0] < 2:
            raise RuntimeError("first transient")
        return "ok"

    outcome = await submit_with_retry(submit, window_seconds=5)

    assert isinstance(outcome, Submitted)
    assert outcome.payload == "ok"
    assert len(calls) == 1
    assert isinstance(calls[0], RuntimeError)


# ---------------------------------------------------------------------------
# Type invariants
# ---------------------------------------------------------------------------


def test_submitted_is_frozen_dataclass() -> None:
    submitted = Submitted(payload="x", attempt_count=1)
    with pytest.raises(FrozenInstanceError):
        cast(Any, submitted).payload = "y"


def test_gateway_submission_failed_is_frozen_dataclass() -> None:
    failed = GatewaySubmissionFailed(reason="r", attempt_count=3, last_error_class="ConnectError")
    with pytest.raises(FrozenInstanceError):
        cast(Any, failed).attempt_count = 99
