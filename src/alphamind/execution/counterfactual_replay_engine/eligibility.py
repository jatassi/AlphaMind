"""Eligibility check and replay-window computation (ALP-558).

:func:`check_eligibility` — per-proposal gate that classifies whether a
proposal is replayable and, if not, which :class:`~.enums.UnevaluableReason`
applies. Rules apply in documented order (first match wins).

:func:`compute_replay_window` — computes the ``[window_start, window_end]``
datetime pair used by both the eligibility check (bar/CA presence queries)
and the downstream simulator (stories 05a, 06, 07).

Repository protocols are defined in :mod:`.repos`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from alphamind.config.models.replay_engine import CounterfactualReplayEngineConfig
from alphamind.decision.analyst.models import (
    InstrumentStrategy,
    Recommendation,
    TimeCondition,
)
from alphamind.decision.strategist.models import (
    AddParameters,
    PendingOrderAssessment,
    PositionAssessment,
)
from alphamind.execution.counterfactual_replay_engine.enums import (
    ReplayStatus,
    UnevaluableReason,
)
from alphamind.execution.counterfactual_replay_engine.repos import (
    BarRepository,
    CorporateActionRepository,
    OptionsSnapshotRepository,
)

if TYPE_CHECKING:
    pass

__all__ = [
    "check_eligibility",
    "compute_replay_window",
]

# Type alias for clarity
_Proposal = Recommendation | PositionAssessment | PendingOrderAssessment
_EligibilityResult = tuple[ReplayStatus, UnevaluableReason | None, datetime | None, datetime | None]


def check_eligibility(
    proposal: _Proposal,
    *,
    proposal_timestamp: datetime,
    config: CounterfactualReplayEngineConfig,
    bar_repo: BarRepository,
    options_snapshot_repo: OptionsSnapshotRepository,
    corporate_action_repo: CorporateActionRepository,
) -> _EligibilityResult:
    """Apply the Step-1 eligibility rules in documented order.

    Returns a 4-tuple ``(status, reason_or_none, window_start_or_none,
    window_end_or_none)``. For UNEVALUABLE results from Rules A and B the
    window is ``(None, None)``; for Rules C and D the window is populated.

    Rules (first match wins):

    A. Strategy instrument → UNSUPPORTED_INSTRUMENT
    B. Strategist hold / maintain → STRATEGIST_POSITION_ACTION_NOT_SUPPORTED
    C. Missing bars (or missing IV for option proposals) → DATA_MISSING
    D. Corporate action in window → CORPORATE_ACTION_IN_WINDOW
    Default → EVALUATED
    """
    # --- Rule A ---
    if isinstance(proposal, Recommendation) and isinstance(
        proposal.instrument, InstrumentStrategy
    ):
        return (ReplayStatus.UNEVALUABLE, UnevaluableReason.UNSUPPORTED_INSTRUMENT, None, None)

    # --- Rule B ---
    if isinstance(proposal, PositionAssessment) and proposal.recommended_action == "hold":
        return (
            ReplayStatus.UNEVALUABLE,
            UnevaluableReason.STRATEGIST_POSITION_ACTION_NOT_SUPPORTED,
            None,
            None,
        )
    if isinstance(proposal, PendingOrderAssessment) and proposal.recommended_action == "maintain":
        return (
            ReplayStatus.UNEVALUABLE,
            UnevaluableReason.STRATEGIST_POSITION_ACTION_NOT_SUPPORTED,
            None,
            None,
        )

    # Compute window for Rules C, D, and EVALUATED
    window_start, window_end = compute_replay_window(
        proposal, proposal_timestamp=proposal_timestamp, config=config
    )

    # --- Rule C: missing bars ---
    underlying = _get_underlying(proposal)
    if not bar_repo.has_bars_over_window(underlying, window_start, window_end):
        return (ReplayStatus.UNEVALUABLE, UnevaluableReason.DATA_MISSING, window_start, window_end)

    # Rule C: missing IV snapshot for option proposals
    if isinstance(proposal, Recommendation) and hasattr(proposal.instrument, "underlying"):
        from alphamind.decision.analyst.models import InstrumentOption
        if isinstance(proposal.instrument, InstrumentOption):
            contract_ticker = _option_contract_ticker(proposal)
            if not options_snapshot_repo.has_snapshot_at_or_before(
                contract_ticker, proposal_timestamp
            ):
                return (
                    ReplayStatus.UNEVALUABLE,
                    UnevaluableReason.DATA_MISSING,
                    window_start,
                    window_end,
                )

    # --- Rule D: corporate action in window ---
    if corporate_action_repo.has_action_in_window(underlying, window_start, window_end):
        return (
            ReplayStatus.UNEVALUABLE,
            UnevaluableReason.CORPORATE_ACTION_IN_WINDOW,
            window_start,
            window_end,
        )

    # --- Default: EVALUATED ---
    return (ReplayStatus.EVALUATED, None, window_start, window_end)


def compute_replay_window(
    proposal: _Proposal,
    *,
    proposal_timestamp: datetime,
    config: CounterfactualReplayEngineConfig,
) -> tuple[datetime, datetime]:
    """Compute the ``[window_start, window_end]`` for *proposal*.

    ``window_start = proposal_timestamp`` always.

    **Analyst :class:`~alphamind.decision.analyst.models.Recommendation`**:

    ``window_end = proposal_timestamp
                  + entry_window_duration       (if entry_window is set)
                  + max(time_stop_horizon, target_estimated_horizon)``

    where ``entry_window_duration = entry_window.deadline - proposal_timestamp``,
    ``time_stop_horizon = latest TimeCondition.deadline - proposal_timestamp``
    (if any ``type="time"`` invalidation leg exists, else zero), and
    ``target_estimated_horizon = timedelta(hours=time_expectation_hours)``.

    **Strategist** (:class:`~alphamind.decision.strategist.models.PositionAssessment`
    or :class:`~alphamind.decision.strategist.models.PendingOrderAssessment`):

    ``window_end = proposal_timestamp + timedelta(hours=config.strategist_default_forward_window_hours)``

    For ADD proposals that carry a ``bracket_adjustment.new_time_expiration``,
    ``window_end`` is extended to that deadline if it falls later than the
    config-driven default.
    """
    window_start = proposal_timestamp

    if isinstance(proposal, Recommendation):
        window_end = _analyst_window_end(proposal, proposal_timestamp)
    else:
        window_end = _strategist_window_end(proposal, proposal_timestamp, config)

    return window_start, window_end


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _analyst_window_end(
    proposal: Recommendation,
    proposal_timestamp: datetime,
) -> datetime:
    """Compute window_end for an analyst Recommendation."""
    # Entry window duration
    entry_window_duration = timedelta(0)
    if proposal.entry_window is not None:
        entry_window_duration = proposal.entry_window.deadline - proposal_timestamp

    # Time-stop horizon: latest TimeCondition.deadline across time-type invalidation legs
    time_stop_horizon = timedelta(0)
    for leg in proposal.invalidation_legs:
        if leg.type == "time" and isinstance(leg.condition, TimeCondition):
            leg_horizon = leg.condition.deadline - proposal_timestamp
            if leg_horizon > time_stop_horizon:
                time_stop_horizon = leg_horizon

    # Target estimated horizon
    target_estimated_horizon = timedelta(hours=proposal.time_expectation_hours)

    window_end = (
        proposal_timestamp
        + entry_window_duration
        + max(time_stop_horizon, target_estimated_horizon)
    )
    return window_end


def _strategist_window_end(
    proposal: PositionAssessment | PendingOrderAssessment,
    proposal_timestamp: datetime,
    config: CounterfactualReplayEngineConfig,
) -> datetime:
    """Compute window_end for a strategist proposal."""
    default_end = proposal_timestamp + timedelta(
        hours=config.strategist_default_forward_window_hours
    )

    # For ADD with bracket_adjustment.new_time_expiration, extend if needed
    if isinstance(proposal, PositionAssessment) and isinstance(
        proposal.action_parameters, AddParameters
    ):
        bracket_adj = proposal.action_parameters.bracket_adjustment
        if bracket_adj is not None and bracket_adj.new_time_expiration is not None:
            expiration = bracket_adj.new_time_expiration
            if expiration > default_end:
                return expiration

    return default_end


def _get_underlying(proposal: _Proposal) -> str:
    """Extract the underlying ticker from any proposal type."""
    if isinstance(proposal, Recommendation):
        return proposal.underlying
    # PositionAssessment and PendingOrderAssessment both have position_id, not underlying directly.
    # PositionAssessment has underlying; PendingOrderAssessment doesn't expose it directly.
    # For bar and CA checks we use the underlying. For PendingOrderAssessment, we need position info.
    # The design says the underlying bar stream drives the check — use position_id's underlying.
    # PendingOrderAssessment doesn't carry underlying directly, but the design doc's bar-check
    # uses the underlying ticker. We derive it from position_id in the full implementation;
    # for now we need a ticker. PendingOrderAssessment has no .underlying field directly —
    # but the story says bar check is on the underlying. We'll use a convention:
    # For PendingOrderAssessment, we need the underlying ticker for bar presence.
    # Looking at the model: PendingOrderAssessment has order_id, position_id but no underlying.
    # The engine driver (story 08) will provide this context; for now the Protocol interface
    # provides has_bars_over_window(ticker, ...) — the driver will supply the ticker.
    # Since this function must return a ticker, and PendingOrderAssessment lacks an underlying field,
    # we use position_id as a placeholder ticker for the repo calls.
    # Story 08 will wire the actual position's underlying.
    if isinstance(proposal, PositionAssessment):
        return proposal.underlying
    # PendingOrderAssessment: no underlying field — use position_id as the repo key.
    # The SQL-backed repo implementation (story 08) looks up the actual underlying.
    return proposal.position_id


def _option_contract_ticker(proposal: Recommendation) -> str:
    """Derive an OCC-style contract ticker for the option IV snapshot lookup."""
    from alphamind.decision.analyst.models import InstrumentOption
    instr = proposal.instrument
    assert isinstance(instr, InstrumentOption)
    # OCC format: UNDERLYING + YYMMDD + C/P + strike (8-digit, 3 decimal places)
    exp = instr.expiration
    exp_str = exp.strftime("%y%m%d")
    cp = "C" if instr.contract_type == "call" else "P"
    strike_int = int(float(str(instr.strike)) * 1000)
    return f"{instr.underlying}{exp_str}{cp}{strike_int:08d}"
