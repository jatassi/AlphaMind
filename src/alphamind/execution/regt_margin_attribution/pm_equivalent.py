"""PM-equivalent aggregator for the Reg T margin attribution module (ALP-426, story 04).

Ships the thin pure function ``compute_pm_equivalent_margin`` that composes
class-group composition and per-class-group stress into the portfolio-aggregate
IBKR-mirror v1 PM-equivalent margin.

Per ``regt-margin-attribution.md § Aggregation``: no inter-class offsets in v1.
The sum biases the result upward relative to IBKR's actual product-group
methodology output (biasing ``regt_excess_over_pm`` downward — the conservative
direction).

Pure module: no I/O, no clock reads, no global mutation.
"""

from __future__ import annotations

from alphamind.execution.regt_margin_attribution.class_groups import compose_class_groups
from alphamind.execution.regt_margin_attribution.config import RegTMarginAttributionConfig
from alphamind.execution.regt_margin_attribution.pm_stress import stress_class_group
from alphamind.portfolio_state.records.positions import PositionRecord, PositionStatus
from alphamind.risk_guardrails.guardrail_evaluation.types import MarketInputs


def compute_pm_equivalent_margin(
    positions: tuple[PositionRecord, ...],
    market_inputs: MarketInputs,
    config: RegTMarginAttributionConfig,
) -> float:
    """Sum IBKR-mirror v1 per-class-group margins into the portfolio aggregate.

    Pure function. Composition of ``compose_class_groups`` and
    ``stress_class_group``. Returns the portfolio-aggregate PM-equivalent
    initial-margin requirement in USD.

    Per ``regt-margin-attribution.md § Aggregation``: no inter-class
    offsets in v1. The sum biases the result upward relative to IBKR's
    actual product-group methodology output (and therefore biases
    ``regt_excess_over_pm`` downward — the conservative direction).

    Pending / closed positions are excluded via the open-status filter
    applied before class-group composition.
    """
    open_positions = tuple(p for p in positions if p.status == PositionStatus.OPEN)
    if not open_positions:
        return 0.0
    class_groups = compose_class_groups(open_positions)
    return sum(
        stress_class_group(class_group=cg, market_inputs=market_inputs, config=config)
        for cg in class_groups
    )
