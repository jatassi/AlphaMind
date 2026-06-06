"""Standalone end-to-end verify for the counterfactual replay engine (ALP-566).

The engine runs **out-of-pipeline** (CLI only), so ``scripts/verify_debug_e2e.py``
does not cover it. This script is the deterministic operator check after a code
change or a migration that touches ``counterfactual_replays``: it seeds a
controlled DB state of PM-decision rows + bar history + IV snapshots + (for the
strategist paths) position state, runs
:func:`~alphamind.execution.counterfactual_replay_engine.engine.replay_pending_proposals`
directly, and asserts each expected outcome against the returned
:class:`ReplayBatchResult` and the persisted
:class:`CounterfactualReplayRecord` set.

The seed is the *ground truth*: :func:`build_test_seed` inserts the eight
documented proposals and returns a :class:`ReplaySeed` carrying, per proposal, the
expected ``replay_status`` / ``unevaluable_reason`` / ``exit_leg`` and the two
analytically-derived P/L expectations. Because the engine driver passes
``adv_shares=None`` / ``realized_volatility=None``, the paper harness returns no
estimate and every slippage / fee is zero — so the equity TARGET_HIT P/L is
exactly ``(target - entry) * qty`` and the strategist CLOSE P/L is exactly
``(exit_open - basis) * qty``, both checked within a small tolerance.

The pure ``assert_*`` helpers take the loaded records (and the seed) and return a
failure message or ``None``, so each is unit-testable in isolation
(``tests/scripts/test_verify_counterfactual_replay_engine.py``). The full
end-to-end run is exercised by invoking this script — it is the
verify-of-the-verify and is not embedded in pytest.

Usage::

    uv run python scripts/verify_counterfactual_replay_engine.py [--db-path PATH]

Exit codes: ``0`` on full PASS, ``1`` on any FAIL.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

# Side-effect import: register the state-persistence tables on ``Base.metadata``
# so ``create_all`` (ephemeral run) and the engine's reads see them.
import alphamind.state.tables  # noqa: F401
from alphamind._kernel.ids import BracketId, EnvelopeId, OrderId, PositionId, Symbol
from alphamind._kernel.money import money, price
from alphamind.config.models.execution import FeeSchedule, OrderType, PaperHarness
from alphamind.config.models.replay_engine import CounterfactualReplayEngineConfig
from alphamind.execution.counterfactual_replay_engine.engine import (
    ReplayBatchResult,
    replay_pending_proposals,
)
from alphamind.execution.counterfactual_replay_engine.enums import (
    Confidence,
    ExitLeg,
    ReplayKind,
    ReplayStatus,
    UnevaluableReason,
)
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    CorporateActions,
    OhlcvBars,
    OptionsContracts,
    OptionsContractSnapshots,
)
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.portfolio_state.events.activity_log import ActivityLogEntry
from alphamind.portfolio_state.events.pm_decision import PMDecisionDetail
from alphamind.portfolio_state.events.types import (
    EventGroup,
    EventSource,
    EventType,
    PMVerdict,
)
from alphamind.portfolio_state.records.orders import (
    BracketLeg,
    BracketLegEnforcement,
    BracketLegStatus,
    BracketLegType,
    BracketRecord,
    BracketStatus,
    PriceTrigger,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    OptionContractType,
    OptionGreeks,
    OptionsPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
)
from alphamind.scripts._stdio import configure_utf8_stdio
from alphamind.state.invocation_context.activity_log import activity_log_entry_to_row
from alphamind.state.repository.counterfactual_replays import (
    load_counterfactual_replays_for_envelope,
)
from alphamind.state.tables.brackets_codec import record_to_rows as bracket_record_to_rows
from alphamind.state.tables.invocations import InvocationRow
from alphamind.state.tables.orders import OrderRow
from alphamind.state.tables.positions_codec import record_to_row as position_record_to_row
from alphamind.state.tables.process_lifetimes import ProcessLifetimeRow

if TYPE_CHECKING:
    from alphamind.execution.counterfactual_replay_engine.records import (
        CounterfactualReplayRecord,
    )

__all__ = [
    "EXPECTED_TOTAL",
    "ExpectedProposal",
    "ReplaySeed",
    "assert_batch_counts",
    "assert_corporate_action_in_window",
    "assert_data_missing",
    "assert_idempotent_second_run",
    "assert_ineligible_strategy",
    "assert_modification_record_written",
    "assert_option_target_hit",
    "assert_per_envelope_outcomes",
    "assert_strategist_close",
    "assert_target_hit_pl",
    "build_test_seed",
    "main",
    "run_verification",
]

# --- Seed clock ------------------------------------------------------------
# The proposal time anchors every window; ``as_of`` is four days later so the
# analyst 24h horizon and the strategist 72h forward window have both elapsed.
_PROPOSAL_TS = datetime(2026, 6, 1, 14, 0, 0, tzinfo=UTC)
_AS_OF = _PROPOSAL_TS + timedelta(days=4)
_RISK_FREE_RATE = 0.04
_INVOCATION_ID = "inv-2026-06-01T14:00:00Z-cfr"

# 15-minute bars; the seed walks this grid forward from the proposal bar.
_BAR_STEP = timedelta(minutes=15)

# P/L tolerance: with no harness estimate the drag is exactly zero, so the
# realized P/L equals the gross move to the cent. A one-cent band absorbs only
# Decimal-rounding noise.
_PL_TOLERANCE = Decimal("0.01")

_ENGINE_CONFIG = CounterfactualReplayEngineConfig(
    iv_lag_low_confidence_threshold_minutes=30,
    strategist_default_forward_window_hours=72,
)

_PAPER_HARNESS = PaperHarness(
    spread_buffer_pct=0.1,
    impact_coefficients={
        OrderType.market: 0.5,
        OrderType.limit: 0.3,
        OrderType.stop: 0.4,
    },
    fee_schedule=FeeSchedule(
        cat_per_executed_share=0.0001,
        taf_per_share_sells=0.000166,
        sec_pct_of_notional_sells=0.0000278,
        orf_per_options_contract=0.02685,
        occ_per_options_contract=0.02,
    ),
)


# ---------------------------------------------------------------------------
# Ground-truth seed
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExpectedProposal:
    """The expected replay outcome for one seeded proposal.

    ``envelope_id`` keys the persisted record; ``replay_kind`` distinguishes the
    REJECTION / MODIFICATION_ORIGINAL_FORM record. ``expected_status`` and
    (when UNEVALUABLE) ``expected_reason`` / (when EVALUATED) ``expected_exit_leg``
    are the per-record assertions. ``expected_realized_pl`` is set only on the
    two analytically-derivable trades (the equity and strategist-close TARGET P/L
    the tolerance assertions check).
    """

    label: str
    envelope_id: str
    replay_kind: ReplayKind
    expected_status: ReplayStatus
    expected_reason: UnevaluableReason | None = None
    expected_exit_leg: ExitLeg | None = None
    expected_realized_pl: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ReplaySeed:
    """The ground truth a :func:`build_test_seed` run establishes.

    ``proposals`` is the per-envelope expectation list (one per seeded proposal);
    ``as_of`` is the batch "now" the runner gates the horizon against.
    """

    proposals: tuple[ExpectedProposal, ...]
    as_of: datetime


# The eight documented proposals, by stable envelope id.
_ENV_EQUITY_TARGET = "env-analyst-equity-target"
_ENV_OPTION_TARGET = "env-analyst-option-target"
_ENV_MODIFIED = "env-analyst-modified"
_ENV_STRATEGIST_CLOSE = "env-strategist-close"
_ENV_STRATEGIST_ADD = "env-strategist-add"
_ENV_INELIGIBLE = "env-ineligible-strategy"
_ENV_DATA_MISSING = "env-data-missing"
_ENV_CORP_ACTION = "env-corporate-action"

EXPECTED_TOTAL = 8
"""One persisted record per seeded proposal (story §2)."""

# Equity TARGET_HIT P/L: market entry fills at bars[1].open (= 100), target 110
# hit on a later bar high; qty 10; zero drag → (110 - 100) * 10 = 100.
_EQUITY_ENTRY_OPEN = 100.0
_EQUITY_TARGET = 110.0
_EQUITY_QTY = 10.0
_EQUITY_EXPECTED_PL = (Decimal("110.00") - Decimal("100.00")) * Decimal(10)

# Strategist CLOSE P/L: one-shot exit at bars[1].open (= 105) against the
# average cost basis (100); long 8 shares; zero drag → (105 - 100) * 8 = 40.
_CLOSE_BASIS = 100.0
_CLOSE_EXIT_OPEN = 105.0
_CLOSE_QTY = 8.0
_CLOSE_EXPECTED_PL = (Decimal("105.00") - Decimal("100.00")) * Decimal(8)


def build_test_seed(session: Session) -> ReplaySeed:
    """Insert the eight documented proposals + their supporting data.

    Seeds, in one transaction, the FK substrate (process-lifetime + invocation),
    the per-proposal PM-decision activity-log rows, the bar histories / IV
    snapshots / corporate actions the eligibility gate and simulators read, and
    the strategist position state (with brackets) the close / add paths fold.
    Returns the :class:`ReplaySeed` ground truth the assertions read.
    """
    _seed_fk_substrate(session)

    _seed_analyst_equity_target(session)
    _seed_analyst_option_target(session)
    _seed_analyst_modified(session)
    _seed_strategist_close(session)
    _seed_strategist_add(session)
    _seed_ineligible_strategy(session)
    _seed_data_missing(session)
    _seed_corporate_action(session)

    session.flush()

    return ReplaySeed(
        proposals=(
            ExpectedProposal(
                label="analyst equity reject (TARGET_HIT)",
                envelope_id=_ENV_EQUITY_TARGET,
                replay_kind=ReplayKind.REJECTION,
                expected_status=ReplayStatus.EVALUATED,
                expected_exit_leg=ExitLeg.TARGET_HIT,
                expected_realized_pl=_EQUITY_EXPECTED_PL,
            ),
            ExpectedProposal(
                label="analyst option long-call reject (TARGET_HIT)",
                envelope_id=_ENV_OPTION_TARGET,
                replay_kind=ReplayKind.REJECTION,
                expected_status=ReplayStatus.EVALUATED,
                expected_exit_leg=ExitLeg.TARGET_HIT,
            ),
            ExpectedProposal(
                label="analyst modified (MODIFICATION_ORIGINAL_FORM record)",
                envelope_id=_ENV_MODIFIED,
                replay_kind=ReplayKind.MODIFICATION_ORIGINAL_FORM,
                expected_status=ReplayStatus.EVALUATED,
                expected_exit_leg=ExitLeg.TARGET_HIT,
            ),
            ExpectedProposal(
                label="strategist CLOSE on equity position",
                envelope_id=_ENV_STRATEGIST_CLOSE,
                replay_kind=ReplayKind.REJECTION,
                expected_status=ReplayStatus.EVALUATED,
                expected_exit_leg=ExitLeg.STRATEGIST_CLOSE_AT_PROPOSAL,
                expected_realized_pl=_CLOSE_EXPECTED_PL,
            ),
            ExpectedProposal(
                label="strategist ADD on option position",
                envelope_id=_ENV_STRATEGIST_ADD,
                replay_kind=ReplayKind.REJECTION,
                expected_status=ReplayStatus.EVALUATED,
                expected_exit_leg=ExitLeg.TARGET_HIT,
            ),
            ExpectedProposal(
                label="ineligible strategy instrument (unsupported_instrument)",
                envelope_id=_ENV_INELIGIBLE,
                replay_kind=ReplayKind.REJECTION,
                expected_status=ReplayStatus.UNEVALUABLE,
                expected_reason=UnevaluableReason.UNSUPPORTED_INSTRUMENT,
            ),
            ExpectedProposal(
                label="data-missing (DATA_MISSING)",
                envelope_id=_ENV_DATA_MISSING,
                replay_kind=ReplayKind.REJECTION,
                expected_status=ReplayStatus.UNEVALUABLE,
                expected_reason=UnevaluableReason.DATA_MISSING,
            ),
            ExpectedProposal(
                label="corporate-action-in-window (CORPORATE_ACTION_IN_WINDOW)",
                envelope_id=_ENV_CORP_ACTION,
                replay_kind=ReplayKind.REJECTION,
                expected_status=ReplayStatus.UNEVALUABLE,
                expected_reason=UnevaluableReason.CORPORATE_ACTION_IN_WINDOW,
            ),
        ),
        as_of=_AS_OF,
    )


# ---------------------------------------------------------------------------
# Seed helpers — FK substrate
# ---------------------------------------------------------------------------


def _seed_fk_substrate(session: Session) -> None:
    """Seed the process-lifetime + invocation the activity_log FK requires."""
    session.merge(_process_lifetime_row())
    session.flush()
    session.add(_invocation_row())
    session.flush()


_PLT_ID = "plt-cfr-verify"
_TS_ISO = "2026-06-01T14:00:00Z"


def _process_lifetime_row() -> ProcessLifetimeRow:
    return ProcessLifetimeRow(
        process_lifetime_id=_PLT_ID,
        process_role="monitor",
        process_start_at=_TS_ISO,
        process_pid=1,
        hostname="verify-host",
        git_sha="0" * 40,
        git_branch="main",
        git_dirty=0,
        python_version="3.13.0",
        pip_freeze_hash="0" * 64,
        pip_freeze_snapshot_path="snapshot/path",
        anthropic_sdk_version="0.0.0",
        claude_agent_sdk_version="0.0.0",
        os_release="verify-os",
    )


def _invocation_row() -> InvocationRow:
    return InvocationRow(
        invocation_id=_INVOCATION_ID,
        process_lifetime_id=_PLT_ID,
        start_at=_TS_ISO,
        phase1_completed_at=None,
        phase2_completed_at=None,
        trigger_type="manual",
        trigger_source="continuous_monitor",
        trigger_reason="verify-fixture",
        git_sha_at_invocation="0" * 40,
        active_profile="default",
        active_regime="normal",
        active_mode="normal",
        active_overlays_json="[]",
        resolved_config_hash="0" * 64,
        resolved_config_snapshot_path="snapshot/path",
        feature_flags_snapshot_json="{}",
        data_calibration_state_snapshot_path="snapshot/path",
        data_source_freshness_json="{}",
        fill_collection_summary_json=None,
        command_execution_summary_json=None,
        staleness_flag=0,
        snapshot_metadata_json=None,
    )


# ---------------------------------------------------------------------------
# Seed helpers — bars / options / corporate actions
# ---------------------------------------------------------------------------


def _ensure_underlying(session: Session, ticker: str) -> None:
    """Seed the ``asset_universe`` row the ``ohlcv_bars.ticker`` FK requires."""
    session.add(
        AssetUniverse(
            asset_id=f"asset-{ticker.lower()}",
            ticker=ticker,
            full_name=f"{ticker} Holdings",
            asset_class="equity",
            asset_role="universe",
            exchange="NASDAQ",
            is_active=1,
            added_date="2020-01-01",
            last_updated=_TS_ISO,
        )
    )
    session.flush()


def _bar(
    ticker: str,
    period_start: datetime,
    *,
    open_: float,
    high: float,
    low: float,
    close: float,
) -> OhlcvBars:
    """A prod-faithful 15-minute bar (``period_end == period_start``).

    The production polygon collector writes ``period_end == period_start``; the
    SQL bar repo floors the load start to the bar boundary and selects by
    ``period_start`` alone, so the proposal-containing bar is ``bars[0]`` and the
    market entry fills at ``bars[1].open``.
    """
    iso = period_start.isoformat()
    return OhlcvBars(
        ticker=ticker,
        timeframe="15min",
        period_start=iso,
        period_end=iso,
        session="regular",
        adj_open=open_,
        adj_high=high,
        adj_low=low,
        adj_close=close,
        adj_volume=1,
        adj_vwap=None,
        unadj_open=open_,
        unadj_high=high,
        unadj_low=low,
        unadj_close=close,
        unadj_volume=1_000_000,
        unadj_vwap=None,
        trade_count=None,
        source="verify",
        ingested_at=_TS_ISO,
    )


def _add_flat_bars_with_spike(
    session: Session,
    ticker: str,
    *,
    base: float,
    spike_high: float,
    spike_index: int,
    count: int,
) -> None:
    """Seed *count* flat bars at *base* with one bar's high spiking to *spike_high*.

    The spike bar (at *spike_index*) lets a long target between *base* and
    *spike_high* fire as a TARGET_HIT; the others are flat so no earlier
    target / stop / time leg trips first.
    """
    _ensure_underlying(session, ticker)
    for i in range(count):
        ts = _PROPOSAL_TS + _BAR_STEP * i
        high = spike_high if i == spike_index else base
        session.add(_bar(ticker, ts, open_=base, high=high, low=base, close=base))


def _option_contract_row(
    *, contract_ticker: str, underlying: str, strike: float, expiration: str
) -> OptionsContracts:
    return OptionsContracts(
        contract_ticker=contract_ticker,
        underlying_ticker=underlying,
        expiration_date=expiration,
        strike_price=strike,
        contract_type="call",
        first_seen_at="2026-05-01T00:00:00Z",
        last_seen_at=_TS_ISO,
        source="verify",
    )


def _add_iv_snapshots(
    session: Session,
    *,
    contract_ticker: str,
    underlying: str,
    implied_volatility: float,
    count: int,
) -> None:
    """Seed one IV snapshot per bar so entry + exit IV lookups both resolve.

    Each snapshot is keyed at a bar's ``period_start`` so the at-or-before lookup
    returns a zero-lag IV for both the entry bar and the exit bar.
    """
    for i in range(count):
        ts = _PROPOSAL_TS + _BAR_STEP * i
        session.add(
            OptionsContractSnapshots(
                snapshot_ts=ts.isoformat(),
                contract_ticker=contract_ticker,
                underlying_ticker=underlying,
                open_interest=100,
                volume_today=50,
                last_price=5.0,
                bid=4.9,
                ask=5.1,
                implied_volatility=implied_volatility,
                underlying_price=100.0,
                source="verify",
                ingested_at=_TS_ISO,
            )
        )


# ---------------------------------------------------------------------------
# Seed helpers — PM-decision activity-log rows
# ---------------------------------------------------------------------------


def _add_pm_decision(
    session: Session,
    *,
    entry_id: str,
    envelope_id: str,
    verdict: PMVerdict,
    source_provenance_json: dict[str, Any],
    originating_proposal_json: dict[str, Any],
    position_id: str | None = None,
) -> None:
    """Persist one PM_DECISION activity-log row via the production write path.

    ``activity_log_entry_to_row`` stores ``entry_at`` as
    ``isoformat().replace("+00:00","Z")``; building the row through the codec (not
    by hand) keeps the stored timestamp byte-identical to production so the
    queue's ``since`` / horizon filtering matches.
    """
    detail = PMDecisionDetail(
        envelope_id=envelope_id,
        source_provenance_json=source_provenance_json,
        evaluation_json={"thesis_quality": "strong"},
        modifications_json=[],
        resulting_command_ids=("cmd-1",),
        verdict=verdict,
        originating_proposal_json=originating_proposal_json,
    )
    entry = ActivityLogEntry(
        entry_id=entry_id,
        invocation_id=_INVOCATION_ID,
        timestamp=_PROPOSAL_TS,
        event_type=EventType.PM_DECISION,
        event_group=EventGroup.PM_DECISION,
        position_id=position_id,
        order_id=None,
        thesis_id=None,
        source=EventSource.COMMAND_EXECUTOR,
        detail=detail,
    )
    session.add(activity_log_entry_to_row(entry))


def _analyst_equity_json(*, ticker: str, target: str) -> dict[str, Any]:
    """A valid analyst equity ``Recommendation`` body (market entry, 24h horizon)."""
    return {
        "recommendation_id": "REC-1",
        "instrument": {"asset_type": "equity", "ticker": ticker, "direction": "long"},
        "underlying": ticker,
        "sector": "tech",
        "conviction_level": 3,
        "entry_order": {"type": "market"},
        "position_size": {
            "quantity": _EQUITY_QTY,
            "dollar_value": "1000.00",
            "pct_of_portfolio": 0.05,
        },
        "target": {
            "target_type": "absolute_price",
            "price": target,
            "dollar_pl_target": "500.00",
        },
        "invalidation_legs": [
            {
                "leg_id": "INV-1",
                "type": "price",
                "is_hard": True,
                "condition": {
                    "underlying_trigger": ticker,
                    "comparator": "<=",
                    "trigger_price": "80.00",
                },
                "order_parameters": {"order_type": "market"},
            }
        ],
        "time_expectation_hours": 24.0,
        "guardrail_validation_result": {
            "overall": "PASS",
            "per_rule": [],
            "checked_at": "2026-06-01T13:00:00+00:00",
        },
        "thesis_narrative": "Earnings beat expected.",
        "target_rationale": "Resistance at target.",
        "invalidation_rationale": [{"leg_id": "INV-1", "rationale": "Support broken."}],
        "position_size_rationale": "Standard allocation.",
        "counterarguments_acknowledged": "Macro risk acknowledged.",
    }


def _analyst_option_json(
    *, ticker: str, strike: str, expiration: str, target: str
) -> dict[str, Any]:
    """A valid analyst single-leg long-call ``Recommendation`` body."""
    body = _analyst_equity_json(ticker=ticker, target=target)
    body["recommendation_id"] = "REC-2"
    body["instrument"] = {
        "asset_type": "option",
        "underlying": ticker,
        "strike": strike,
        "expiration": expiration,
        "contract_type": "call",
        "direction": "long",
    }
    return body


def _analyst_strategy_json(*, ticker: str) -> dict[str, Any]:
    """A valid analyst multi-leg strategy ``Recommendation`` body (ineligible).

    A strategy instrument is the Rule-A UNSUPPORTED_INSTRUMENT case: it never
    reaches the simulator, so it need only be a *valid* ``Recommendation`` that
    hydrates. A strategy take-profit references net P/L, so the target is
    ``pl_percentage`` (an ``absolute_price`` strategy target is rejected upstream).
    """
    body = _analyst_equity_json(ticker=ticker, target="110.00")
    body["recommendation_id"] = "REC-3"
    body["target"] = {
        "target_type": "pl_percentage",
        "price": "110.00",
        "dollar_pl_target": "500.00",
        "pl_percentage": 0.5,
    }
    body["instrument"] = {
        "asset_type": "strategy",
        "strategy_type": "vertical_spread",
        "underlying": ticker,
        "legs": [
            {
                "strike": "100.00",
                "expiration": "2026-07-17",
                "contract_type": "call",
                "direction": "long",
                "quantity_ratio": 1,
            },
            {
                "strike": "110.00",
                "expiration": "2026-07-17",
                "contract_type": "call",
                "direction": "short",
                "quantity_ratio": 1,
            },
        ],
    }
    return body


def _strategist_close_json(*, position_id: str, underlying: str) -> dict[str, Any]:
    """A valid strategist ``PositionAssessment`` body (close action)."""
    return {
        "assessment_id": "SA-1",
        "position_id": position_id,
        "thesis_id": "THESIS-1",
        "underlying": underlying,
        "sector": "tech",
        "thesis_status": "invalidated",
        "recommended_action": "close",
        "action_parameters": {
            "action": "close",
            "quantity": "all",
            "order_type": "market",
            "close_rationale_type": "thesis_invalidated",
        },
        "exposure_impact": {
            "sector_delta_adjusted_change": "-800.00",
            "net_directional_impact": "-800.00",
        },
        "status_rationale": "Thesis invalidated by earnings miss.",
        "action_rationale": "Close to preserve capital.",
    }


def _strategist_add_json(*, position_id: str, underlying: str, target: str) -> dict[str, Any]:
    """A valid strategist ``PositionAssessment`` body (add action, option position).

    The add carries a ``bracket_adjustment.new_target_level`` so the bracket walk
    has a concrete target without relying on the position's existing TAKE_PROFIT.
    """
    return {
        "assessment_id": "SA-2",
        "position_id": position_id,
        "thesis_id": "THESIS-2",
        "underlying": underlying,
        "sector": "tech",
        "thesis_status": "on-track",
        "recommended_action": "add",
        "action_parameters": {
            "action": "add",
            "additional_quantity": 2.0,
            "additional_dollar_value": "1000.00",
            "entry_order": {"type": "market"},
            "bracket_adjustment": {
                "action": "adjust-bracket",
                "new_target_level": {"price": target, "order_type": "limit"},
            },
        },
        "exposure_impact": {
            "sector_delta_adjusted_change": "1000.00",
            "net_directional_impact": "1000.00",
        },
        "guardrail_validation_result": {
            "overall": "PASS",
            "per_rule": [],
            "checked_at": "2026-06-01T13:00:00+00:00",
        },
        "add_conviction_justification": "Conviction increased on follow-through.",
        "status_rationale": "Thesis intact and strengthening.",
        "action_rationale": "Add to a working position.",
    }


# ---------------------------------------------------------------------------
# Seed helpers — strategist position state
# ---------------------------------------------------------------------------


def _seed_equity_position(
    session: Session, *, position_id: str, ticker: str, basis: float, qty: float
) -> None:
    """Seed an open long equity position with a single entry fill at *basis*."""
    record = PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=_PROPOSAL_TS - timedelta(days=1),
        details=EquityPositionDetails(
            ticker=Symbol(ticker),
            share_count=qty,
            average_cost_basis_per_share=basis,
        ),
        execution_history=(
            PositionFill(
                fill_timestamp=_PROPOSAL_TS - timedelta(days=1),
                fill_price=price(str(basis)),
                fill_quantity=qty,
                slippage=money("0"),
                fees=money("0"),
            ),
        ),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )
    session.add(position_record_to_row(record))
    session.flush()


@dataclass(frozen=True, slots=True)
class _OptionPositionSpec:
    """The economics of a seeded long-call option position + its bracket levels."""

    underlying: str
    strike: float
    expiration: str
    premium: float
    qty: float
    target: float
    stop: float


def _seed_option_position_with_bracket(
    session: Session,
    *,
    position_id: str,
    bracket_id: str,
    spec: _OptionPositionSpec,
) -> None:
    """Seed an open long option position + one bracket (TAKE_PROFIT + PRICE_STOP).

    The bracket rows exercise the as-of position-state reconstruction's
    ``_load_open_brackets`` read; ``brackets_codec.record_to_rows`` flushes the
    bracket row before its leg rows, satisfying the non-deferrable
    ``bracket_legs.bracket_id`` FK.
    """
    record = PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=None,
        bracket_id=BracketId(bracket_id),
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=_PROPOSAL_TS - timedelta(days=1),
        details=OptionsPositionDetails(
            underlying_ticker=Symbol(spec.underlying),
            strike_price=spec.strike,
            expiration_date=datetime.fromisoformat(spec.expiration).date(),
            contract_type=OptionContractType.CALL,
            contract_count=spec.qty,
            contract_multiplier=100.0,
            premium_paid_per_contract=spec.premium,
            greeks=OptionGreeks(delta=0.5, gamma=0.1, theta=-0.1, vega=0.2),
        ),
        execution_history=(
            PositionFill(
                fill_timestamp=_PROPOSAL_TS - timedelta(days=1),
                fill_price=price(str(spec.premium)),
                fill_quantity=spec.qty,
                slippage=money("0"),
                fees=money("0"),
            ),
        ),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )
    session.add(position_record_to_row(record))
    session.flush()

    bracket = BracketRecord(
        bracket_id=BracketId(bracket_id),
        position_id=PositionId(position_id),
        status=BracketStatus.ACTIVE,
        entry_order_id=OrderId(f"{bracket_id}-entry"),
        protective_legs=(
            BracketLeg(
                leg_id="LEG-TP",
                leg_type=BracketLegType.TAKE_PROFIT,
                order_id=None,
                trigger=PriceTrigger(
                    underlying_ticker=Symbol(spec.underlying),
                    threshold_usd=spec.target,
                    direction="GTE",
                ),
                enforcement=BracketLegEnforcement.MECHANICAL,
                status=BracketLegStatus.ACTIVE,
            ),
            BracketLeg(
                leg_id="LEG-SL",
                leg_type=BracketLegType.PRICE_STOP,
                order_id=None,
                trigger=PriceTrigger(
                    underlying_ticker=Symbol(spec.underlying),
                    threshold_usd=spec.stop,
                    direction="LTE",
                ),
                enforcement=BracketLegEnforcement.MECHANICAL,
                status=BracketLegStatus.ACTIVE,
            ),
        ),
        modification_history=(),
        corporate_action_cancellation_reason=None,
        entry_window_deadline=None,
    )
    bracket_row, leg_rows = bracket_record_to_rows(bracket)
    session.add(bracket_row)
    session.flush()
    for leg_row in leg_rows:
        session.add(leg_row)
    session.flush()
    # The bracket's ``entry_order_id`` carries a deferrable FK to ``orders``;
    # seed the entry order so the cluster commits cleanly.
    session.add(
        _entry_order_row(
            order_id=f"{bracket_id}-entry", bracket_id=bracket_id, position_id=position_id
        )
    )
    session.flush()


def _entry_order_row(*, order_id: str, bracket_id: str, position_id: str) -> OrderRow:
    """Minimal filled entry ``OrderRow`` — the bracket's ``entry_order_id`` target."""
    return OrderRow(
        order_id=order_id,
        position_id=position_id,
        bracket_id=bracket_id,
        order_role="ENTRY",
        order_class="SIMPLE",
        instrument_spec_json='{"instrument_type":"EQUITY","ticker":"STUB"}',
        direction="BUY",
        order_type="MARKET",
        quantity=1.0,
        price_parameters_json="{}",
        duration="DAY",
        status="FILLED",
        alpaca_order_id=None,
        alpaca_order_id_chain_json="[]",
        submission_timestamp=_TS_ISO,
        last_update_timestamp=_TS_ISO,
        filled_quantity=1.0,
        average_fill_price=None,
        remaining_quantity=0.0,
        modification_count=0,
        metadata_json=(
            '{"originating_thesis_id":null,"originating_pm_command_id":null,"age_hours":0.0}'
        ),
        client_order_id=None,
    )


