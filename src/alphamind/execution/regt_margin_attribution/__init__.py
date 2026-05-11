"""Reg T margin attribution — IBKR-mirror v1 reference model.

Per ``regt-margin-attribution.md``, the OMS computes, per fill, the marginal
Reg T margin consumed and the marginal portfolio-margin-equivalent that would
have been consumed under a published broker portfolio-margin model.

This package provides the configuration skeleton (story 01a). Later stories
add the class-group composition, stress revaluation, aggregator, per-fill
orchestrator, and Phase 1 wedge modules.
"""

from __future__ import annotations

from alphamind.execution.regt_margin_attribution.aggregates import RegTExcessAggregates
from alphamind.execution.regt_margin_attribution.class_groups import (
    ClassGroup,
    compose_class_groups,
)
from alphamind.execution.regt_margin_attribution.config import (
    IvShockMultipliers,
    RegTMarginAttributionConfig,
    ShockParameters,
    load_regt_margin_attribution_config,
)
from alphamind.execution.regt_margin_attribution.orchestrator import compute_attribution
from alphamind.execution.regt_margin_attribution.pm_equivalent import (
    compute_pm_equivalent_margin,
)
from alphamind.execution.regt_margin_attribution.pm_stress import stress_class_group
from alphamind.execution.regt_margin_attribution.regt_margin import compute_regt_margin

__all__ = [
    "ClassGroup",
    "IvShockMultipliers",
    "RegTExcessAggregates",
    "RegTMarginAttributionConfig",
    "ShockParameters",
    "compose_class_groups",
    "compute_attribution",
    "compute_pm_equivalent_margin",
    "compute_regt_margin",
    "load_regt_margin_attribution_config",
    "stress_class_group",
]
