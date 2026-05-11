"""``SqlPortfolioStateRepository`` — Protocol implementation backed by SQLAlchemy (story 06).

Implements every method of
:class:`alphamind.portfolio_state.repository.PortfolioStateRepository`
against the SQL tables shipped in stories 02b and 04a-04e:

* Tier 1 methods read directly from per-entity tables and decode through
  the existing per-table codecs in
  :mod:`alphamind.execution.state_persistence.tables`.
* Tier 2 methods delegate to the four ``activity_log`` query helpers
  shipped in story 03 (see :mod:`...repository.activity_log_queries`).
* Tier 3 methods follow the per-field strategy pre-resolved in
  :issue:`ALP-119`: drawdown from the singleton table, P/L inputs computed
  on-the-fly via SQL aggregation over CLOSED positions, thesis quality
  aggregates as an empty default placeholder, active risk parameters via
  the injected callable provider.

Snapshot isolation enforcement: ``get_current_invocation_metadata``
raises :class:`RepositoryConsistencyError` when the bound invocation row
shows ``phase1_completed_at IS NULL`` — covering the happy path required
by this story; story 07 + 08 verify the full six-step ordering.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.execution.regt_margin_attribution.aggregates import RegTExcessAggregates
from alphamind.execution.state_persistence.config import StatePersistenceConfig
from alphamind.execution.state_persistence.repository.activity_log_queries import (
    read_intra_invocation_changelog,
    read_position_modification_trail,
    read_recent_pm_decision_log,
)
from alphamind.execution.state_persistence.tables.bracket_legs import BracketLegRow
from alphamind.execution.state_persistence.tables.brackets import BracketRow
from alphamind.execution.state_persistence.tables.brackets_codec import (
    rows_to_record as bracket_rows_to_record,
)
from alphamind.execution.state_persistence.tables.cash_ledger import (
    CASH_LEDGER_SINGLETON_ID,
    CashLedgerRow,
)
from alphamind.execution.state_persistence.tables.cash_ledger_codec import (
    cash_ledger_record_from_row,
)
from alphamind.execution.state_persistence.tables.drawdown_state import (
    DRAWDOWN_STATE_SINGLETON_ID,
    DrawdownStateRow,
)
from alphamind.execution.state_persistence.tables.drawdown_state_codec import (
    drawdown_state_record_from_row,
)
from alphamind.execution.state_persistence.tables.fill_records import FillRecordRow
from alphamind.execution.state_persistence.tables.invocations import InvocationRow
from alphamind.execution.state_persistence.tables.orders import OrderRow
from alphamind.execution.state_persistence.tables.orders_codec import (
    row_to_record as order_row_to_record,
)
from alphamind.execution.state_persistence.tables.positions import PositionRow
from alphamind.execution.state_persistence.tables.positions_codec import (
    row_to_record as position_row_to_record,
)
from alphamind.execution.state_persistence.tables.theses import ThesisRow
from alphamind.execution.state_persistence.tables.theses_codec import (
    rows_to_record as thesis_rows_to_record,
)
from alphamind.execution.state_persistence.tables.thesis_components import (
    ThesisComponentRow,
)
from alphamind.execution.state_persistence.write_paths.records import (
    FillProcessingStatus,
)
from alphamind.portfolio_state.aggregates.risk_parameters import ActiveRiskParameterSet
from alphamind.portfolio_state.aggregates.thesis_quality import ThesisQualityAggregate
from alphamind.portfolio_state.records.activity_log import ActivityLogEntry
from alphamind.portfolio_state.records.capital import (
    CashLedger,
    DrawdownState,
    RiskBudgetConsumption,
)
from alphamind.portfolio_state.records.orders import (
    BracketRecord,
    OrderRecord,
    OrderStatus,
)
from alphamind.portfolio_state.records.positions import (
    PositionRecord,
    PositionStatus,
)
from alphamind.portfolio_state.records.theses import (
    RecentThesisResolution,
    ThesisRecord,
    ThesisRecordStatus,
)
from alphamind.portfolio_state.repository import (
    CurrentInvocationMetadata,
    PortfolioPnLInputs,
    PriorInvocationContext,
    RepositoryConsistencyError,
)
from alphamind.risk_guardrails.guardrail_evaluation.types import RiskZone

_PENDING_ORDER_STATUSES = (OrderStatus.PENDING.value, OrderStatus.PARTIALLY_FILLED.value)


def _empty_thesis_quality_aggregate(now: datetime) -> ThesisQualityAggregate:
    """Empty placeholder per ALP-119 Pre-resolved decision (D).

    The thesis-quality computation lands in a follow-up feedback-loop story;
    this story serves an empty default so the snapshot assembler can produce
    a valid ``PortfolioStateSnapshot`` without aggregate data.
    """
    return ThesisQualityAggregate.model_validate(
        {
            "as_of_timestamp": now,
            "resolution_counts_by_window": (),
            "duration_stats_by_window": (),
            "invalidation_timing_stats_by_window": (),
            "signal_hit_rates": (),
            "signal_to_thesis_conversions": (),
            "conviction_calibration": (),
            "conviction_sizing_deviation_by_window": (),
            "performance_attribution": (),
            "alpha_beta_decomposition_by_window": (),
        }
    )


class SqlPortfolioStateRepository:
    """SQLAlchemy-backed implementation of ``PortfolioStateRepository``.

    Reads each Protocol method via a fresh ``AsyncSession`` from the supplied
    factory; the assembler reads each method exactly once per snapshot, so
    no in-repo caching is needed.

    Tier 3 strategy per :issue:`ALP-119` Pre-resolved decision (D):

    * ``drawdown_state`` — singleton table read.
    * ``portfolio_pnl_inputs`` — on-the-fly SQL aggregation over CLOSED
      positions (raw realized P/L over rolling windows). The richer
      win-rate / profit-factor inputs default to ``None`` until a
      follow-up feedback-loop story populates them.
    * ``thesis_quality_aggregates`` — empty placeholder.
    * ``active_risk_parameters`` — supplied via the zero-arg
      ``active_risk_parameters_provider`` callable injected at construction.
    * ``risk_budget_consumption`` — zero-valued passthrough; the actual
      computation runs in story 07's snapshot-assembly path.
    * ``prior_active_risk_parameters`` — supplied via
      ``prior_active_risk_parameters_provider`` keyed by the prior
      invocation's ``resolved_config_snapshot_path``.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        invocation_id: str,
        active_risk_parameters_provider: Callable[[], Awaitable[ActiveRiskParameterSet]],
        prior_active_risk_parameters_provider: Callable[[str], Awaitable[ActiveRiskParameterSet]],
        config: StatePersistenceConfig,
    ) -> None:
        self._session_factory = session_factory
        self._invocation_id = invocation_id
        self._active_risk_parameters_provider = active_risk_parameters_provider
        self._prior_active_risk_parameters_provider = prior_active_risk_parameters_provider
        self._config = config

    # ------------------------------------------------------------------
    # Tier 1 — direct table reads
    # ------------------------------------------------------------------

    async def get_open_positions(self) -> tuple[PositionRecord, ...]:
        async with self._session_factory() as session:
            stmt = (
                select(PositionRow)
                .where(PositionRow.status == PositionStatus.OPEN.value)
                .order_by(PositionRow.position_id.asc())
            )
            result = await session.execute(stmt)
            return tuple(position_row_to_record(row) for row in result.scalars())

    async def get_pending_positions(self) -> tuple[PositionRecord, ...]:
        async with self._session_factory() as session:
            stmt = (
                select(PositionRow)
                .where(PositionRow.status == PositionStatus.PENDING.value)
                .order_by(PositionRow.position_id.asc())
            )
            result = await session.execute(stmt)
            return tuple(position_row_to_record(row) for row in result.scalars())

    async def get_active_theses(self) -> tuple[ThesisRecord, ...]:
        async with self._session_factory() as session:
            return await self._read_theses_by_status(session, ThesisRecordStatus.ACTIVE)

    async def get_recent_thesis_resolutions(
        self, *, lookback_trading_days: int
    ) -> tuple[RecentThesisResolution, ...]:
        # ``lookback_trading_days`` is accepted for Protocol parity; the
        # trading-calendar primitive that would enable a date-windowed SQL
        # filter lands in the data layer. The resolved-thesis registry is
        # bounded by Phase 1 retention so returning all resolved theses
        # is correct for the v1 snapshot.
        del lookback_trading_days
        async with self._session_factory() as session:
            resolved = await self._read_theses_by_status(session, ThesisRecordStatus.RESOLVED)
            return tuple(self._to_recent_resolution(t) for t in resolved)

    async def get_cash_ledger(self) -> CashLedger:
        async with self._session_factory() as session:
            row = await session.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
            if row is None:
                msg = "cash_ledger singleton row is missing — run Phase 1 to seed it"
                raise RepositoryConsistencyError(msg)
            # Computed fields (cash_pct / true_deployable / RegT excess) are
            # supplied as zeros here; the assembler recomputes them via
            # ``compute_cash_pct_of_portfolio`` /
            # ``compute_true_deployable_capital_usd`` during enrichment.
            return cash_ledger_record_from_row(
                row,
                cash_pct_of_portfolio=0.0,
                true_deployable_capital_usd=0.0,
                regt_excess_trailing_30d_usd=0.0,
                regt_excess_trailing_90d_usd=0.0,
                regt_excess_lifetime_usd=0.0,
            )

    async def get_regt_excess_aggregates(self, now: datetime) -> RegTExcessAggregates:
        """Sum ``regt_excess_over_pm`` across fill-record metadata.

        Three calendar-day-anchored windows in one round trip:

        * ``trailing_30d_usd`` — fills with ``processing_timestamp >= now - 30d``.
        * ``trailing_90d_usd`` — fills with ``processing_timestamp >= now - 90d``.
        * ``lifetime_usd`` — every processed fill with non-null
          ``regt_attribution_json``.

        Per ``regt-margin-attribution.md § Aggregation and delivery``: fills
        predating the attribution module (``regt_attribution_json IS NULL``)
        contribute zero to every window. Defense-in-depth: rows with
        ``processing_status != 'processed'`` are also excluded.

        ``now`` must be timezone-aware (UTC convention). Naive datetimes
        produce timezone-implicit ISO strings whose lex order does not match
        the absolute order of stored UTC timestamps, so the window cutoffs
        would silently misclassify rows.

        Implementation uses ``json_extract``, a SQLite-native function.
        AlphaMind is SQLite-only per CLAUDE.md; a Postgres migration would
        need to rewrite this query.
        """
        if now.tzinfo is None:
            msg = "now must be timezone-aware (UTC convention)"
            raise ValueError(msg)
        cutoff_30d = (now - timedelta(days=30)).isoformat()
        cutoff_90d = (now - timedelta(days=90)).isoformat()
        excess_expr = func.json_extract(
            FillRecordRow.regt_attribution_json, "$.regt_excess_over_pm"
        )
        sum_30d = func.coalesce(
            func.sum(
                case((FillRecordRow.processing_timestamp >= cutoff_30d, excess_expr), else_=0)
            ),
            0.0,
        )
        sum_90d = func.coalesce(
            func.sum(
                case((FillRecordRow.processing_timestamp >= cutoff_90d, excess_expr), else_=0)
            ),
            0.0,
        )
        sum_lifetime = func.coalesce(func.sum(excess_expr), 0.0)
        stmt = select(sum_30d, sum_90d, sum_lifetime).where(
            FillRecordRow.processing_status == FillProcessingStatus.PROCESSED.value,
            FillRecordRow.regt_attribution_json.is_not(None),
        )
        async with self._session_factory() as session:
            row = (await session.execute(stmt)).one()
        return RegTExcessAggregates(
            trailing_30d_usd=float(row[0]),
            trailing_90d_usd=float(row[1]),
            lifetime_usd=float(row[2]),
        )

    async def get_pending_orders(self) -> tuple[OrderRecord, ...]:
        async with self._session_factory() as session:
            stmt = (
                select(OrderRow)
                .where(OrderRow.status.in_(_PENDING_ORDER_STATUSES))
                .order_by(OrderRow.submission_timestamp.asc(), OrderRow.order_id.asc())
            )
            result = await session.execute(stmt)
            return tuple(order_row_to_record(row) for row in result.scalars())

    async def get_brackets_for_positions(
        self, *, position_ids: tuple[str, ...]
    ) -> tuple[BracketRecord, ...]:
        if not position_ids:
            return ()
        async with self._session_factory() as session:
            bracket_stmt = (
                select(BracketRow)
                .where(BracketRow.position_id.in_(position_ids))
                .order_by(BracketRow.bracket_id.asc())
            )
            bracket_rows = list((await session.execute(bracket_stmt)).scalars())
            if not bracket_rows:
                return ()
            bracket_ids = [b.bracket_id for b in bracket_rows]
            leg_stmt = (
                select(BracketLegRow)
                .where(BracketLegRow.bracket_id.in_(bracket_ids))
                .order_by(BracketLegRow.bracket_id.asc(), BracketLegRow.leg_index.asc())
            )
            legs_by_bracket: dict[str, list[BracketLegRow]] = {bid: [] for bid in bracket_ids}
            for leg in (await session.execute(leg_stmt)).scalars():
                legs_by_bracket[leg.bracket_id].append(leg)
        return tuple(
            bracket_rows_to_record(b, tuple(legs_by_bracket[b.bracket_id])) for b in bracket_rows
        )

    # ------------------------------------------------------------------
    # Tier 2 — activity log + invocations
    # ------------------------------------------------------------------

    async def get_intra_invocation_changelog(
        self, *, invocation_id: str
    ) -> tuple[ActivityLogEntry, ...]:
        async with self._session_factory() as session:
            return await read_intra_invocation_changelog(session, invocation_id)

    async def get_recent_pm_decision_log(
        self, *, sliding_window_invocations: int
    ) -> tuple[ActivityLogEntry, ...]:
        async with self._session_factory() as session:
            return await read_recent_pm_decision_log(session, sliding_window_invocations)

    async def get_position_modification_trail(
        self, *, position_ids: tuple[str, ...]
    ) -> dict[str, tuple[ActivityLogEntry, ...]]:
        if not position_ids:
            return {}
        async with self._session_factory() as session:
            trail = await read_position_modification_trail(session, list(position_ids))
        # Match the StubPortfolioStateRepository contract: omit position_ids
        # whose trail is empty so consumers iterate only the populated keys.
        return {pid: entries for pid, entries in trail.items() if entries}

    async def get_current_invocation_metadata(self) -> CurrentInvocationMetadata:
        async with self._session_factory() as session:
            row = await session.get(InvocationRow, self._invocation_id)
            if row is None:
                msg = (
                    f"invocations row {self._invocation_id!r} is missing — "
                    "insert_invocation_record should have committed it before snapshot read"
                )
                raise RepositoryConsistencyError(msg)
            if row.phase1_completed_at is None:
                msg = (
                    f"invocations row {self._invocation_id!r} has phase1_completed_at "
                    "IS NULL — snapshot read attempted before Phase 1 commit"
                )
                raise RepositoryConsistencyError(msg)
            return CurrentInvocationMetadata(
                invocation_id=row.invocation_id,
                phase1_committed_at=datetime.fromisoformat(row.phase1_completed_at),
                # ``pipeline_invocation_started_at`` is left ``None`` at snapshot
                # assembly time per the snapshot design (see archive ref in
                # ``PortfolioStateSnapshot``). The field is meant to be populated
                # later by the pipeline runtime that consumes the snapshot —
                # populating it from the row's ``start_at`` (which predates the
                # snapshot assembly) violates the assembled-snapshot ordering
                # invariant ``pipeline_invocation_started_at >= snapshot_assembled_at``.
                pipeline_invocation_started_at=None,
            )

    async def get_prior_invocation_context(self) -> PriorInvocationContext:
        async with self._session_factory() as session:
            stmt = (
                select(InvocationRow)
                .where(
                    InvocationRow.invocation_id != self._invocation_id,
                    InvocationRow.start_at
                    < (
                        select(InvocationRow.start_at)
                        .where(InvocationRow.invocation_id == self._invocation_id)
                        .scalar_subquery()
                    ),
                )
                .order_by(InvocationRow.start_at.desc())
                .limit(1)
            )
            row = (await session.execute(stmt)).scalar_one_or_none()

        if row is None:
            return PriorInvocationContext(
                prior_invocation_id=None,
                prior_active_risk_parameters=None,
                prior_phase1_committed_at=None,
            )

        prior_params = await self._prior_active_risk_parameters_provider(
            row.resolved_config_snapshot_path
        )
        prior_phase1_at = (
            None
            if row.phase1_completed_at is None
            else datetime.fromisoformat(row.phase1_completed_at)
        )
        return PriorInvocationContext(
            prior_invocation_id=row.invocation_id,
            prior_active_risk_parameters=prior_params,
            prior_phase1_committed_at=prior_phase1_at,
        )

    # ------------------------------------------------------------------
    # Tier 3 — derived/aggregate
    # ------------------------------------------------------------------

    async def get_drawdown_state(self) -> DrawdownState:
        async with self._session_factory() as session:
            row = await session.get(DrawdownStateRow, DRAWDOWN_STATE_SINGLETON_ID)
            if row is None:
                msg = "drawdown_state singleton row is missing — run Phase 1 to seed it"
                raise RepositoryConsistencyError(msg)
            # Computed read-time fields (intraday DD, zone, tier) carry
            # neutral defaults; the snapshot assembler enriches them via
            # ``compute_drawdown_by_source_pct`` and the risk-zone classifier
            # at delivery time.
            return drawdown_state_record_from_row(
                row,
                intraday_drawdown_pct=0.0,
                daily_zone=RiskZone.NORMAL,
                cumulative_zone=RiskZone.NORMAL,
                cumulative_tier=None,
            )

    async def get_portfolio_pnl_inputs(self) -> PortfolioPnLInputs:
        async with self._session_factory() as session:
            stmt = select(PositionRow.realized_pnl_to_date_usd).where(
                PositionRow.status == PositionStatus.CLOSED.value
            )
            realized_pnls = (await session.execute(stmt)).scalars().all()
        return _aggregate_pnl_inputs(realized_pnls)

    async def get_thesis_quality_aggregates(self) -> ThesisQualityAggregate:
        return _empty_thesis_quality_aggregate(datetime.now(UTC))

    async def get_active_risk_parameters(self) -> ActiveRiskParameterSet:
        return await self._active_risk_parameters_provider()

    async def get_risk_budget_consumption(self) -> RiskBudgetConsumption:
        # Zero-valued passthrough; the assembler computes actual consumption
        # by combining position + cash + active_risk_parameters in story 07.
        return RiskBudgetConsumption(entries=())

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _read_theses_by_status(
        self, session: AsyncSession, status: ThesisRecordStatus
    ) -> tuple[ThesisRecord, ...]:
        thesis_stmt = (
            select(ThesisRow)
            .where(ThesisRow.status == status.value)
            .order_by(ThesisRow.thesis_id.asc())
        )
        thesis_rows = list((await session.execute(thesis_stmt)).scalars())
        if not thesis_rows:
            return ()
        thesis_ids = [t.thesis_id for t in thesis_rows]
        comp_stmt = select(ThesisComponentRow).where(ThesisComponentRow.thesis_id.in_(thesis_ids))
        components_by_thesis: dict[str, list[ThesisComponentRow]] = {tid: [] for tid in thesis_ids}
        for comp in (await session.execute(comp_stmt)).scalars():
            components_by_thesis[comp.thesis_id].append(comp)
        return tuple(
            thesis_rows_to_record(t, tuple(components_by_thesis[t.thesis_id])) for t in thesis_rows
        )

    @staticmethod
    def _to_recent_resolution(thesis: ThesisRecord) -> RecentThesisResolution:
        # Resolution-required fields are guaranteed non-None for RESOLVED status
        # by ThesisRecord's validators.
        assert thesis.resolution_category is not None
        assert thesis.resolution_pnl_usd is not None
        assert thesis.resolution_timestamp is not None
        active_duration_hours = (
            thesis.resolution_timestamp - thesis.generation_timestamp
        ).total_seconds() / 3600.0
        component_outcomes = tuple(
            (c.component_id, c.resolution_outcome)
            for c in thesis.components
            if c.resolution_outcome is not None
        )
        return RecentThesisResolution(
            thesis_id=thesis.thesis_id,
            position_id=thesis.position_id,
            resolution_category=thesis.resolution_category,
            component_outcomes=component_outcomes,
            resolution_pnl_usd=thesis.resolution_pnl_usd,
            active_duration_hours=active_duration_hours,
            expected_duration_hours=thesis.time_expectation_hours,
            signal_post_mortem=None,
        )


def _aggregate_pnl_inputs(realized_pnls: Sequence[float | None]) -> PortfolioPnLInputs:
    """Aggregate raw realized P/L from CLOSED-position rows.

    Computes the cumulative total cleanly mappable from per-position
    ``realized_pnl_to_date_usd``. The win-rate / profit-factor / per-window
    splits remain ``None`` until the feedback-loop story populates a richer
    aggregation surface.
    """
    cumulative = sum(pnl for pnl in realized_pnls if pnl is not None)
    return PortfolioPnLInputs(
        daily_realized_pnl_usd=0.0,
        cumulative_realized_pnl_usd=float(cumulative),
        rolling_realized_pnl={"1d": 0.0, "3d": 0.0, "5d": 0.0, "20d": 0.0},
        win_rate_pct=None,
        average_win_size_usd=None,
        average_loss_size_usd=None,
        profit_factor=None,
    )


__all__ = ["SqlPortfolioStateRepository"]
