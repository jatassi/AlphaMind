"""Paper-evaluation harness — calibrates Alpaca paper fills with estimated live-execution drag."""

from alphamind.execution.paper_evaluation_harness.enrichment import (
    AdvLookup,
    OrderAttributes,
    OrderLookup,
    VolLookup,
    attach_live_execution_estimate,
)
from alphamind.execution.paper_evaluation_harness.fees import compute_regulatory_fees
from alphamind.execution.paper_evaluation_harness.harness import (
    compute_live_execution_estimate,
)
from alphamind.execution.paper_evaluation_harness.impact import estimate_impact
from alphamind.execution.paper_evaluation_harness.spread import estimate_spread

__all__ = [
    "AdvLookup",
    "OrderAttributes",
    "OrderLookup",
    "VolLookup",
    "attach_live_execution_estimate",
    "compute_live_execution_estimate",
    "compute_regulatory_fees",
    "estimate_impact",
    "estimate_spread",
]