# ---------------------------------------------------------------------------
# Per-proposal seed orchestrators
# ---------------------------------------------------------------------------


def _seed_analyst_equity_target(session: Session) -> None:
    ticker = "AAPL"
    _add_flat_bars_with_spike(
        session, ticker, base=_EQUITY_ENTRY_OPEN, spike_high=_EQUITY_TARGET, spike_index=3, count=8
    )
    _add_pm_decision(
        session,
        entry_id="e-equity-target",
        envelope_id=_ENV_EQUITY_TARGET,
        verdict=PMVerdict.REJECT,
        source_provenance_json={"source_provenance": "pm_analyst"},
        originating_proposal_json=_analyst_equity_json(ticker=ticker, target="110.00"),
    )


def _seed_analyst_option_target(session: Session) -> None:
    ticker = "MSFT"
    strike = "100.00"
    expiration = "2026-07-17"
    contract_ticker = "O:MSFT260717C00100000"
    _add_flat_bars_with_spike(
        session, ticker, base=_EQUITY_ENTRY_OPEN, spike_high=_EQUITY_TARGET, spike_index=3, count=8
    )
    session.add(
        _option_contract_row(
            contract_ticker=contract_ticker, underlying=ticker, strike=100.0, expiration=expiration
        )
    )
    session.flush()
    _add_iv_snapshots(
        session,
        contract_ticker=contract_ticker,
        underlying=ticker,
        implied_volatility=0.35,
        count=8,
    )
    _add_pm_decision(
        session,
        entry_id="e-option-target",
        envelope_id=_ENV_OPTION_TARGET,
        verdict=PMVerdict.REJECT,
        source_provenance_json={"source_provenance": "pm_analyst"},
        originating_proposal_json=_analyst_option_json(
            ticker=ticker, strike=strike, expiration=expiration, target="110.00"
        ),
    )


