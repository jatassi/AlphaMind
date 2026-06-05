"""Tests for the safety core's pure evaluation (ALP-857).

``evaluate_safety`` is the functional core: a pure function over the broker
snapshot (positions) + the live price reads that returns a typed
``SafetyEvaluation`` naming every detected breach + the price-staleness
partition. No I/O, no DB, no broker — every input is a plain value, so these
tests need no fakes for the four boundaries.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from alphamind.execution.continuous_monitor.safety_core.evaluation import (
    SafetyLimits,
    evaluate_safety,
)
from alphamind.execution.continuous_monitor.safety_core.records import SafetyPosition
from alphamind.execution.continuous_monitor.underlying_stream.cache import (
    FreshPrice,
    MissingPrice,
    StalePrice,
)

_AS_OF = datetime(2026, 6, 5, 14, 30, tzinfo=UTC)


def _limits(
    *,
    max_gross_exposure_pct: float = 200.0,
    max_position_concentration_pct: float = 25.0,
) -> SafetyLimits:
    return SafetyLimits(
        max_gross_exposure_pct=max_gross_exposure_pct,
        max_position_concentration_pct=max_position_concentration_pct,
    )


def _position(
    *,
    symbol: str = "AAPL",
    market_value: float = 1000.0,
    side: Literal["long", "short"] = "long",
) -> SafetyPosition:
    return SafetyPosition(symbol=symbol, market_value=market_value, side=side)


def test_within_limits_reports_no_breach() -> None:
    """Positions comfortably under every limit → no breach flagged."""
    evaluation = evaluate_safety(
        as_of=_AS_OF,
        equity=10_000.0,
        positions=(_position(market_value=1000.0),),
        price_reads={"AAPL": FreshPrice(price=150.0, as_of=_AS_OF)},
        limits=_limits(),
    )

    assert evaluation.breached_rules == ()
    assert not evaluation.price_staleness.globally_stale
    assert evaluation.price_staleness.fresh == frozenset({"AAPL"})


def test_gross_exposure_over_limit_breaches() -> None:
    """Summed absolute market value over the gross limit → gross-exposure breach.

    A short counts toward gross by its absolute value, so a long + short whose
    absolute values sum past the limit breaches even when the net is small.
    """
    evaluation = evaluate_safety(
        as_of=_AS_OF,
        equity=10_000.0,
        positions=(
            _position(symbol="AAPL", market_value=15_000.0, side="long"),
            _position(symbol="TSLA", market_value=-12_000.0, side="short"),
        ),
        price_reads={
            "AAPL": FreshPrice(price=1.0, as_of=_AS_OF),
            "TSLA": FreshPrice(price=1.0, as_of=_AS_OF),
        },
        limits=_limits(max_gross_exposure_pct=200.0, max_position_concentration_pct=200.0),
    )

    # gross = 27_000 / 10_000 = 270% > 200%
    assert "gross_exposure_pct" in evaluation.breached_rules


def test_single_name_concentration_over_limit_breaches() -> None:
    """One name's absolute market value over the concentration limit breaches."""
    evaluation = evaluate_safety(
        as_of=_AS_OF,
        equity=10_000.0,
        positions=(_position(symbol="NVDA", market_value=3_000.0),),
        price_reads={"NVDA": FreshPrice(price=1.0, as_of=_AS_OF)},
        limits=_limits(max_gross_exposure_pct=200.0, max_position_concentration_pct=25.0),
    )

    # NVDA = 3_000 / 10_000 = 30% > 25%
    assert "position_concentration_pct" in evaluation.breached_rules
    assert "gross_exposure_pct" not in evaluation.breached_rules


def test_non_positive_equity_disables_percentage_checks() -> None:
    """Zero/negative equity → no division-by-zero, no percentage breach."""
    evaluation = evaluate_safety(
        as_of=_AS_OF,
        equity=0.0,
        positions=(_position(market_value=99_999.0),),
        price_reads={"AAPL": FreshPrice(price=1.0, as_of=_AS_OF)},
        limits=_limits(max_gross_exposure_pct=1.0, max_position_concentration_pct=1.0),
    )

    assert evaluation.breached_rules == ()


def test_price_staleness_partitions_fresh_stale_missing() -> None:
    """Mixed reads partition into fresh/stale/missing; not globally stale."""
    evaluation = evaluate_safety(
        as_of=_AS_OF,
        equity=10_000.0,
        positions=(
            _position(symbol="AAPL"),
            _position(symbol="TSLA"),
            _position(symbol="NVDA"),
        ),
        price_reads={
            "AAPL": FreshPrice(price=150.0, as_of=_AS_OF),
            "TSLA": StalePrice(last_price=200.0, as_of=_AS_OF, age_seconds=999.0),
            "NVDA": MissingPrice(),
        },
        limits=_limits(),
    )

    staleness = evaluation.price_staleness
    assert staleness.fresh == frozenset({"AAPL"})
    assert staleness.stale == frozenset({"TSLA"})
    assert staleness.missing == frozenset({"NVDA"})
    assert not staleness.globally_stale


def test_globally_stale_when_no_ticker_reads_fresh() -> None:
    """Every expected ticker stale/missing → globally stale (writer wedged)."""
    evaluation = evaluate_safety(
        as_of=_AS_OF,
        equity=10_000.0,
        positions=(_position(symbol="AAPL"), _position(symbol="TSLA")),
        price_reads={
            "AAPL": StalePrice(last_price=150.0, as_of=_AS_OF, age_seconds=999.0),
            "TSLA": MissingPrice(),
        },
        limits=_limits(),
    )

    assert evaluation.price_staleness.globally_stale


def test_no_positions_is_not_globally_stale() -> None:
    """Empty expected set → not globally stale (nothing to escalate)."""
    evaluation = evaluate_safety(
        as_of=_AS_OF,
        equity=10_000.0,
        positions=(),
        price_reads={},
        limits=_limits(),
    )

    assert not evaluation.price_staleness.globally_stale
    assert evaluation.breached_rules == ()
