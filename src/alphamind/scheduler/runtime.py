"""Runtime-dimensions resolver — produce the four per-invocation runtime values."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from alphamind.config.models.modes import Mode
from alphamind.config.models.overlays import Overlay
from alphamind.config.models.regimes import Regime
from alphamind.config.models.run_types import RunType
from alphamind.config.resolver import RuntimeDimensions
from alphamind.risk_guardrails.breach_behavior.types import HaltState
from alphamind.risk_guardrails.regime_adaptation.types import OverlayActivationDecision
from alphamind.state.tables.invocations import InvocationRow


async def _resolve_active_regime(session: AsyncSession) -> Regime:
    """Return the active regime carried by the most recent successful invocation.

    "Successful" means ``command_execution_completed_at IS NOT NULL`` — the Phase 2 write
    path stamps that column once distillation has finished and the new regime
    label has been persisted. Aborted invocations are skipped because their
    ``active_regime`` reflects the regime they *operated under*, not a newly
    classified label.

    Returns :class:`Regime.normal` when no successful prior invocation exists.
    """
    stmt = (
        select(InvocationRow.active_regime)
        .where(InvocationRow.command_execution_completed_at.is_not(None))
        .order_by(InvocationRow.start_at.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    regime_value = result.scalar_one_or_none()
    if regime_value is None:
        return Regime.normal
    return Regime(regime_value)


def _resolve_active_mode(*, halt_state: HaltState | None) -> Mode:
    if halt_state is None:
        return Mode.normal
    return Mode.halt


def _resolve_active_overlays(
    *,
    pre_event_decision: OverlayActivationDecision,
    stress_decision: OverlayActivationDecision,
) -> tuple[Overlay, ...]:
    active: list[Overlay] = []
    if pre_event_decision.is_active:
        active.append(Overlay.pre_event)
    if stress_decision.is_active:
        active.append(Overlay.stress)
    return tuple(active)


async def resolve_runtime_dimensions(
    session: AsyncSession,
    *,
    firing_trigger: RunType,
    halt_state: HaltState | None,
    pre_event_decision: OverlayActivationDecision,
    stress_decision: OverlayActivationDecision,
) -> RuntimeDimensions:
    """Produce the four per-invocation runtime dimensions from persistent state.

    The caller (story 03b's ``run_invocation``) is responsible for computing
    ``halt_state``, ``pre_event_decision``, and ``stress_decision`` from the
    upstream guardrail and regime-adaptation primitives; this composer only
    folds the precomputed inputs together with the prior-regime DB read.
    """
    active_regime = await _resolve_active_regime(session)
    active_mode = _resolve_active_mode(halt_state=halt_state)
    active_overlays = _resolve_active_overlays(
        pre_event_decision=pre_event_decision,
        stress_decision=stress_decision,
    )
    return RuntimeDimensions(
        active_regime=active_regime,
        active_mode=active_mode,
        active_overlays=active_overlays,
        firing_trigger=firing_trigger,
    )