def _seed_analyst_modified(session: Session) -> None:
    ticker = "GOOG"
    _add_flat_bars_with_spike(
        session, ticker, base=_EQUITY_ENTRY_OPEN, spike_high=_EQUITY_TARGET, spike_index=3, count=8
    )
    _add_pm_decision(
        session,
        entry_id="e-modified",
        envelope_id=_ENV_MODIFIED,
        verdict=PMVerdict.APPROVE_WITH_MODIFICATION,
        source_provenance_json={"source_provenance": "pm_analyst"},
        originating_proposal_json=_analyst_equity_json(ticker=ticker, target="110.00"),
    )


def _seed_strategist_close(session: Session) -> None:
    ticker = "AMZN"
    position_id = "POS-CLOSE"
    # Flat bars at the close exit level so bars[1].open == exit price.
    _ensure_underlying(session, ticker)
    for i in range(6):
        ts = _PROPOSAL_TS + _BAR_STEP * i
        session.add(
            _bar(
                ticker,
                ts,
                open_=_CLOSE_EXIT_OPEN,
                high=_CLOSE_EXIT_OPEN,
                low=_CLOSE_EXIT_OPEN,
                close=_CLOSE_EXIT_OPEN,
            )
        )
    _seed_equity_position(
        session, position_id=position_id, ticker=ticker, basis=_CLOSE_BASIS, qty=_CLOSE_QTY
    )
    _add_pm_decision(
        session,
        entry_id="e-strategist-close",
        envelope_id=_ENV_STRATEGIST_CLOSE,
        verdict=PMVerdict.REJECT,
        source_provenance_json={
            "source_provenance": "pm_strategist",
            "recommendation_type": "position_assessment",
        },
        originating_proposal_json=_strategist_close_json(
            position_id=position_id, underlying=ticker
        ),
        position_id=position_id,
    )


