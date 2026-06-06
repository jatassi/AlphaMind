"""Per-proposal + batch engine driver (ALP-564, story 08 §3 / §4).

The imperative shell that composes the already-built pure replay primitives
against a production SQLAlchemy ``Session``:

* :func:`replay_proposal` — replays one PM decision end-to-end (idempotency →
  hydrate → eligibility → route → map → persist), returning the written
  ``CounterfactualReplayRecord`` or ``None`` on an idempotency hit.
* :func:`replay_pending_proposals` — walks the due-proposal queue, replays each,
  folds per-reason counts into a frozen :class:`ReplayBatchResult`, and catches
  the three typed per-proposal errors so one malformed proposal never aborts the
  batch.

The driver has no I/O concern beyond the session and the config: ADV /
realized-volatility are passed as ``None`` (the paper-harness primitive degrades
gracefully), and the paper-harness liquidity / spread envelope flags are
``None`` ("unknown" — the classifier does not demote on them alone), matching the
v2 confidence inputs the design specifies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from typing import TYPE_CHECKING, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.decision.analyst.models import InstrumentOption, Recommendation
from alphamind.decision.strategist.models import PendingOrderAssessment, PositionAssessment
from alphamind.execution.counterfactual_replay_engine.eligibility import check_eligibility
from alphamind.execution.counterfactual_replay_engine.enums import ReplayKind, ReplayStatus
from alphamind.execution.counterfactual_replay_engine.equity_replay import replay_equity_proposal
from alphamind.execution.counterfactual_replay_engine.iv_lookup import (
    SqlOptionsSnapshotRepository,
)
from alphamind.execution.counterfactual_replay_engine.option_replay import replay_option_proposal
from alphamind.execution.counterfactual_replay_engine.proposal_hydration import (
    ProposalHydrationError,
    hydrate_originating_proposal,
)
from alphamind.execution.counterfactual_replay_engine.queue import iter_pending_replay_proposals
from alphamind.execution.counterfactual_replay_engine.record_mapping import (
    MappingContext,
    record_from_equity_result,
    record_from_option_result,
    record_from_strategist_result,
)
from alphamind.execution.counterfactual_replay_engine.records import CounterfactualReplayRecord
from alphamind.execution.counterfactual_replay_engine.repos import OhlcvBar
from alphamind.execution.counterfactual_replay_engine.sql_repos import (
    SqlBarRepository,
    SqlCorporateActionRepository,
)
from alphamind.execution.counterfactual_replay_engine.strategist_replay import (
    reconstruct_pending_order_entry,
    replay_pending_order_proposal,
    replay_strategist_proposal,
)
from alphamind.execution.counterfactual_replay_engine.unevaluable_writer import (
    write_unevaluable_record,
)
from alphamind.portfolio_state.records.orders import OrderRecord
from alphamind.portfolio_state.records.positions import OptionsPositionDetails
from alphamind.state.repository.counterfactual_replays import (
    insert_counterfactual_replay,
    load_counterfactual_replays_for_envelope,
)
from alphamind.state.repository.position_state import (
    PositionStateNotFoundError,
    load_position_state_at,
)
from alphamind.state.tables.orders import OrderRow
from alphamind.state.tables.orders_codec import row_to_record as order_row_to_record

if TYPE_CHECKING:
    from alphamind._kernel.ids import EnvelopeId
    from alphamind.config.models.execution import PaperHarness
    from alphamind.config.models.replay_engine import CounterfactualReplayEngineConfig
    from alphamind.portfolio_state.events.activity_log import ActivityLogEntry
    from alphamind.portfolio_state.events.pm_decision import PMDecisionDetail
    from alphamind.state.repository.position_state import PositionStateSnapshot

__all__ = [
    "OriginatingProposalLookupError",
    "ReplayBatchResult",
    "replay_pending_proposals",
    "replay_proposal",
]

# 15-minute bars: consecutive bars are 900s apart. A larger gap is a coverage hole.
_BAR_INTERVAL_SECONDS = 900.0


class OriginatingProposalLookupError(Exception):
    """Raised when a pending-order proposal references state the driver can't read.

    Distinct from :class:`ProposalHydrationError` (the proposal JSON body itself
    is malformed) and :class:`PositionStateNotFoundError` (the position fold
    fails): this is the I/O-level "the order this assessment names does not exist"
    failure. Caught and counted at the batch boundary like the others.
    """


@dataclass(frozen=True, slots=True)
class _ReplayInputs:
    """The per-run knobs threaded inward, bundled to keep signatures small.

    ``as_of`` is the batch "now" used to gate the evaluation horizon; the
    position-state and eligibility ``proposal_timestamp`` is the PM-decision
    entry's own timestamp, not this value.
    """

    config: CounterfactualReplayEngineConfig
    paper_harness_config: PaperHarness
    risk_free_rate: float
    as_of: datetime


@dataclass(frozen=True, slots=True)
class ReplayBatchResult:
    """The roll-up a :func:`replay_pending_proposals` run returns.

    ``unevaluable_by_reason`` keys are the :class:`UnevaluableReason` *values*
    (the persisted strings). ``error_count`` / ``first_error`` capture the typed
    per-proposal failures the batch swallowed (a malformed proposal must not
    abort the run); ``first_error`` is the message of the first one for triage.
    """

    evaluated: int
    unevaluable_by_reason: dict[str, int]
    skipped_idempotent: int
    error_count: int
    first_error: str | None


def replay_proposal(
    session: Session,
    entry: ActivityLogEntry,
    detail: PMDecisionDetail,
    replay_kind: ReplayKind,
    *,
    config: CounterfactualReplayEngineConfig,
    paper_harness_config: PaperHarness,
    risk_free_rate: float,
    as_of: datetime,
) -> CounterfactualReplayRecord | None:
    """Replay one PM decision and persist a ``CounterfactualReplayRecord``.

    Returns ``None`` when ``(envelope_id, replay_kind)`` is already recorded
    (idempotency) or when the proposal's evaluation horizon has not elapsed by
    ``as_of`` (not yet due). Otherwise hydrates the proposal, runs the Step-1
    eligibility gate (writing an UNEVALUABLE record and returning on a fail), then
    routes to the equity / option / strategist replay path, maps the result to a
    record, persists it, and returns it. The proposal timestamp threaded into
    eligibility + window + position-state is ``entry.timestamp`` (the proposal
    carries none); ``as_of`` is the batch "now" the horizon is gated against.
    """
    envelope_id = _envelope_id(detail)
    if _already_recorded(session, envelope_id, replay_kind):
        return None

    inputs = _ReplayInputs(
        config=config,
        paper_harness_config=paper_harness_config,
        risk_free_rate=risk_free_rate,
        as_of=as_of,
    )
    proposal = hydrate_originating_proposal(detail)
    bar_repo = SqlBarRepository(session)
    options_repo = SqlOptionsSnapshotRepository(session)
    corporate_action_repo = SqlCorporateActionRepository(session)

    status, reason, window_start, window_end = check_eligibility(
        proposal,
        proposal_timestamp=entry.timestamp,
        config=config,
        bar_repo=bar_repo,
        options_snapshot_repo=options_repo,
        corporate_action_repo=corporate_action_repo,
    )
    if status is ReplayStatus.UNEVALUABLE:
        assert reason is not None
        return write_unevaluable_record(session, envelope_id, replay_kind, reason)

    assert window_start is not None
    assert window_end is not None
    # The evaluation horizon must have elapsed by ``as_of``. The queue gates this
    # for batch runs, but a direct (CLI single-proposal) call must not replay an
    # un-settled window — skip it, same as an idempotency hit, rather than write a
    # half-formed counterfactual the operator would have to purge.
    if window_end > inputs.as_of:
        return None

    bars = bar_repo.load_bars(ticker=_repo_key(proposal), start=window_start, end=window_end)
    ctx = MappingContext(
        envelope_id=envelope_id,
        replay_kind=replay_kind,
        bar_coverage_complete=_bar_coverage_complete(bars),
        liquidity_within_typical_envelope=None,
        spread_within_typical_envelope=None,
        window_start=window_start,
        window_end=window_end,
        replay_timestamp=datetime.now(UTC),
    )
    record = _route_and_map(
        session,
        proposal,
        ctx=ctx,
        inputs=inputs,
        bars=bars,
        options_repo=options_repo,
        proposal_timestamp=entry.timestamp,
    )
    insert_counterfactual_replay(session, record)
    return record


def replay_pending_proposals(
    session: Session,
    *,
    config: CounterfactualReplayEngineConfig,
    paper_harness_config: PaperHarness,
    risk_free_rate: float,
    as_of: datetime,
    since: datetime | None = None,
) -> ReplayBatchResult:
    """Replay every due PM decision, accumulating per-reason counts.

    Per-proposal :class:`ProposalHydrationError`, :class:`PositionStateNotFoundError`,
    and :class:`OriginatingProposalLookupError` are caught, counted, and skipped
    so one malformed proposal never aborts the batch (design § Activation). Any
    other exception propagates — it signals infrastructure failure, not a
    per-proposal contract violation.
    """
    evaluated = 0
    unevaluable_by_reason: dict[str, int] = {}
    skipped_idempotent = 0
    error_count = 0
    first_error: str | None = None

    for entry, detail, replay_kind in iter_pending_replay_proposals(
        session, as_of=as_of, since=since, config=config
    ):
        try:
            record = replay_proposal(
                session,
                entry,
                detail,
                replay_kind,
                config=config,
                paper_harness_config=paper_harness_config,
                risk_free_rate=risk_free_rate,
                as_of=as_of,
            )
        except (
            ProposalHydrationError,
            PositionStateNotFoundError,
            OriginatingProposalLookupError,
        ) as exc:
            error_count += 1
            if first_error is None:
                first_error = str(exc)
            continue

        if record is None:
            skipped_idempotent += 1
        elif record.replay_status is ReplayStatus.UNEVALUABLE:
            assert record.unevaluable_reason is not None
            key = record.unevaluable_reason.value
            unevaluable_by_reason[key] = unevaluable_by_reason.get(key, 0) + 1
        else:
            evaluated += 1

    return ReplayBatchResult(
        evaluated=evaluated,
        unevaluable_by_reason=unevaluable_by_reason,
        skipped_idempotent=skipped_idempotent,
        error_count=error_count,
        first_error=first_error,
    )


# ---------------------------------------------------------------------------
# Routing — one helper per proposal shape, each returning a mapped record
# ---------------------------------------------------------------------------


def _route_and_map(
    session: Session,
    proposal: Recommendation | PositionAssessment | PendingOrderAssessment,
    *,
    ctx: MappingContext,
    inputs: _ReplayInputs,
    bars: tuple[OhlcvBar, ...],
    options_repo: SqlOptionsSnapshotRepository,
    proposal_timestamp: datetime,
) -> CounterfactualReplayRecord:
    """Dispatch an EVALUATED proposal to its replay path and map the result.

    ``proposal_timestamp`` is the as-of for the position-state read (the proposal
    time, not the batch ``as_of``).
    """
    envelope_id = ctx.envelope_id
    replay_kind = ctx.replay_kind
    harness = inputs.paper_harness_config

    if isinstance(proposal, Recommendation):
        if isinstance(proposal.instrument, InstrumentOption):
            option_result = replay_option_proposal(
                proposal,
                bars,
                iv_repo=options_repo,
                paper_harness_config=harness,
                risk_free_rate=inputs.risk_free_rate,
                adv_contracts=None,
                realized_volatility=None,
            )
            threshold = float(inputs.config.iv_lag_low_confidence_threshold_minutes)
            return record_from_option_result(option_result, ctx, iv_lag_threshold_minutes=threshold)
        equity_result = replay_equity_proposal(
            proposal,
            bars,
            paper_harness_config=harness,
            adv_shares=None,
            realized_volatility=None,
        )
        return record_from_equity_result(equity_result, ctx)

    if isinstance(proposal, PositionAssessment):
        state = load_position_state_at(
            session, position_id=proposal.position_id, as_of=proposal_timestamp
        )
        strategist_result = replay_strategist_proposal(
            proposal,
            state,
            bars,
            iv_repo=options_repo,
            paper_harness_config=harness,
            risk_free_rate=inputs.risk_free_rate,
            adv=None,
            realized_volatility=None,
        )
        return record_from_strategist_result(
            strategist_result, ctx, instrument_kind=_instrument_kind(state)
        )

    # PendingOrderAssessment
    state = load_position_state_at(
        session, position_id=proposal.position_id, as_of=proposal_timestamp
    )
    entry_proposal = reconstruct_pending_order_entry(_load_order(session, proposal), state)
    pending_result = replay_pending_order_proposal(
        proposal,
        entry_proposal,
        bars,
        iv_repo=options_repo,
        paper_harness_config=harness,
        risk_free_rate=inputs.risk_free_rate,
        adv=None,
        realized_volatility=None,
    )
    if pending_result.unevaluable_reason is not None:
        return write_unevaluable_record(
            session, envelope_id, replay_kind, pending_result.unevaluable_reason
        )
    assert pending_result.action_result is not None
    return record_from_strategist_result(
        pending_result.action_result, ctx, instrument_kind=_instrument_kind(state)
    )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _envelope_id(detail: PMDecisionDetail) -> EnvelopeId:
    from alphamind._kernel.ids import EnvelopeId

    return EnvelopeId(detail.envelope_id)


def _already_recorded(session: Session, envelope_id: EnvelopeId, replay_kind: ReplayKind) -> bool:
    existing = load_counterfactual_replays_for_envelope(session, envelope_id)
    return any(record.replay_kind is replay_kind for record in existing)


def _repo_key(proposal: Recommendation | PositionAssessment | PendingOrderAssessment) -> str:
    """The bar / corporate-action repo key — underlying ticker, or position_id.

    A ``PendingOrderAssessment`` carries no underlying; the SQL repos resolve its
    ``position_id`` to the underlying before querying (story 08 §1).
    """
    if isinstance(proposal, Recommendation):
        return proposal.underlying
    if isinstance(proposal, PositionAssessment):
        return proposal.underlying
    return proposal.position_id


def _load_order(session: Session, proposal: PendingOrderAssessment) -> OrderRecord:
    row = session.execute(
        select(OrderRow).where(OrderRow.order_id == str(proposal.order_id))
    ).scalar_one_or_none()
    if row is None:
        msg = f"pending-order assessment references missing order_id={proposal.order_id!r}"
        raise OriginatingProposalLookupError(msg)
    return order_row_to_record(row)


def _instrument_kind(state: PositionStateSnapshot) -> Literal["equity", "option"]:
    """``"option"`` when the position is a single-leg option, else ``"equity"``."""
    return "option" if isinstance(state.details, OptionsPositionDetails) else "equity"


def _bar_coverage_complete(bars: tuple[OhlcvBar, ...]) -> bool:
    """``True`` iff the bars form a contiguous 15-minute run (no gaps > one bar).

    A gap between consecutive ``period_start`` values larger than one bar
    interval signals a hole the simulator walked across — the confidence
    classifier demotes on it.
    """
    if not bars:
        return False
    for prev, nxt in pairwise(bars):
        gap = (nxt.period_start - prev.period_start).total_seconds()
        if gap > _BAR_INTERVAL_SECONDS * 1.5:
            return False
    return True
