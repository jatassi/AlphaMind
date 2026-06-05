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


def _is_transient(exc: Exception) -> bool:
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
    calls: list[Exception] = []

    def fake_is_transient(exc: Exception) -> bool:
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
# BaseException propagation — CancelledError / KeyboardInterrupt / SystemExit
# must NOT enter the transient-classifier path; they are external interruptions
# (event-loop teardown, operator interrupt) that the retry helper must surface
# unchanged regardless of what the classifier would say about an Exception of
# the same status-code shape.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancelled_error_propagates_without_retry() -> None:
    """``asyncio.CancelledError`` propagates through ``submit_with_retry``.

    Regression: a permissive ``except BaseException`` clause routed
    ``CancelledError`` through ``is_transient`` (which returned ``True`` because
    the exception lacks a status_code), turning event-loop cancellation into a
    silent retry storm.
    """
    import asyncio

    classifier_calls: list[BaseException] = []

    def _accepting_classifier(exc: Exception) -> bool:
        classifier_calls.append(exc)
        return True

    async def submit() -> str:
        raise asyncio.CancelledError("operator cancelled")

    with pytest.raises(asyncio.CancelledError):
        await submit_with_retry(
            submit,
            window_seconds=5,
            transient_classifier=_accepting_classifier,
        )

    assert classifier_calls == [], (
        "CancelledError should propagate without consulting the transient "
        f"classifier; got {len(classifier_calls)} classifier call(s)."
    )


@pytest.mark.asyncio
async def test_keyboard_interrupt_propagates_without_retry() -> None:
    """``KeyboardInterrupt`` propagates through ``submit_with_retry``.

    Same regression as ``CancelledError``; operator Ctrl-C must abort, not
    retry.
    """
    classifier_calls: list[BaseException] = []

    def _accepting_classifier(exc: Exception) -> bool:
        classifier_calls.append(exc)
        return True

    async def submit() -> str:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        await submit_with_retry(
            submit,
            window_seconds=5,
            transient_classifier=_accepting_classifier,
        )

    assert classifier_calls == [], (
        "KeyboardInterrupt should propagate without consulting the transient "
        f"classifier; got {len(classifier_calls)} classifier call(s)."
    )


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


# ---------------------------------------------------------------------------
# bounded_broker_call — time-bounded write offload (ALP-841 class, write side)
# ---------------------------------------------------------------------------


class TestBoundedBrokerCall:
    """A synchronous Alpaca write call (``submit_order`` / ``replace`` / ``cancel``)
    runs on a worker thread bounded by a wall-clock ``wait_for``, so a hung socket
    can neither freeze the calling event loop nor park the caller (e.g. the
    monitor's fire-close) for the full client-factory socket timeout.
    """

    def test_hung_call_does_not_block_event_loop(self) -> None:
        """While the call is parked, a concurrent coroutine still makes progress."""
        import asyncio
        import threading

        from alphamind.execution.broker_adapter.retry import bounded_broker_call

        release = threading.Event()

        def hang() -> str:
            release.wait(timeout=5.0)
            return "ack"

        async def scenario() -> int:
            ticks = 0

            async def call() -> str:
                return await bounded_broker_call(hang)

            async def heartbeat() -> None:
                nonlocal ticks
                for _ in range(5):
                    await asyncio.sleep(0)
                    ticks += 1

            call_task = asyncio.create_task(call())
            await heartbeat()
            release.set()
            assert await call_task == "ack"
            return ticks

        ticks = asyncio.run(scenario())
        assert ticks == 5, "concurrent coroutine starved → submit ran inline on the loop"

    def test_hung_call_times_out_and_is_classified_transient(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A call exceeding the budget raises ``TimeoutError`` — which ``is_transient``
        classifies as retriable, so ``submit_with_retry`` retries it (not a permanent
        rejection)."""
        import asyncio
        import threading

        import alphamind.execution.broker_adapter.retry as retry_mod
        from alphamind.execution.broker_adapter.errors import is_transient
        from alphamind.execution.broker_adapter.retry import bounded_broker_call

        never_released = threading.Event()

        def hang() -> str:
            never_released.wait(timeout=5.0)
            return "ack"

        monkeypatch.setattr(retry_mod, "_SUBMIT_TIMEOUT_SECONDS", 0.05)

        async def run() -> None:
            await bounded_broker_call(hang)

        try:
            with pytest.raises(TimeoutError) as excinfo:
                asyncio.run(run())
        finally:
            never_released.set()

        # The timeout routes down the transient path so the submit retry loop
        # re-attempts rather than surfacing a permanent rejection.
        assert is_transient(excinfo.value)