def _seed_strategist_add(session: Session) -> None:
    ticker = "NVDA"
    position_id = "POS-ADD"
    bracket_id = "BRK-ADD"
    strike = 100.0
    expiration = "2026-07-17"
    contract_ticker = "O:NVDA260717C00100000"
    _add_flat_bars_with_spike(
        session, ticker, base=_EQUITY_ENTRY_OPEN, spike_high=_EQUITY_TARGET, spike_index=3, count=8
    )
    session.add(
        _option_contract_row(
            contract_ticker=contract_ticker, underlying=ticker, strike=strike, expiration=expiration
        )
    )
    session.flush()
    _add_iv_snapshots(
        session,
        contract_ticker=contract_ticker,
        underlying=ticker,
        implied_volatility=0.35,
        count=8,
    )
    _seed_option_position_with_bracket(
        session,
        position_id=position_id,
        bracket_id=bracket_id,
        spec=_OptionPositionSpec(
            underlying=ticker,
            strike=strike,
            expiration=expiration,
            premium=5.0,
            qty=4.0,
            target=_EQUITY_TARGET,
            stop=80.0,
        ),
    )
    _add_pm_decision(
        session,
        entry_id="e-strategist-add",
        envelope_id=_ENV_STRATEGIST_ADD,
        verdict=PMVerdict.REJECT,
        source_provenance_json={
            "source_provenance": "pm_strategist",
            "recommendation_type": "position_assessment",
        },
        originating_proposal_json=_strategist_add_json(
            position_id=position_id, underlying=ticker, target="110.00"
        ),
        position_id=position_id,
    )


