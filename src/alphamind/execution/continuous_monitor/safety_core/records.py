"""Typed records for the isolated safety core (ALP-857 / ADR-0004).

The safety core reads positions from the **broker snapshot** (the Broker-Owned
Fact, ``get_positions`` / ``PositionSnapshot``) — never the fill-projection — and
prices from the **live underlying stream**. These frozen dataclasses are the
plain-value shapes the pure :func:`evaluate_safety` core consumes and produces;
they carry no I/O and no DB coupling, so the core stays a pure function over
values (functional-core/imperative-shell, ADR-0004).

``SafetyPosition`` is the *minimal* position shape the core needs — symbol,
broker market value, and side — projected from the broker ``PositionSnapshot``
at the shell boundary. It deliberately excludes cost basis / sector / greeks /
regime: those live only in the DB Projection the safety core must not read.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass(frozen=True, slots=True)
class SafetyPosition:
    """One open position, projected from the broker ``PositionSnapshot``.

    ``market_value`` is the broker's signed market value (positive for longs,
    negative for shorts); the core takes ``abs`` for gross-exposure and
    concentration math. ``side`` is carried for diagnostics. This is the entire
    position surface the safety core sees — a Broker-Owned Fact, not the
    Projection.
    """

    symbol: str
    market_value: float
    side: Literal["long", "short"]


@dataclass(frozen=True, slots=True)
class PriceStaleness:
    """Per-tick partition of the open-position tickers by price freshness.

    ``fresh`` / ``stale`` / ``missing`` partition the expected-ticker set (their
    union equals it). ``globally_stale`` is True when *every* expected ticker is
    stale or missing — the writer-wedged cold-feed case where the live price
    stream is dead and any stop enforcement would run against frozen prices
    (ALP-770 class). Empty expected set → ``globally_stale=False`` (no positions,
    nothing to escalate).
    """

    fresh: frozenset[str]
    stale: frozenset[str]
    missing: frozenset[str]
    globally_stale: bool


@dataclass(frozen=True, slots=True)
class SafetyEvaluation:
    """Aggregate output of one safety-core evaluation tick (pure value).

    ``breached_rules`` names every portfolio-level safety limit the broker
    snapshot breached this tick (gross-exposure, single-name concentration) —
    the detections with no broker floor. ``price_staleness`` is the freshness
    partition. The safety core logs these loudly and beats its heartbeat; it
    writes nothing to the shared DB and submits nothing to the broker (the
    broker floor protects positions, ADR-0004).
    """

    as_of: datetime
    breached_rules: tuple[str, ...]
    price_staleness: PriceStaleness
