"""Cumulative ``regt_excess_over_pm`` aggregates (ALP-429, story 06b).

Per ``regt-margin-attribution.md § Aggregation and delivery``: cumulative
aggregates are computed at portfolio-state delivery time by summing
``regt_excess_over_pm`` across fill-record metadata. No separate aggregate
table — recompute per snapshot invocation.

The ``RegTExcessAggregates`` value object carries the three calendar-day-
anchored windows the snapshot assembler attaches to ``CashLedger`` Step 11
enrichment.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RegTExcessAggregates:
    """Three calendar-day-anchored ``regt_excess_over_pm`` sums in USD.

    Trailing windows are anchored at the snapshot's ``now`` and span the
    preceding 30 / 90 calendar days. Lifetime sums every processed fill
    bearing non-null ``regt_attribution_json``; fills predating the
    attribution module contribute zero.
    """

    trailing_30d_usd: float
    trailing_90d_usd: float
    lifetime_usd: float


__all__ = ["RegTExcessAggregates"]