def _seed_ineligible_strategy(session: Session) -> None:
    ticker = "TSLA"
    _add_flat_bars_with_spike(
        session, ticker, base=_EQUITY_ENTRY_OPEN, spike_high=_EQUITY_TARGET, spike_index=3, count=8
    )
    _add_pm_decision(
        session,
        entry_id="e-ineligible",
        envelope_id=_ENV_INELIGIBLE,
        verdict=PMVerdict.REJECT,
        source_provenance_json={"source_provenance": "pm_analyst"},
        originating_proposal_json=_analyst_strategy_json(ticker=ticker),
    )


def _seed_data_missing(session: Session) -> None:
    # No bars seeded for this ticker → the eligibility bar-presence check fails.
    ticker = "META"
    _add_pm_decision(
        session,
        entry_id="e-data-missing",
        envelope_id=_ENV_DATA_MISSING,
        verdict=PMVerdict.REJECT,
        source_provenance_json={"source_provenance": "pm_analyst"},
        originating_proposal_json=_analyst_equity_json(ticker=ticker, target="110.00"),
    )


def _seed_corporate_action(session: Session) -> None:
    ticker = "NFLX"
    _add_flat_bars_with_spike(
        session, ticker, base=_EQUITY_ENTRY_OPEN, spike_high=_EQUITY_TARGET, spike_index=3, count=8
    )
    # An ex-date inside the replay window → CORPORATE_ACTION_IN_WINDOW.
    session.add(
        CorporateActions(
            action_id="CA-1",
            ticker=ticker,
            action_type="dividend",
            declaration_date=None,
            ex_date=(_PROPOSAL_TS + timedelta(hours=12)).date().isoformat(),
            record_date=None,
            payable_date=None,
            ratio=None,
            cash_amount_per_share=0.5,
            new_ticker=None,
            acquirer_ticker=None,
            spin_off_ticker=None,
            description="Quarterly dividend.",
            source="verify",
            ingested_at=_TS_ISO,
        )
    )
    _add_pm_decision(
        session,
        entry_id="e-corp-action",
        envelope_id=_ENV_CORP_ACTION,
        verdict=PMVerdict.REJECT,
        source_provenance_json={"source_provenance": "pm_analyst"},
        originating_proposal_json=_analyst_equity_json(ticker=ticker, target="110.00"),
    )


