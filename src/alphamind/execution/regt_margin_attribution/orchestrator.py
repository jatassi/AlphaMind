"""Per-fill attribution orchestrator (ALP-427, story 05).

Ships the pure function ``compute_attribution`` that snapshots Reg T and
PM-equivalent margins against pre- and post-fill position sets and packages
the eight-field ``RegTMarginAttribution`` record consumed by the Phase 1
fill-integration write path (story 06a).

Pure module: no I/O beyond the inputs' provider lookups, no clock reads, no
global mutation, no caching.
"""

from __future__ import annotations

from alphamind._kernel.money import signed_money
from alphamind.execution.regt_margin_attribution.config import RegTMarginAttributionConfig
from alphamind.execution.regt_margin_attribution.pm_equivalent import (
    compute_pm_equivalent_margin,
)
from alphamind.execution.regt_margin_attribution.regt_margin import compute_regt_margin
from alphamind.portfolio_state.records.positions import PositionRecord
from alphamind.risk_guardrails.guardrail_evaluation.types import MarketInputs
from alphamind.state.records import (
    RegTMarginAttribution,
)


def compute_attribution(
    *,
    pre_fill_positions: tuple[PositionRecord, ...],
    post_fill_positions: tuple[PositionRecord, ...],
    market_inputs: MarketInputs,
    config: RegTMarginAttributionConfig,
) -> RegTMarginAttribution:
    """Compute the per-fill Reg T margin attribution record.

    Pure function. Snapshots Reg T and PM-equivalent margins against the
    pre- and post-fill position sets, computes the four marginal deltas, and
    packages the eight-field ``RegTMarginAttribution`` record per
    ``regt-margin-attribution.md § Outputs``.

    The eight straight-line steps (no branching, no caching, no internal state):

    1. ``regt_margin_before  = compute_regt_margin(pre_fill_positions, ...)``
    2. ``pm_equivalent_before = compute_pm_equivalent_margin(pre_fill_positions, ...)``
    3. ``regt_margin_after   = compute_regt_margin(post_fill_positions, ...)``
    4. ``pm_equivalent_after = compute_pm_equivalent_margin(post_fill_positions, ...)``
    5. ``regt_marginal_consumption = regt_margin_after - regt_margin_before``
    6. ``pm_marginal_consumption   = pm_equivalent_after - pm_equivalent_before``
    7. ``regt_excess_over_pm = regt_marginal_consumption - pm_marginal_consumption``
    8. ``pm_model_version = config.pm_model_version``

    No special-casing for zero-delta fills (the algebra produces zeros
    correctly) and no caching across calls (orchestrator is stateless).

    Propagates ``KeyError`` from :func:`compute_regt_margin` if a position's
    underlying is missing from ``market_inputs.underlying_prices``.
    Propagates ``IvLookupError`` from :func:`compute_pm_equivalent_margin`
    if an option leg's IV is unavailable.
    """
    regt_margin_before = compute_regt_margin(pre_fill_positions, market_inputs.underlying_prices)
    pm_equivalent_before = compute_pm_equivalent_margin(pre_fill_positions, market_inputs, config)
    regt_margin_after = compute_regt_margin(post_fill_positions, market_inputs.underlying_prices)
    pm_equivalent_after = compute_pm_equivalent_margin(post_fill_positions, market_inputs, config)
    regt_marginal_consumption = regt_margin_after - regt_margin_before
    pm_marginal_consumption = pm_equivalent_after - pm_equivalent_before
    regt_excess_over_pm = regt_marginal_consumption - pm_marginal_consumption
    # ALP-462 — RegTMarginAttribution fields carry ``Money``; the upstream
    # compute functions still produce float, so wrap via ``signed_money`` so
    # the durability layer round-trips Decimal-exact values.
    return RegTMarginAttribution(
        regt_margin_before=signed_money(str(regt_margin_before)),
        regt_margin_after=signed_money(str(regt_margin_after)),
        regt_marginal_consumption=signed_money(str(regt_marginal_consumption)),
        pm_equivalent_before=signed_money(str(pm_equivalent_before)),
        pm_equivalent_after=signed_money(str(pm_equivalent_after)),
        pm_marginal_consumption=signed_money(str(pm_marginal_consumption)),
        regt_excess_over_pm=signed_money(str(regt_excess_over_pm)),
        pm_model_version=config.pm_model_version,
    )
