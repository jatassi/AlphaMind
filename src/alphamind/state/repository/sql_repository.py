"""``SqlPortfolioStateRepository`` — Protocol implementation backed by SQLAlchemy (story 06).

Implements every method of
:class:`alphamind.portfolio_state.repository.PortfolioStateRepository`
against the SQL tables shipped in stories 02b and 04a-04e:

* Tier 1 methods read directly from per-entity tables and decode through
  the existing per-table codecs in
  :mod:`alphamind.state.tables`.
* Tier 2 methods delegate to the four ``activity_log`` query helpers
  shipped in story 03 (see :mod:`...repository.activity_log_queries`).
* Tier 3 methods follow the per-field strategy pre-resolved in
  :issue:`ALP-119`: drawdown from the singleton table, P/L inputs computed
  on-the-fly via SQL aggregation over CLOSED positions, thesis quality
  aggregates as an empty default placeholder, active risk parameters via
  the injected callable provider.

Snapshot isolation enforcement: ``get_current_invocation_metadata``
falls back to the most-recently-completed pipeline invocation when the
bound row is missing or has ``fill_collection_completed_at IS NULL`` (e.g. scheduler
paused mid-invocation), so the breach_loop can continue ticking.  Only
raises :class:`RepositoryConsistencyError` when no completed pipeline
invocation exists at all.  Story 07 + 08 verify the full six-step ordering.

ALP-454 Pre-resolved decision (C): per the audit, the Protocol surface
is synchronous (SQLite is the persistence engine; aiosqlite already
serialises through a single worker thread). This implementation derives
a sync ``sessionmaker`` from the supplied ``async_sessionmaker``'s
engine URL (stripping ``+aiosqlite`` to get the sync ``sqlite`` driver)
so callers can keep their existing async-engine wiring for writes while
the repository reads block synchronously. WAL mode permits the
concurrent reader. The provider callables are likewise sync.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, create_engine, event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, sessionmaker

from alphamind._kernel.regime import RiskZone
from alphamind.execution.regt_margin_attribution.aggregates import RegTExcessAggregates
from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
from alphamind.portfolio_state.aggregates.risk_parameters import ActiveRiskParameterSet
from alphamind.portfolio_state.aggregates.thesis_quality import ThesisQualityAggregate
from alphamind.portfolio_state.events.activity_log import ActivityLogEntry, EventType
from alphamind.portfolio_state.records.cash import CashLedger
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
from alphamind.state.config import StatePersistenceConfig
from alphamind.state.invocation_context.activity_log import (
    activity_log_entry_from_row,
)
from alphamind.state.records import (
    FillProcessingStatus,
)
from alphamind.state.tables.activity_log import ActivityLogRow
from alphamind.state.tables.bracket_legs import BracketLegRow
from alphamind.state.tables.brackets import BracketRow
from alphamind.state.tables.brackets_codec import (
    rows_to_records_isolated as bracket_rows_to_records_isolated,
)
from alphamind.state.tables.cash_ledger import (
    CASH_LEDGER_SINGLETON_ID,
    CashLedgerRow,
)
from alphamind.state.tables.cash_ledger_codec import (
    cash_ledger_record_from_row,
)
from alphamind.state.tables.drawdown_state import (
    DRAWDOWN_STATE_SINGLETON_ID,
    DrawdownStateRow,
)
from alphamind.state.tables.drawdown_state_codec import (
    drawdown_state_record_from_row,
)
from alphamind.state.tables.fill_records import FillRecordRow
from alphamind.state.tables.invocations import InvocationRow
from alphamind.state.tables.orders import OrderRow
from alphamind.state.tables.orders_codec import (
    row_to_record as order_row_to_record,
)
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.positions_codec import (
    row_to_record as position_row_to_record,
)
from alphamind.state.tables.theses import ThesisRow
from alphamind.state.tables.theses_codec import (
    rows_to_record as thesis_rows_to_record,
)
from alphamind.state.tables.thesis_components import (
    ThesisComponentRow,
)

# NOTE: the activity_log query helpers in ``activity_log_queries`` still take
# the async session because their other consumer (``config_change.py``) runs
# inside the fill-collection ``InvocationContext`` write transaction. The SQL repo
# inlines sync equivalents of those queries on its own session below.

_PENDING_ORDER_STATUSES = (OrderStatus.PENDING.value, OrderStatus.PARTIALLY_FILLED.value)
log = logging.getLogger(__name__)


def _apply_pragmas(dbapi_connection: object, _connection_record: object) -> None:
    """Mirror :func:`alphamind.persistence.session._apply_pragmas`.

    Each fresh DBAPI connection opened by the sync engine applies the same
    four pragmas the async engine applies. Duplicated here rather than
    imported because ``persistence.session`` lives below the execution layer
    in the import-linter contract.
    """
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=60000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def _build_sync_session_factory(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> sessionmaker[Session]:
    """Build a sync ``sessionmaker`` from the async session factory's engine URL.

    Strips the ``+aiosqlite`` async driver suffix off the URL so the resulting
    engine uses sync ``sqlite``. The underlying SQLite file is shared with the
    async engine; WAL mode permits the concurrent reader. Pragmas mirror
    :func:`alphamind.persistence.session.make_engine`.
    """
    async_engine = async_session_factory.kw["bind"]
    url = async_engine.url
    sync_url = url.set(drivername=url.drivername.replace("+aiosqlite", ""))
    sync_engine = create_engine(sync_url)
    event.listen(sync_engine, "connect", _apply_pragmas)
    return sessionmaker(bind=sync_engine, expire_on_commit=False)


def _empty_thesis_quality_aggregate(now: datetime) -> ThesisQualityAggregate:
    """Empty placeholder per ALP-119 Pre-resolved decision (D).

    The thesis-quality computation lands in a follow-up feedback-loop story;
    this story serves an empty default so the snapshot assembler can produce
    a valid ``PortfolioStateSnapshot`` without aggregate data.
    """
    return ThesisQualityAggregate(
        as_of_timestamp=now,
        resolution_counts_by_window=(),
        duration_stats_by_window=(),
        invalidation_timing_stats_by_window=(),
        signal_hit_rates=(),
        signal_to_thesis_conversions=(),
        conviction_calibration=(),
        conviction_sizing_deviation_by_window=(),
        performance_attribution=(),
        alpha_beta_decomposition_by_window=(),
    )


class SqlPortfolioStateRepository:
    """SQLAlchemy-backed implementation of ``PortfolioStateRepository``.

    Reads each Protocol method via a fresh sync ``Session`` derived from
    the supplied async session factory's underlying engine; the assembler
    reads each method exactly once per snapshot, so no in-repo caching is
    needed.

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
        active_risk_parameters_provider: Callable[[], ActiveRiskParameterSet],
        prior_active_risk_parameters_provider: Callable[[str], ActiveRiskParameterSet],
        config: StatePersistenceConfig,
    ) -> None:
        # Build a sync ``sessionmaker`` from the async session factory's engine
        # URL. The async sessionmaker is preserved as a parameter so existing
        # upstream wiring (which threads ``async_sessionmaker[AsyncSession]``
        # through command_execution_dispatch, invocation_context, etc.) stays unchanged.
        # ALP-454 (C).
        self._sync_session_factory = _build_sync_session_factory(session_factory)
        self._invocation_id = invocation_id
        self._active_risk_parameters_provider = active_risk_parameters_provider
        self._prior_active_risk_parameters_provider = prior_active_risk_parameters_provider
        self._config = config

    # ------------------------------------------------------------------
    # Tier 1 — direct table reads
    # ------------------------------------------------------------------

    def get_open_positions(self) -> tuple[PositionRecord, ...]:
        with self._sync_session_factory() as session:
            stmt = (
                select(PositionRow)
                .where(PositionRow.status == PositionStatus.OPEN.value)
                .order_by(PositionRow.position_id.asc())
            )
            result = session.execute(stmt)
            return tuple(position_row_to_record(row) for row in result.scalars())

    def get_pending_positions(self) -> tuple[PositionRecord, ...]:
        with self._sync_session_factory() as session:
            stmt = (
                select(PositionRow)
                .where(PositionRow.status == PositionStatus.PENDING.value)
                .order_by(PositionRow.position_id.asc())
            )
            result = session.execute(stmt)
            return tuple(position_row_to_record(row) for row in result.scalars())

    def get_active_theses(self) -> tuple[ThesisRecord, ...]:
        with self._sync_session_factory() as session:
            return self._read_theses_by_status(session, ThesisRecordStatus.ACTIVE)

    def get_recent_thesis_resolutions(
        self, *, lookback_trading_days: int
    ) -> tuple[RecentThesisResolution, ...]:
        # ``lookback_trading_days`` is accepted for Protocol parity; the
        # trading-calendar primitive that would enable a date-windowed SQL
        # filter lands in the data layer. The resolved-thesis registry is
        # bounded by fill-collection retention so returning all resolved theses
        # is correct for the v1 snapshot.
        del lookback_trading_days
        with self._sync_session_factory() as session:
            resolved = self._read_theses_by_status(session, ThesisRecordStatus.RESOLVED)
            return tuple(self._to_recent_resolution(t) for t in resolved)

    def get_cash_ledger(self) -> CashLedger:
        with self._sync_session_factory() as session:
            row = session.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
            if row is None:
                msg = "cash_ledger singleton row is missing — run fill-collection to seed it"
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

    def get_regt_excess_aggregates(self, now: datetime) -> RegTExcessAggregates:
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
        with self._sync_session_factory() as session:
            row = session.execute(stmt).one()
        return RegTExcessAggregates(
            trailing_30d_usd=float(row[0]),
            trailing_90d_usd=float(row[1]),
            lifetime_usd=float(row[2]),
        )

    def get_pending_orders(self) -> tuple[OrderRecord, ...]:
        with self._sync_session_factory() as session:
            stmt = (
                select(OrderRow)
                .where(OrderRow.status.in_(_PENDING_ORDER_STATUSES))
                .order_by(OrderRow.submission_timestamp.asc(), OrderRow.order_id.asc())
            )
            result = session.execute(stmt)
            return tuple(order_row_to_record(row) for row in result.scalars())

    def get_brackets_for_positions(
        self, *, position_ids: tuple[str, ...]
    ) -> tuple[BracketRecord, ...]:
        if not position_ids:
            return ()
        with self._sync_session_factory() as session:
            bracket_stmt = (
                select(BracketRow)
                .where(BracketRow.position_id.in_(position_ids))
                .order_by(BracketRow.bracket_id.asc())
            )
            bracket_rows = list(session.execute(bracket_stmt).scalars())
            if not bracket_rows:
                return ()
            bracket_ids = [b.bracket_id for b in bracket_rows]
            leg_stmt = (
                select(BracketLegRow)
                .where(BracketLegRow.bracket_id.in_(bracket_ids))
                .order_by(BracketLegRow.bracket_id.asc(), BracketLegRow.leg_index.asc())
            )
            legs_by_bracket: dict[str, list[BracketLegRow]] = {bid: [] for bid in bracket_ids}
            for leg in session.execute(leg_stmt).scalars():
                legs_by_bracket[leg.bracket_id].append(leg)
        # Per-bracket isolation (ALP-732 Gap 1) — one unreadable bracket is
        # skipped + surfaced rather than poisoning the whole batch and aborting
        # the breach-loop tick. Shared with the bracket-stop watcher's loader.
        return bracket_rows_to_records_isolated(bracket_rows, legs_by_bracket)

    # ------------------------------------------------------------------
    # Tier 2 — activity log + invocations
    # ------------------------------------------------------------------

    def get_intra_invocation_changelog(self, *, invocation_id: str) -> tuple[ActivityLogEntry, ...]:
        with self._sync_session_factory() as session:
            stmt = (
                select(ActivityLogRow)
                .where(ActivityLogRow.invocation_id == invocation_id)
                .order_by(ActivityLogRow.entry_at.asc(), ActivityLogRow.entry_id.asc())
            )
            return tuple(
                activity_log_entry_from_row(row) for row in session.execute(stmt).scalars()
            )

    def get_recent_pm_decision_log(
        self, *, sliding_window_invocations: int
    ) -> tuple[ActivityLogEntry, ...]:
        recent_invocations_subq = (
            select(InvocationRow.invocation_id)
            .order_by(InvocationRow.start_at.desc())
            .limit(sliding_window_invocations)
            .subquery()
        )
        stmt = (
            select(ActivityLogRow)
            .where(
                ActivityLogRow.event_type == EventType.PM_DECISION.value,
                ActivityLogRow.invocation_id.in_(select(recent_invocations_subq.c.invocation_id)),
            )
            .order_by(ActivityLogRow.entry_at.asc(), ActivityLogRow.entry_id.asc())
        )
        with self._sync_session_factory() as session:
            return tuple(
                activity_log_entry_from_row(row) for row in session.execute(stmt).scalars()
            )

    def get_position_modification_trail(
        self, *, position_ids: tuple[str, ...]
    ) -> dict[str, tuple[ActivityLogEntry, ...]]:
        if not position_ids:
            return {}
        stmt = (
            select(ActivityLogRow)
            .where(ActivityLogRow.position_id.in_(position_ids))
            .order_by(ActivityLogRow.entry_at.asc(), ActivityLogRow.entry_id.asc())
        )
        grouped: dict[str, list[ActivityLogEntry]] = {pid: [] for pid in position_ids}
        with self._sync_session_factory() as session:
            for row in session.execute(stmt).scalars():
                # row.position_id is non-None because the WHERE clause filtered
                # to the supplied list — narrow for the type checker.
                pid = row.position_id
                assert pid is not None
                grouped[pid].append(activity_log_entry_from_row(row))
        # Match the StubPortfolioStateRepository contract: omit position_ids
        # whose trail is empty so consumers iterate only the populated keys.
        return {pid: tuple(entries) for pid, entries in grouped.items() if entries}

    def get_current_invocation_metadata(self) -> CurrentInvocationMetadata:
        with self._sync_session_factory() as session:
            row = session.get(InvocationRow, self._invocation_id)
            # When the bound invocation is missing or paused before fill-collection
            # commit (fill_collection_completed_at IS NULL), the monitor must not go
            # DEGRADED — fall back to the most-recently-completed *pipeline* invocation
            # (resolved_config_snapshot_path != '' excludes maintenance ticks,
            # consistent with get_prior_invocation_context) so the breach_loop
            # can continue ticking with stale-but-valid state metadata.
            if row is None or row.fill_collection_completed_at is None:
                fallback = session.execute(
                    select(InvocationRow)
                    .where(
                        InvocationRow.fill_collection_completed_at.is_not(None),
                        InvocationRow.resolved_config_snapshot_path != "",
                    )
                    .order_by(InvocationRow.start_at.desc())
                    .limit(1)
                ).scalar_one_or_none()
                if fallback is None:
                    msg = (
                        f"invocations row {self._invocation_id!r} is missing or has "
                        "fill_collection_completed_at IS NULL and no completed invocation exists "
                        "to fall back to"
                    )
                    raise RepositoryConsistencyError(msg)
                log.warning(
                    "get_current_invocation_metadata: bound invocation %r is paused or missing "
                    "— ticking against prior completed invocation %r "
                    "(fill_collection_committed_at=%s). "
                    "Snapshot metadata is stale; risk evaluation continues.",
                    self._invocation_id,
                    fallback.invocation_id,
                    fallback.fill_collection_completed_at,
                )
                row = fallback
            # fill_collection_completed_at is not None: either the if-condition above
            # was False (bound row already had a non-NULL value) or the fallback WHERE
            # clause guaranteed it for the fallback row.
            assert row.fill_collection_completed_at is not None
            return CurrentInvocationMetadata(
                invocation_id=row.invocation_id,
                fill_collection_committed_at=datetime.fromisoformat(
                    row.fill_collection_completed_at
                ),
                # ``pipeline_invocation_started_at`` is left ``None`` at snapshot
                # assembly time per the snapshot design (see archive ref in
                # ``PortfolioStateSnapshot``). The field is meant to be populated
                # later by the pipeline runtime that consumes the snapshot —
                # populating it from the row's ``start_at`` (which predates the
                # snapshot assembly) violates the assembled-snapshot ordering
                # invariant ``pipeline_invocation_started_at >= snapshot_assembled_at``.
                pipeline_invocation_started_at=None,
            )

    def get_prior_invocation_context(self) -> PriorInvocationContext:
        with self._sync_session_factory() as session:
            stmt = (
                select(InvocationRow)
                .where(
                    InvocationRow.invocation_id != self._invocation_id,
                    # Skip pathless maintenance ticks (e.g. ``borrow_accrual``,
                    # ALP-754): they write a lineage row but never resolve a
                    # config, so their snapshot path is empty and they carry no
                    # regime-adaptation parameters to anchor hysteresis on. The
                    # prior must be a run that actually resolved a config.
                    InvocationRow.resolved_config_snapshot_path != "",
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
            row = session.execute(stmt).scalar_one_or_none()

        if row is None:
            return PriorInvocationContext(
                prior_invocation_id=None,
                prior_active_risk_parameters=None,
                prior_fill_collection_committed_at=None,
            )

        prior_params = self._prior_active_risk_parameters_provider(
            row.resolved_config_snapshot_path
        )
        prior_fill_collection_at = (
            None
            if row.fill_collection_completed_at is None
            else datetime.fromisoformat(row.fill_collection_completed_at)
        )
        return PriorInvocationContext(
            prior_invocation_id=row.invocation_id,
            prior_active_risk_parameters=prior_params,
            prior_fill_collection_committed_at=prior_fill_collection_at,
        )

    # ------------------------------------------------------------------
    # Tier 3 — derived/aggregate
    # ------------------------------------------------------------------

    def get_drawdown_state(self) -> DrawdownState:
        with self._sync_session_factory() as session:
            row = session.get(DrawdownStateRow, DRAWDOWN_STATE_SINGLETON_ID)
            if row is None:
                msg = "drawdown_state singleton row is missing — run fill-collection to seed it"
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

    def get_portfolio_pnl_inputs(self) -> PortfolioPnLInputs:
        """Portfolio-cumulative realized P&L, rolled up from the per-position field.

        Deliberately sums ``positions.realized_pnl_to_date_usd`` over CLOSED
        rows — the per-**position** grain — so positions with **no** thesis link
        (DVN / manual-trade / broker-fact-with-no-Intent) are included in the
        portfolio rollup. This is **not** the per-thesis ``thesis_pnl_ledger``
        (log-derived, ADR-0005 invariant 3): that ledger answers thesis-level
        attribution and structurally cannot cover non-thesis positions, so the
        portfolio aggregate reads the per-position field instead. The two are
        distinct grains, not a duplicated path — see
        ``docs/design/05-execution-layer/broker-boundary-redesign.md`` § 4
        invariant 3.
        """
        with self._sync_session_factory() as session:
            stmt = select(PositionRow.realized_pnl_to_date_usd).where(
                PositionRow.status == PositionStatus.CLOSED.value
            )
            realized_pnls = session.execute(stmt).scalars().all()
        return _aggregate_pnl_inputs(realized_pnls)

    def get_thesis_quality_aggregates(self) -> ThesisQualityAggregate:
        return _empty_thesis_quality_aggregate(datetime.now(UTC))

    def get_active_risk_parameters(self) -> ActiveRiskParameterSet:
        return self._active_risk_parameters_provider()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_theses_by_status(
        self, session: Session, status: ThesisRecordStatus
    ) -> tuple[ThesisRecord, ...]:
        thesis_stmt = (
            select(ThesisRow)
            .where(ThesisRow.status == status.value)
            .order_by(ThesisRow.thesis_id.asc())
        )
        thesis_rows = list(session.execute(thesis_stmt).scalars())
        if not thesis_rows:
            return ()
        thesis_ids = [t.thesis_id for t in thesis_rows]
        comp_stmt = select(ThesisComponentRow).where(ThesisComponentRow.thesis_id.in_(thesis_ids))
        components_by_thesis: dict[str, list[ThesisComponentRow]] = {tid: [] for tid in thesis_ids}
        for comp in session.execute(comp_stmt).scalars():
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

    Computes the cumulative total cleanly mappable from per-**position**
    ``realized_pnl_to_date_usd`` — the rollup spans every CLOSED position,
    including non-thesis ones, and is **not** derived from the per-thesis
    ``thesis_pnl_ledger`` (see :meth:`get_portfolio_pnl_inputs`). The win-rate /
    profit-factor / per-window splits remain ``None`` until the feedback-loop
    story populates a richer aggregation surface.
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