# ---------------------------------------------------------------------------
# Pure assertion helpers — return a failure message or ``None``
# ---------------------------------------------------------------------------


def assert_batch_counts(result: ReplayBatchResult, seed: ReplaySeed) -> str | None:
    """The first run evaluated 5 proposals and wrote 3 UNEVALUABLE records."""
    expected_evaluated = sum(
        1 for p in seed.proposals if p.expected_status is ReplayStatus.EVALUATED
    )
    expected_unevaluable_by_reason: dict[str, int] = {}
    for p in seed.proposals:
        if p.expected_status is ReplayStatus.UNEVALUABLE:
            assert p.expected_reason is not None
            key = p.expected_reason.value
            expected_unevaluable_by_reason[key] = expected_unevaluable_by_reason.get(key, 0) + 1

    if result.error_count != 0:
        return f"expected 0 batch errors, got {result.error_count} (first: {result.first_error})"
    if result.skipped_idempotent != 0:
        return f"expected 0 skipped_idempotent on first run, got {result.skipped_idempotent}"
    if result.skipped_not_due != 0:
        return f"expected 0 skipped_not_due, got {result.skipped_not_due}"
    if result.evaluated != expected_evaluated:
        return f"expected {expected_evaluated} evaluated, got {result.evaluated}"
    if result.unevaluable_by_reason != expected_unevaluable_by_reason:
        return (
            f"expected unevaluable_by_reason {expected_unevaluable_by_reason}, "
            f"got {result.unevaluable_by_reason}"
        )
    return None


