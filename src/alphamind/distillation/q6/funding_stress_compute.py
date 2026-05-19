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
from alphamind.distillation.normalization import percentile_rank

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
    """Output of the funding-stress composite refresh.

    ``component_percentiles`` carries ``None`` for components whose trailing
    series is empty or zero-variance; the assembly site preserves the
    ``None`` so the published payload reads as ``null`` rather than a
    fabricated rank.
    """

    composite_value: float
    components: Mapping[str, float]
    component_percentiles: Mapping[str, float | None]
    components_above_percentile: int
    alert_active: bool
    state: CalibrationState
    bootstrap_reason: str | None


def compute_funding_stress_alert(
    *,
    components: Mapping[str, float],
    prior_history: Sequence[Mapping[str, float]],
    component_alert_count: int,
    component_alert_percentile: float,
) -> tuple[dict[str, float | None], int, bool]:
    """Pure compute over per-component percentiles + aggregate alert.

    Returns ``(component_percentiles, components_above, alert_active)``:

    - ``component_percentiles`` — current value's percentile rank against the
      trailing component series, per component name. ``None`` when the
      trailing series is empty or zero-variance; such a component cannot
      contribute to the aggregate count.
    - ``components_above`` — count of components whose percentile is at or
      above ``component_alert_percentile``. ``None`` percentiles are skipped.
    - ``alert_active`` — True when ``components_above >= component_alert_count``.

    The caller composes this with the persistence-row write so the alert
    column matches the per-component verdict.
    """
    component_percentiles: dict[str, float | None] = {}
    for name, current in components.items():
        trailing = [row[name] for row in prior_history if name in row]
        component_percentiles[name] = percentile_rank(trailing, current)
    components_above = sum(
        1
        for percentile in component_percentiles.values()
        if percentile is not None and percentile >= component_alert_percentile
    )
    alert_active = components_above >= component_alert_count
    return component_percentiles, components_above, alert_active


__all__ = [
    "FUNDING_STRESS_COMPONENT_NAMES",
    "FUNDING_STRESS_COMPOSITE_KIND",
    "FundingStressResult",
    "compute_funding_stress_alert",
]
