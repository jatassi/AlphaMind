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

from alphamind.config.models.replay_engine import CounterfactualReplayEngineConfig
from alphamind.decision.analyst.models import (
    InstrumentOption,
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

__all__ = [
    "check_eligibility",
    "compute_replay_window",
]

# Type alias for the three proposal shapes the engine processes.
_Proposal = Recommendation | PositionAssessment | PendingOrderAssessment
_EligibilityResult = tuple[ReplayStatus, UnevaluableReason | None, datetime | None, datetime | None]

_UNEVALUABLE = ReplayStatus.UNEVALUABLE
_EVALUATED = ReplayStatus.EVALUATED


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
    rule_a = _check_rule_a(proposal)
    if rule_a is not None:
        return rule_a

    # --- Rule B ---
    rule_b = _check_rule_b(proposal)
    if rule_b is not None:
        return rule_b

    # Compute window for Rules C, D, and EVALUATED
    window_start, window_end = compute_replay_window(
        proposal, proposal_timestamp=proposal_timestamp, config=config
    )
    underlying = _get_underlying(proposal)

    # --- Rule C: missing bars ---
    if not bar_repo.has_bars_over_window(underlying, window_start, window_end):
        return (_UNEVALUABLE, UnevaluableReason.DATA_MISSING, window_start, window_end)

    # Rule C continued: missing IV snapshot for option proposals
    if isinstance(proposal, Recommendation) and isinstance(proposal.instrument, InstrumentOption):
        contract_ticker = _option_contract_ticker(proposal)
        if not options_snapshot_repo.has_snapshot_at_or_before(contract_ticker, proposal_timestamp):
            return (_UNEVALUABLE, UnevaluableReason.DATA_MISSING, window_start, window_end)

    # --- Rule D: corporate action in window ---
    if corporate_action_repo.has_action_in_window(underlying, window_start, window_end):
        return (
            _UNEVALUABLE,
            UnevaluableReason.CORPORATE_ACTION_IN_WINDOW,
            window_start,
            window_end,
        )

    # --- Default: EVALUATED ---
    return (_EVALUATED, None, window_start, window_end)


def compute_replay_window(
    proposal: _Proposal,
    *,
    proposal_timestamp: datetime,
    config: CounterfactualReplayEngineConfig,
) -> tuple[datetime, datetime]:
    """Compute the ``[window_start, window_end]`` for *proposal*.

    ``window_start = proposal_timestamp`` always.

    **Analyst** :class:`~alphamind.decision.analyst.models.Recommendation`:
    ``window_end = proposal_timestamp + entry_window_duration
    + max(time_stop_horizon, target_estimated_horizon)``,
    where ``entry_window_duration = entry_window.deadline - proposal_timestamp``
    (zero when no entry_window), ``time_stop_horizon`` is the latest
    ``TimeCondition.deadline - proposal_timestamp`` across time-type
    invalidation legs (zero if none), and
    ``target_estimated_horizon = timedelta(hours=time_expectation_hours)``.

    **Strategist** proposals use
    ``proposal_timestamp + timedelta(hours=config.strategist_default_forward_window_hours)``.
    For ADD proposals with ``bracket_adjustment.new_time_expiration``,
    ``window_end`` extends to that deadline when it falls later than the default.
    """
    window_start = proposal_timestamp
    if isinstance(proposal, Recommendation):
        return window_start, _analyst_window_end(proposal, proposal_timestamp)
    return window_start, _strategist_window_end(proposal, proposal_timestamp, config)


# ---------------------------------------------------------------------------
# Private rule helpers
# ---------------------------------------------------------------------------


def _check_rule_a(proposal: _Proposal) -> _EligibilityResult | None:
    """Return Rule A result or None if rule doesn't apply."""
    if isinstance(proposal, Recommendation) and isinstance(proposal.instrument, InstrumentStrategy):
        return (_UNEVALUABLE, UnevaluableReason.UNSUPPORTED_INSTRUMENT, None, None)
    return None


def _check_rule_b(proposal: _Proposal) -> _EligibilityResult | None:
    """Return Rule B result or None if rule doesn't apply."""
    if isinstance(proposal, PositionAssessment) and proposal.recommended_action == "hold":
        return (
            _UNEVALUABLE,
            UnevaluableReason.STRATEGIST_POSITION_ACTION_NOT_SUPPORTED,
            None,
            None,
        )
    if isinstance(proposal, PendingOrderAssessment) and proposal.recommended_action == "maintain":
        return (
            _UNEVALUABLE,
            UnevaluableReason.STRATEGIST_POSITION_ACTION_NOT_SUPPORTED,
            None,
            None,
        )
    return None


# ---------------------------------------------------------------------------
# Private window helpers
# ---------------------------------------------------------------------------


def _analyst_window_end(
    proposal: Recommendation,
    proposal_timestamp: datetime,
) -> datetime:
    """Compute window_end for an analyst Recommendation."""
    entry_window_duration = timedelta(0)
    if proposal.entry_window is not None:
        entry_window_duration = proposal.entry_window.deadline - proposal_timestamp

    time_stop_horizon = timedelta(0)
    for leg in proposal.invalidation_legs:
        if leg.type == "time" and isinstance(leg.condition, TimeCondition):
            leg_horizon = leg.condition.deadline - proposal_timestamp
            if leg_horizon > time_stop_horizon:
                time_stop_horizon = leg_horizon

    target_estimated_horizon = timedelta(hours=proposal.time_expectation_hours)
    return (
        proposal_timestamp
        + entry_window_duration
        + max(time_stop_horizon, target_estimated_horizon)
    )


def _strategist_window_end(
    proposal: PositionAssessment | PendingOrderAssessment,
    proposal_timestamp: datetime,
    config: CounterfactualReplayEngineConfig,
) -> datetime:
    """Compute window_end for a strategist proposal."""
    default_end = proposal_timestamp + timedelta(
        hours=config.strategist_default_forward_window_hours
    )
    # For ADD with bracket_adjustment.new_time_expiration, extend if needed.
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
    """Extract the underlying ticker from any proposal type.

    For :class:`~alphamind.decision.strategist.models.PendingOrderAssessment`
    the model carries no ``underlying`` field — ``position_id`` is used as
    the repo key here; the SQL-backed repo (story 08) resolves the actual
    underlying from the position record.
    """
    if isinstance(proposal, Recommendation):
        return proposal.underlying
    if isinstance(proposal, PositionAssessment):
        return proposal.underlying
    # PendingOrderAssessment: no .underlying — use position_id as repo key.
    return proposal.position_id


def _option_contract_ticker(proposal: Recommendation) -> str:
    """Derive an OCC-style contract ticker for the option IV snapshot lookup."""
    instr = proposal.instrument
    assert isinstance(instr, InstrumentOption)
    exp_str = instr.expiration.strftime("%y%m%d")
    cp = "C" if instr.contract_type == "call" else "P"
    strike_int = int(float(str(instr.strike)) * 1000)
    return f"{instr.underlying}{exp_str}{cp}{strike_int:08d}"