def assert_per_envelope_outcomes(
    records: dict[str, CounterfactualReplayRecord], seed: ReplaySeed
) -> str | None:
    """Each seeded proposal wrote one record with the expected status + reason/leg."""
    if len(records) != EXPECTED_TOTAL:
        return f"expected {EXPECTED_TOTAL} persisted records, got {len(records)}"
    for proposal in seed.proposals:
        record = records.get(proposal.envelope_id)
        if record is None:
            return f"{proposal.label}: no record written for envelope {proposal.envelope_id}"
        if record.replay_status is not proposal.expected_status:
            return (
                f"{proposal.label}: expected status {proposal.expected_status.value}, "
                f"got {record.replay_status.value}"
            )
        if proposal.expected_status is ReplayStatus.UNEVALUABLE:
            if record.unevaluable_reason is not proposal.expected_reason:
                return (
                    f"{proposal.label}: expected reason "
                    f"{proposal.expected_reason.value if proposal.expected_reason else None}, "
                    f"got {record.unevaluable_reason.value if record.unevaluable_reason else None}"
                )
        elif (
            proposal.expected_exit_leg is not None
            and record.exit_leg is not proposal.expected_exit_leg
        ):
            return (
                f"{proposal.label}: expected exit_leg {proposal.expected_exit_leg.value}, "
                f"got {record.exit_leg.value if record.exit_leg else None}"
            )
    return None


def assert_target_hit_pl(
    records: dict[str, CounterfactualReplayRecord], seed: ReplaySeed
) -> str | None:
    """The equity TARGET_HIT record's realized P/L is positive and within tolerance."""
    return _assert_pl_within_tolerance(records, seed, _ENV_EQUITY_TARGET)


def assert_strategist_close(
    records: dict[str, CounterfactualReplayRecord], seed: ReplaySeed
) -> str | None:
    """The strategist-close record's realized P/L matches the documented value."""
    record = records.get(_ENV_STRATEGIST_CLOSE)
    if record is None:
        return "strategist close: no record written"
    if record.exit_leg is not ExitLeg.STRATEGIST_CLOSE_AT_PROPOSAL:
        return (
            "strategist close: expected exit_leg strategist_close_at_proposal, "
            f"got {record.exit_leg.value if record.exit_leg else None}"
        )
    return _assert_pl_within_tolerance(records, seed, _ENV_STRATEGIST_CLOSE)


def assert_option_target_hit(
    records: dict[str, CounterfactualReplayRecord],
) -> str | None:
    """The option TARGET_HIT record is EVALUATED with a non-null P/L + valid confidence."""
    record = records.get(_ENV_OPTION_TARGET)
    if record is None:
        return "option target: no record written"
    if record.replay_status is not ReplayStatus.EVALUATED:
        return f"option target: expected EVALUATED, got {record.replay_status.value}"
    if record.exit_leg is not ExitLeg.TARGET_HIT:
        return (
            "option target: expected exit_leg target_hit, "
            f"got {record.exit_leg.value if record.exit_leg else None}"
        )
    if record.realized_pl is None:
        return "option target: realized_pl is None, expected a value"
    if record.confidence not in (Confidence.HIGH, Confidence.MEDIUM, Confidence.LOW):
        return f"option target: confidence {record.confidence!r} not in the valid set"
    return None


def assert_modification_record_written(
    records: dict[str, CounterfactualReplayRecord],
) -> str | None:
    """The PM-modified proposal wrote a MODIFICATION_ORIGINAL_FORM record."""
    record = records.get(_ENV_MODIFIED)
    if record is None:
        return "modified proposal: no record written"
    if record.replay_kind is not ReplayKind.MODIFICATION_ORIGINAL_FORM:
        return (
            "modified proposal: expected replay_kind modification_original_form, "
            f"got {record.replay_kind.value}"
        )
    return None


def assert_ineligible_strategy(
    records: dict[str, CounterfactualReplayRecord],
) -> str | None:
    """The strategy-instrument proposal is UNEVALUABLE / unsupported_instrument."""
    return _assert_unevaluable(records, _ENV_INELIGIBLE, UnevaluableReason.UNSUPPORTED_INSTRUMENT)


def assert_data_missing(
    records: dict[str, CounterfactualReplayRecord],
) -> str | None:
    """The bars-missing proposal is UNEVALUABLE / data_missing."""
    return _assert_unevaluable(records, _ENV_DATA_MISSING, UnevaluableReason.DATA_MISSING)


def assert_corporate_action_in_window(
    records: dict[str, CounterfactualReplayRecord],
) -> str | None:
    """The corporate-action proposal is UNEVALUABLE / corporate_action_in_window."""
    return _assert_unevaluable(
        records, _ENV_CORP_ACTION, UnevaluableReason.CORPORATE_ACTION_IN_WINDOW
    )


