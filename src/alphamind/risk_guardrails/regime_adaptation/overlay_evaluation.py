"""Filesystem-/DB-loading shells for the two regime-adaptation activators (ALP-472 lift).

The scheduler's pre-event and stress activators take pure inputs
(`event_calendar`, `composite_alert_state`); this module owns the loading
side so the orchestrator no longer has to thread those reads inline.

* :func:`evaluate_pre_event_decision` reads ``event_calendar.yaml`` from
  the config dir and calls :func:`evaluate_pre_event_overlay`.
* :func:`evaluate_stress_decision` fetches the composite-alert state via
  ``AsyncSession.run_sync`` (the canonical sync-bridge for the SQL helper)
  and calls :func:`evaluate_stress_overlay`.

Both helpers also assert the per-overlay sub-record type at the boundary
so a malformed ``overlays_map`` fails fast with a clear message rather than
relying on attribute-access failures deeper in the activator.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.config.models.overlays import Overlay, PreEventOverlay, StressOverlay
from alphamind.config.models.scheduler import SchedulerConfig
from alphamind.risk_guardrails.regime_adaptation.event_calendar import (
    load_event_calendar,
)
from alphamind.risk_guardrails.regime_adaptation.pre_event_activator import (
    evaluate_pre_event_overlay,
)
from alphamind.risk_guardrails.regime_adaptation.stress_activator import (
    evaluate_stress_overlay,
    fetch_composite_alert_state,
)
from alphamind.risk_guardrails.regime_adaptation.types import (
    OverlayActivationDecision,
)

__all__ = [
    "evaluate_pre_event_decision",
    "evaluate_stress_decision",
]


def evaluate_pre_event_decision(
    *,
    now: datetime,
    config_dir: Path,
    overlays_map: Mapping[Overlay, PreEventOverlay | StressOverlay],
    scheduler_config: SchedulerConfig,
) -> OverlayActivationDecision:
    """Load the event calendar and run the pre-event activator."""
    pre_event_overlay = overlays_map[Overlay.pre_event]
    if not isinstance(pre_event_overlay, PreEventOverlay):
        msg = f"overlays_map[Overlay.pre_event] is not a PreEventOverlay: {pre_event_overlay!r}"
        raise TypeError(msg)
    event_calendar = load_event_calendar(config_dir / "event_calendar.yaml")
    return evaluate_pre_event_overlay(
        now_utc=now,
        event_calendar=event_calendar,
        scheduler_config=scheduler_config,
        pre_event_overlay=pre_event_overlay,
    )


async def evaluate_stress_decision(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    overlays_map: Mapping[Overlay, PreEventOverlay | StressOverlay],
) -> OverlayActivationDecision:
    """Fetch the composite alert state via a sync-session bridge and evaluate stress.

    :func:`fetch_composite_alert_state` is a synchronous helper that consumes
    a sync :class:`~sqlalchemy.orm.Session` (it queries the
    ``DistillationCompositeState`` table the regime-adaptation orchestrator
    persists). The pipeline scheduler runs on async sessions;
    :meth:`AsyncSession.run_sync` is SQLAlchemy 2.x's canonical bridge that
    hands the sync helper the underlying :class:`Session` connected to the
    same DB.
    """
    stress_overlay = overlays_map[Overlay.stress]
    if not isinstance(stress_overlay, StressOverlay):
        msg = f"overlays_map[Overlay.stress] is not a StressOverlay: {stress_overlay!r}"
        raise TypeError(msg)
    async with session_factory() as session:
        composite_alert_state = await session.run_sync(
            lambda sync_session: fetch_composite_alert_state(sync_session)
        )
    return evaluate_stress_overlay(
        funding_stress_alert_active=composite_alert_state.funding_stress_alert_active,
        market_liquidity_alert_active=composite_alert_state.market_liquidity_alert_active,
        funding_stress_calibration_state=composite_alert_state.funding_stress_calibration_state,
        market_liquidity_calibration_state=composite_alert_state.market_liquidity_calibration_state,
        stress_overlay=stress_overlay,
    )
