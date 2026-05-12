"""``HaltTransitionTracker`` — emits ``HALT_ACTIVATED`` / ``HALT_LIFTED`` events.

The breach-evaluation loop calls :meth:`HaltTransitionTracker.observe` on
every tick. The tracker remembers the prior tick's daily-halt and
cumulative-full-halt states so it only emits an activity-log entry on the
inactive→active or active→inactive transitions — *not* on every cycle while
the halt persists in either state.

Per ``docs/design/06-risk-guardrails/breach-behavior.md`` § *Drawdown halt
mode*, daily-halt and cumulative-tier-3 full-halt are independent boolean
flags. A single ``HaltState`` can carry both simultaneously, and they can
transition independently. The tracker handles each flag independently and
returns zero, one, or two entries per ``observe`` call accordingly.

Design reference:
* Parent issue ALP-123 § Pre-resolved decision (H) — halt-state activation
  logging behavior.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from datetime import datetime

from alphamind.portfolio_state.events.activity_log import (
    ActivityLogEntry,
    EventGroup,
    EventSource,
    EventType,
    HaltActivatedDetail,
    HaltLiftedDetail,
)
from alphamind.risk_guardrails.breach_behavior import HaltState


class HaltTransitionTracker:
    """Stateful tracker over (daily, cumulative) halt-active booleans.

    The two booleans are tracked independently so a simultaneous activation
    emits two entries on the same tick, and an independent lift emits exactly
    one. Persistence (active stays active) emits nothing.

    Not thread-safe — the breach loop is the single caller and runs serially
    in one asyncio task.
    """

    def __init__(self) -> None:
        self._daily_active: bool = False
        self._cumulative_active: bool = False

    def observe(
        self,
        *,
        halt_state: HaltState | None,
        invocation_id: str,
        at: datetime,
        source: EventSource,
        entry_id_factory: Callable[[int], str],
        current_daily_drawdown_pct: float | None = None,
        current_cumulative_drawdown_pct: float | None = None,
        cumulative_full_halt_limit_pct: float | None = None,
    ) -> Iterable[ActivityLogEntry]:
        """Emit transition entries for the latest ``halt_state``.

        Returns at most one entry per halt-type per call. Internal state is
        always updated to match the new ``halt_state`` before returning.

        When ``halt_state`` is ``None`` (no halt active), the tracker treats
        both booleans as ``False`` and emits ``HALT_LIFTED`` entries for any
        flags that were active. ``current_daily_drawdown_pct`` /
        ``current_cumulative_drawdown_pct`` carry the post-lift readings; when
        not supplied the entry records ``0.0`` (the lift implies the rule is
        back below its limit).
        """
        daily_now = halt_state is not None and halt_state.daily_halt_active
        cumulative_now = halt_state is not None and halt_state.cumulative_full_halt_active

        # Determine the readings the entries should record. For active
        # transitions we read from ``halt_state``; for lifts we honor the
        # caller-supplied post-lift readings (with 0.0 as a documented default).
        daily_active_drawdown_pct = halt_state.daily_drawdown_pct if halt_state is not None else 0.0
        daily_active_limit_pct = (
            halt_state.daily_drawdown_limit_pct if halt_state is not None else 0.0
        )
        cumulative_active_drawdown_pct = (
            current_cumulative_drawdown_pct if current_cumulative_drawdown_pct is not None else 0.0
        )
        cumulative_active_limit_pct = (
            cumulative_full_halt_limit_pct if cumulative_full_halt_limit_pct is not None else 0.0
        )

        entries = list(
            self._derive_entries(
                daily_was=self._daily_active,
                daily_now=daily_now,
                cumulative_was=self._cumulative_active,
                cumulative_now=cumulative_now,
                invocation_id=invocation_id,
                at=at,
                source=source,
                entry_id_factory=entry_id_factory,
                daily_active_drawdown_pct=daily_active_drawdown_pct,
                daily_active_limit_pct=daily_active_limit_pct,
                daily_lift_drawdown_pct=(
                    current_daily_drawdown_pct if current_daily_drawdown_pct is not None else 0.0
                ),
                cumulative_active_drawdown_pct=cumulative_active_drawdown_pct,
                cumulative_active_limit_pct=cumulative_active_limit_pct,
                cumulative_lift_drawdown_pct=cumulative_active_drawdown_pct,
            )
        )

        self._daily_active = daily_now
        self._cumulative_active = cumulative_now
        return entries

    @staticmethod
    def _derive_entries(  # noqa: PLR0913
        *,
        daily_was: bool,
        daily_now: bool,
        cumulative_was: bool,
        cumulative_now: bool,
        invocation_id: str,
        at: datetime,
        source: EventSource,
        entry_id_factory: Callable[[int], str],
        daily_active_drawdown_pct: float,
        daily_active_limit_pct: float,
        daily_lift_drawdown_pct: float,
        cumulative_active_drawdown_pct: float,
        cumulative_active_limit_pct: float,
        cumulative_lift_drawdown_pct: float,
    ) -> Iterator[ActivityLogEntry]:
        counter = 0
        if not daily_was and daily_now:
            yield ActivityLogEntry(
                entry_id=entry_id_factory(counter),
                invocation_id=invocation_id,
                timestamp=at,
                event_type=EventType.HALT_ACTIVATED,
                event_group=EventGroup.RISK_AND_GUARDRAIL,
                position_id=None,
                order_id=None,
                thesis_id=None,
                source=source,
                detail=HaltActivatedDetail(
                    halt_type="daily_drawdown",
                    current_drawdown_pct=daily_active_drawdown_pct,
                    limit_pct=daily_active_limit_pct,
                    detected_at=at,
                ),
            )
            counter += 1
        elif daily_was and not daily_now:
            yield ActivityLogEntry(
                entry_id=entry_id_factory(counter),
                invocation_id=invocation_id,
                timestamp=at,
                event_type=EventType.HALT_LIFTED,
                event_group=EventGroup.RISK_AND_GUARDRAIL,
                position_id=None,
                order_id=None,
                thesis_id=None,
                source=source,
                detail=HaltLiftedDetail(
                    halt_type="daily_drawdown",
                    current_drawdown_pct=daily_lift_drawdown_pct,
                    lifted_at=at,
                ),
            )
            counter += 1

        if not cumulative_was and cumulative_now:
            yield ActivityLogEntry(
                entry_id=entry_id_factory(counter),
                invocation_id=invocation_id,
                timestamp=at,
                event_type=EventType.HALT_ACTIVATED,
                event_group=EventGroup.RISK_AND_GUARDRAIL,
                position_id=None,
                order_id=None,
                thesis_id=None,
                source=source,
                detail=HaltActivatedDetail(
                    halt_type="cumulative_drawdown_tier3",
                    current_drawdown_pct=cumulative_active_drawdown_pct,
                    limit_pct=cumulative_active_limit_pct,
                    detected_at=at,
                ),
            )
            counter += 1
        elif cumulative_was and not cumulative_now:
            yield ActivityLogEntry(
                entry_id=entry_id_factory(counter),
                invocation_id=invocation_id,
                timestamp=at,
                event_type=EventType.HALT_LIFTED,
                event_group=EventGroup.RISK_AND_GUARDRAIL,
                position_id=None,
                order_id=None,
                thesis_id=None,
                source=source,
                detail=HaltLiftedDetail(
                    halt_type="cumulative_drawdown_tier3",
                    current_drawdown_pct=cumulative_lift_drawdown_pct,
                    lifted_at=at,
                ),
            )
            counter += 1