def assert_idempotent_second_run(second: ReplayBatchResult, first_total: int) -> str | None:
    """A second run writes no new records; ``skipped_idempotent`` equals the prior total."""
    if second.evaluated != 0:
        return f"second run: expected 0 evaluated, got {second.evaluated}"
    if second.unevaluable_by_reason:
        return (
            f"second run: expected no new unevaluable records, got {second.unevaluable_by_reason}"
        )
    if second.skipped_idempotent != first_total:
        return (
            f"second run: expected skipped_idempotent == prior total {first_total}, "
            f"got {second.skipped_idempotent}"
        )
    return None


def _assert_unevaluable(
    records: dict[str, CounterfactualReplayRecord],
    envelope_id: str,
    reason: UnevaluableReason,
) -> str | None:
    record = records.get(envelope_id)
    if record is None:
        return f"{envelope_id}: no record written"
    if record.replay_status is not ReplayStatus.UNEVALUABLE:
        return f"{envelope_id}: expected UNEVALUABLE, got {record.replay_status.value}"
    if record.unevaluable_reason is not reason:
        return (
            f"{envelope_id}: expected reason {reason.value}, "
            f"got {record.unevaluable_reason.value if record.unevaluable_reason else None}"
        )
    return None


def _assert_pl_within_tolerance(
    records: dict[str, CounterfactualReplayRecord],
    seed: ReplaySeed,
    envelope_id: str,
) -> str | None:
    record = records.get(envelope_id)
    if record is None:
        return f"{envelope_id}: no record written"
    if record.realized_pl is None:
        return f"{envelope_id}: realized_pl is None, expected a value"
    expected = next(
        (p.expected_realized_pl for p in seed.proposals if p.envelope_id == envelope_id), None
    )
    if expected is None:
        return f"{envelope_id}: seed carries no expected_realized_pl"
    actual = Decimal(record.realized_pl)
    if actual <= 0:
        return f"{envelope_id}: realized_pl {actual} is not positive"
    if abs(actual - expected) > _PL_TOLERANCE:
        return (
            f"{envelope_id}: realized_pl {actual} not within {_PL_TOLERANCE} of expected {expected}"
        )
    return None


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _load_records_by_envelope(
    session: Session, seed: ReplaySeed
) -> dict[str, CounterfactualReplayRecord]:
    """Load one persisted record per seeded envelope, keyed by envelope id."""
    records: dict[str, CounterfactualReplayRecord] = {}
    for proposal in seed.proposals:
        for record in load_counterfactual_replays_for_envelope(
            session, EnvelopeId(proposal.envelope_id)
        ):
            records[str(record.pm_decision_envelope_id)] = record
    return records


def _run_batch(session: Session, seed: ReplaySeed) -> ReplayBatchResult:
    return replay_pending_proposals(
        session,
        config=_ENGINE_CONFIG,
        paper_harness_config=_PAPER_HARNESS,
        risk_free_rate=_RISK_FREE_RATE,
        as_of=seed.as_of,
    )


def run_verification(db_path: str | None = None) -> bool:
    """Seed, run the engine twice, assert each outcome, print the report.

    When *db_path* is ``None`` an ephemeral in-memory SQLite DB is used; an
    explicit *db_path* is a throwaway scratch location the script provisions
    itself. Either way the full ORM schema is materialised before seeding (the
    verify DB holds only controlled fixture rows), so a fresh ``--db-path`` works
    without a separate migration step. Returns ``True`` iff every assertion passes.
    """
    engine = make_engine(db_path) if db_path is not None else make_engine(":memory:")
    # Materialise the schema unconditionally — the verify DB is a controlled
    # throwaway; create_all is a no-op when the tables already exist.
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)

    print("=" * 70)
    print("AlphaMind Counterfactual Replay Engine Verification")
    print("=" * 70)

    results: list[tuple[bool, str]] = []
    try:
        with factory() as session:
            seed = build_test_seed(session)
            session.commit()

            first = _run_batch(session, seed)
            session.commit()
            records = _load_records_by_envelope(session, seed)
            first_total = _batch_total(first)

            results.append(_check("batch counts (first run)", assert_batch_counts(first, seed)))
            results.append(
                _check("per-envelope record outcomes", assert_per_envelope_outcomes(records, seed))
            )
            results.append(
                _check("equity TARGET_HIT realized P/L", assert_target_hit_pl(records, seed))
            )
            results.append(
                _check("option TARGET_HIT evaluated record", assert_option_target_hit(records))
            )
            results.append(
                _check(
                    "PM-modified MODIFICATION_ORIGINAL_FORM record",
                    assert_modification_record_written(records),
                )
            )
            results.append(
                _check("strategist CLOSE realized P/L", assert_strategist_close(records, seed))
            )
            results.append(
                _check("ineligible strategy unevaluable", assert_ineligible_strategy(records))
            )
            results.append(_check("data-missing unevaluable", assert_data_missing(records)))
            results.append(
                _check(
                    "corporate-action-in-window unevaluable",
                    assert_corporate_action_in_window(records),
                )
            )

            second = _run_batch(session, seed)
            session.commit()
            results.append(
                _check(
                    "second run is idempotent",
                    assert_idempotent_second_run(second, first_total),
                )
            )
    finally:
        engine.dispose()

    print()
    for passed, line in results:
        marker = "PASS" if passed else "FAIL"
        print(f"{marker} | {line}")

    all_pass = all(passed for passed, _ in results)
    print()
    status = "PASS" if all_pass else "FAIL"
    print(f"verify counterfactual_replay_engine: {status}")
    return all_pass


def _batch_total(result: ReplayBatchResult) -> int:
    """The batch's ``total_processed`` — the sum the idempotency check expects."""
    return (
        result.evaluated
        + sum(result.unevaluable_by_reason.values())
        + result.skipped_idempotent
        + result.skipped_not_due
        + result.error_count
    )


def _check(description: str, failure: str | None) -> tuple[bool, str]:
    """Render one assertion line: ``(passed, description-or-failure-detail)``."""
    if failure is None:
        return True, description
    return False, f"{description}: {failure}"


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point: parse ``--db-path``, run, exit 0 on PASS else 1."""
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(
        description="Standalone end-to-end verify for the counterfactual replay engine."
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help=(
            "Path to a test SQLite database (must be at schema head). When omitted, "
            "an ephemeral in-memory DB is created and the full schema is materialised."
        ),
    )
    args = parser.parse_args(argv)
    passed = run_verification(args.db_path)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
