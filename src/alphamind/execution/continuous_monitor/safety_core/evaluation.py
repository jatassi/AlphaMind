"""Pure safety-core evaluation (ALP-857 / ADR-0004).

The functional core: ``evaluate_safety`` is a pure function over the broker
snapshot (positions + account equity) and the live price reads. It detects the
**no-broker-floor safety items** — portfolio-level gross-exposure and
single-name concentration breaches computable from broker market values, and
underlying price-staleness — and returns a typed :class:`SafetyEvaluation`.

No I/O, no DB, no broker call, no clock read: every input is a plain value the
imperative shell (``loop.py``) loads and passes in. Drawdown/regime/halt
classification is *not* here — it requires the DB Projection (cost basis,
regime rows, risk-parameter rows) the safety core must not read; it stays in
the monitor proper, which is fail-safe under the broker floor.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from alphamind.execution.continuous_monitor.safety_core.records import (
    PriceStaleness,
    SafetyEvaluation,
    SafetyPosition,
)
from alphamind.execution.continuous_monitor.underlying_stream.cache import (
    FreshPrice,
    PriceRead,
    StalePrice,
)

# Stable rule IDs the safety core reports breaches against. Named here (not in
# guardrails.yaml) because the safety core evaluates only these two
# broker-snapshot-derivable portfolio limits; the full rule registry stays with
# the monitor proper's guardrail-library evaluation.
GROSS_EXPOSURE_RULE_ID = "gross_exposure_pct"
POSITION_CONCENTRATION_RULE_ID = "position_concentration_pct"


@dataclass(frozen=True, slots=True)
class SafetyLimits:
    """The two portfolio-level limits the safety core enforces against.

    Both are percentages of account equity. ``max_gross_exposure_pct`` bounds
    the summed absolute market value of all positions; ``max_position_concentration_pct``
    bounds any single name's absolute market value. Sourced at the shell from
    the resolved guardrails config; the core treats them as plain values.
    """

    max_gross_exposure_pct: float
    max_position_concentration_pct: float


def evaluate_safety(
    *,
    as_of: datetime,
    equity: float,
    positions: tuple[SafetyPosition, ...],
    price_reads: Mapping[str, PriceRead],
    limits: SafetyLimits,
) -> SafetyEvaluation:
    """Evaluate the broker snapshot against the no-floor safety limits.

    Pure. Returns a :class:`SafetyEvaluation` naming every breached rule and the
    price-staleness partition over the open-position tickers.

    Args:
        as_of: the tick timestamp (supplied by the shell's clock port).
        equity: account equity from the broker snapshot (the denominator for
            both percentage limits). A non-positive equity disables the
            percentage checks (no meaningful denominator) — the core reports no
            breach rather than dividing by zero.
        positions: open positions projected from the broker ``PositionSnapshot``.
        price_reads: freshness-classified live price per open-position ticker
            (from ``UnderlyingPriceCache.read_all``).
        limits: the two portfolio limits to check against.
    """
    breached: list[str] = []

    if equity > 0.0:
        gross_value = sum(abs(p.market_value) for p in positions)
        gross_pct = gross_value / equity * 100.0
        if gross_pct > limits.max_gross_exposure_pct:
            breached.append(GROSS_EXPOSURE_RULE_ID)

        max_position_pct = max(
            (abs(p.market_value) / equity * 100.0 for p in positions),
            default=0.0,
        )
        if max_position_pct > limits.max_position_concentration_pct:
            breached.append(POSITION_CONCENTRATION_RULE_ID)

    return SafetyEvaluation(
        as_of=as_of,
        breached_rules=tuple(breached),
        price_staleness=_classify_staleness(price_reads),
    )


def _classify_staleness(price_reads: Mapping[str, PriceRead]) -> PriceStaleness:
    """Partition the requested tickers into fresh / stale / missing.

    ``globally_stale`` is True iff the expected set is non-empty and *no* ticker
    read fresh — the cold-feed (writer-wedged) case (ALP-770 class).
    """
    fresh = frozenset(t for t, r in price_reads.items() if isinstance(r, FreshPrice))
    stale = frozenset(t for t, r in price_reads.items() if isinstance(r, StalePrice))
    missing = frozenset(price_reads) - fresh - stale
    globally_stale = bool(price_reads) and not fresh
    return PriceStaleness(
        fresh=fresh,
        stale=stale,
        missing=missing,
        globally_stale=globally_stale,
    )
