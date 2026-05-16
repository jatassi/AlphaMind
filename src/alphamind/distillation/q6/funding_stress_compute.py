"""Q6 funding-stress composite — pure compute (ALP-485).

The funding-stress composite mixes pure compute (percentile rank against
trailing component history) with session-bound writes
(``refresh_composite_state`` + the per-component-verdict ``alert_active``
override). The pure half lives here; the writes live in
:mod:`alphamind.distillation.q6._loaders`.

After the loader has refreshed the persistence row and run the pure
compute, the resulting :class:`FundingStressResult` carries every value
the Phase 2 ``asyncio.to_thread`` core needs to assemble the
``q6.funding_stress`` :class:`OutputBlock` without further DB access.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from alphamind.distillation._calibration_core import CalibrationState

FUNDING_STRESS_COMPOSITE_KIND = "funding_stress"

# The four named funding-stress components per ``external.md`` § quant 6e.
# A constant so the assembly site and the persistence column constraints
# share a single vocabulary.
FUNDING_STRESS_COMPONENT_NAMES: tuple[str, ...] = (
    "sofr_ois_spread",
    "repo_treasury_spread",
    "term_repo_premium",
    "mmf_flow",
)


@dataclass(frozen=True, slots=True)
class FundingStressResult:
    """Output of the funding-stress composite refresh."""

    composite_value: float
    components: Mapping[str, float]
    component_percentiles: Mapping[str, float]
    components_above_percentile: int
    alert_active: bool
    state: CalibrationState
    bootstrap_reason: str | None


def component_percentile(value: float, history: Sequence[float]) -> float:
    """Percentile rank of ``value`` against ``history`` in 0..100.

    Uses the "<= value" convention so a value at the top of the distribution
    reports 100. An empty history returns 0.0 — the caller decides whether
    that's signal or noise.
    """
    if not history:
        return 0.0
    le = sum(1 for x in history if x <= value)
    return float(le) / float(len(history)) * 100.0


def compute_funding_stress_alert(
    *,
    components: Mapping[str, float],
    prior_history: Sequence[Mapping[str, float]],
    component_alert_count: int,
    component_alert_percentile: float,
) -> tuple[dict[str, float], int, bool]:
    """Pure compute over per-component percentiles + aggregate alert.

    Returns ``(component_percentiles, components_above, alert_active)``:

    - ``component_percentiles`` — current value's percentile rank against the
      trailing component series, per component name.
    - ``components_above`` — count of components whose percentile is at or
      above ``component_alert_percentile``.
    - ``alert_active`` — True when ``components_above >= component_alert_count``.

    The caller composes this with the persistence-row write so the alert
    column matches the per-component verdict.
    """
    component_percentiles: dict[str, float] = {}
    for name, current in components.items():
        trailing = [row[name] for row in prior_history if name in row]
        component_percentiles[name] = component_percentile(current, trailing)
    components_above = sum(
        1
        for percentile in component_percentiles.values()
        if percentile >= component_alert_percentile
    )
    alert_active = components_above >= component_alert_count
    return component_percentiles, components_above, alert_active


__all__ = [
    "FUNDING_STRESS_COMPONENT_NAMES",
    "FUNDING_STRESS_COMPOSITE_KIND",
    "FundingStressResult",
    "component_percentile",
    "compute_funding_stress_alert",
]
