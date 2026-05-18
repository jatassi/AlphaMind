"""Synthetic-portfolio seeder for ``--debug-e2e`` mode (story 01c / ALP-496).

``wipe_and_seed`` wipes the nine state-persistence tables enumerated in
the debug-e2e design (§ 6.3) and seeds the synthetic portfolio
(positions + theses + cash-ledger singleton) via the canonical SQLAlchemy
ORM models.

The function is guarded by a DB-path suffix check that refuses to run
unless the resolved path ends with ``-debug-e2e.db`` — the smallest
backstop that prevents a mis-set ``DATABASE_PATH`` env var from wiping
the paper or production DB (parent issue ALP-493 pre-resolved
decision § (C)).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from alphamind.persistence.models import Base, Brief, RegimeAdaptationStateRow
from alphamind.portfolio_state.records.positions import (
    Direction,
    InstrumentType,
    OptionContractType,
    PositionStatus,
)
from alphamind.portfolio_state.records.theses import (
    ThesisComponentType,
    ThesisRecordStatus,
)
from alphamind.state.tables import (
    ActivityLogRow,
    BracketLegRow,
    BracketRow,
    CashLedgerRow,
    CorporateActionIntegrationLedgerRow,
    DrawdownStateRow,
    FillRecordRow,
    InvocationRow,
    OrderRow,
    PositionRow,
    ProcessLifetimeRow,
    ThesisComponentRow,
    ThesisRow,
)
from alphamind.state.tables.cash_ledger import CASH_LEDGER_SINGLETON_ID
from alphamind.state.tables.drawdown_state import DRAWDOWN_STATE_SINGLETON_ID

_DEBUG_DB_SUFFIX = "-debug-e2e.db"


# Tables wiped in FK-safe order — children before parents.
#
# The list is the explicit 9 enumerated in the design (§ 6.3) and
# parent issue ALP-493 pre-resolved decision § (L). The order maps to
# the design's logical names: PositionFills → FillRecordRow,
# PositionTheses → ThesisRow.
# State-persistence tables wiped at the start of every debug-e2e invocation.
#
# The snapshot-from-prod workflow (``scripts/snapshot_prod_for_debug_e2e.py``)
# means the bootstrap DB carries real production rows for every table below,
# so the wipe list MUST cover all state-persistence tables — not just the
# narrower set the original design enumerated when the seeder ran against a
# truly fresh DB. Tables managed by collectors / the data layer
# (``asset_universe``, distillation calibration, news, event calendar, ...)
# are deliberately preserved by the snapshot and are NOT wiped here.
#
# ``PRAGMA defer_foreign_keys = ON`` (set inside :func:`wipe_and_seed`)
# defers FK enforcement until commit, which lets the DELETEs run in any
# order despite the circular FK between ``orders`` and ``brackets``
# (orders.bracket_id → brackets ↔ brackets.entry_order_id → orders). Order
# here documents intent (children-before-parents where there's no cycle)
# rather than being load-bearing for correctness.
_WIPE_ORDER: tuple[type[Base], ...] = (
    ActivityLogRow,
    FillRecordRow,
    BracketLegRow,
    CorporateActionIntegrationLedgerRow,
    OrderRow,
    BracketRow,
    ThesisComponentRow,
    ThesisRow,
    Brief,
    PositionRow,
    CashLedgerRow,
    DrawdownStateRow,
    RegimeAdaptationStateRow,
    InvocationRow,
    ProcessLifetimeRow,
)

# Components seeded per thesis. ACTIVE theses fail validation at codec read
# time unless all three rationale types are present (see
# ``ThesisRecord._check_mandatory_coverage``).
_REQUIRED_COMPONENT_TYPES: tuple[ThesisComponentType, ...] = (
    ThesisComponentType.ENTRY_RATIONALE,
    ThesisComponentType.TARGET_RATIONALE,
    ThesisComponentType.INVALIDATION_RATIONALE,
)


def _isoformat(when: datetime) -> str:
    """ISO-8601 with the canonical ``Z`` suffix the state-persistence layer uses.

    The ``.replace("+00:00", "Z")`` substitution is only safe when
    ``when`` is UTC; a naive or non-UTC input would silently drop its
    offset (no substitution match) and produce an ambiguous timestamp.
    The guard fails loudly so the regression surfaces at the seeder
    rather than downstream where the parsed timestamp is reinterpreted
    as UTC.
    """
    if when.tzinfo is not UTC:
        msg = f"_isoformat requires a UTC datetime, got tzinfo={when.tzinfo!r}"
        raise ValueError(msg)
    return when.isoformat().replace("+00:00", "Z")


def _build_position_row(
    synthetic: Any,
    *,
    position_index: int,
    now: datetime,
) -> PositionRow:
    """Translate a ``Synthetic*`` dataclass into a ``PositionRow``.

    The three synthetic instrument types (``SyntheticEquity``,
    ``SyntheticOption``, ``SyntheticStrategy``) are discriminated
    structurally by attribute presence — ``symbol`` for equities,
    ``contract_type`` for options, ``legs`` for strategies.
    """
    position_id = f"debug-pos-{position_index:02d}"

    if hasattr(synthetic, "legs"):
        return _build_strategy_position_row(synthetic, position_id=position_id, now=now)
    if hasattr(synthetic, "contract_type"):
        return _build_option_position_row(synthetic, position_id=position_id, now=now)
    return _build_equity_position_row(synthetic, position_id=position_id, now=now)


def _build_equity_position_row(
    synthetic: Any,
    *,
    position_id: str,
    now: datetime,
) -> PositionRow:
    direction = Direction(synthetic.direction.value)
    borrow_rate_pct = getattr(synthetic, "borrow_rate_pct", None)
    is_short = direction is Direction.SHORT
    details = {
        "instrument_type": InstrumentType.EQUITY.value,
        "ticker": str(synthetic.symbol),
        "share_count": float(synthetic.qty),
        "average_cost_basis_per_share": float(synthetic.avg_cost),
        "borrow_rate_pct": borrow_rate_pct if is_short else None,
        "locate_status": "LOCATED" if is_short else None,
        "margin_held_usd": 0.0 if is_short else None,
    }
    return PositionRow(
        position_id=position_id,
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.PENDING.value,
        direction=direction.value,
        entry_timestamp=_isoformat(now),
        instrument_type=InstrumentType.EQUITY.value,
        details_json=json.dumps(details),
        execution_history_json="[]",
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=0,
        parent_position_id=None,
        origin=None,
    )


def _build_option_position_row(
    synthetic: Any,
    *,
    position_id: str,
    now: datetime,
) -> PositionRow:
    contract_type = OptionContractType(synthetic.contract_type.value)
    expiration = (now + timedelta(days=int(synthetic.expiration_offset_days))).date()
    direction = Direction.LONG  # synthetic options are long-only in the fixture
    # Fixture greeks (parent issue § (H) defers options_chains seeding;
    # downstream falls back to record-attached greeks).
    greeks = {
        "delta": 0.5 if contract_type is OptionContractType.CALL else -0.5,
        "gamma": 0.01,
        "theta": -0.05,
        "vega": 0.10,
        "as_of_timestamp": None,
        "iv_used": None,
        "refresh_failed": False,
    }
    details = {
        "instrument_type": InstrumentType.OPTIONS.value,
        "underlying_ticker": str(synthetic.underlying),
        "strike_price": float(synthetic.strike),
        "expiration_date": expiration.isoformat(),
        "contract_type": contract_type.value,
        "contract_count": float(synthetic.contracts),
        "contract_multiplier": 100.0,
        "premium_paid_per_contract": float(synthetic.premium_per_contract),
        "greeks": greeks,
    }
    return PositionRow(
        position_id=position_id,
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.PENDING.value,
        direction=direction.value,
        entry_timestamp=_isoformat(now),
        instrument_type=InstrumentType.OPTIONS.value,
        details_json=json.dumps(details),
        execution_history_json="[]",
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=0,
        parent_position_id=None,
        origin=None,
    )


def _build_strategy_position_row(
    synthetic: Any,
    *,
    position_id: str,
    now: datetime,
) -> PositionRow:
    expiration = (now + timedelta(days=int(synthetic.expiration_offset_days))).date()
    legs_payload = []
    # ``SyntheticStrategyLeg`` carries no per-leg premium (the synthetic
    # spread is summarized by ``net_premium`` only), but the pricing layer's
    # ``PriceQuote.__post_init__`` requires per-leg ``price_usd > 0`` when
    # the strategy position is enriched. Spread ``net_premium`` evenly across
    # legs so every leg gets a positive placeholder — the downstream P&L is
    # nominal for debug-e2e (no real prices flow), but the invariant holds.
    per_leg_premium = float(synthetic.net_premium) / max(len(synthetic.legs), 1)
    for index, leg in enumerate(synthetic.legs):
        leg_contract_type = OptionContractType(leg.contract_type.value)
        leg_direction = Direction(leg.direction.value)
        legs_payload.append(
            {
                "leg_id": f"{position_id}-leg-{index}",
                "direction": leg_direction.value,
                "options": {
                    "instrument_type": InstrumentType.OPTIONS.value,
                    "underlying_ticker": str(synthetic.underlying),
                    "strike_price": float(leg.strike),
                    "expiration_date": expiration.isoformat(),
                    "contract_type": leg_contract_type.value,
                    "contract_count": float(leg.contracts),
                    "contract_multiplier": 100.0,
                    "premium_paid_per_contract": per_leg_premium,
                    "greeks": {
                        "delta": 0.0,
                        "gamma": 0.0,
                        "theta": 0.0,
                        "vega": 0.0,
                        "as_of_timestamp": None,
                        "iv_used": None,
                        "refresh_failed": False,
                    },
                },
            }
        )
    strategy_greeks = {
        "delta": 0.0,
        "gamma": 0.0,
        "theta": 0.0,
        "vega": 0.0,
        "as_of_timestamp": None,
        "iv_used": None,
        "refresh_failed": False,
    }
    details = {
        "instrument_type": InstrumentType.STRATEGY.value,
        "strategy_type_label": str(synthetic.strategy_label),
        "legs": legs_payload,
        "net_premium_usd": float(synthetic.net_premium),
        "max_profit_usd": 0.0,
        "max_loss_usd": 0.0,
        "breakeven_levels": [],
        "strategy_greeks": strategy_greeks,
    }
    return PositionRow(
        position_id=position_id,
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.PENDING.value,
        direction=Direction.LONG.value,
        entry_timestamp=_isoformat(now),
        instrument_type=InstrumentType.STRATEGY.value,
        details_json=json.dumps(details),
        execution_history_json="[]",
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=0,
        parent_position_id=None,
        origin=None,
    )


def _component_id(thesis_id: str, component_type: ThesisComponentType) -> str:
    return f"{thesis_id}-{component_type.value.lower()}"


def _build_thesis_rows(
    synthetic: Any, *, now: datetime
) -> tuple[ThesisRow, tuple[ThesisComponentRow, ...]]:
    """Translate a ``SyntheticThesis`` into one parent ``ThesisRow`` + 3 child rows.

    The synthetic thesis is a flat ``(position_index, headline, rationale)``
    triple; the ``ThesisRow`` carries a richer narrative-JSON payload that
    the codec round-trip reads (see
    :func:`alphamind.state.tables.theses_codec.rows_to_record`). ACTIVE
    theses fail validation unless the codec-reconstructed record carries
    ENTRY/TARGET/INVALIDATION rationale components (see
    ``ThesisRecord._check_mandatory_coverage``), so each thesis gets all
    three seeded.
    """
    thesis_id = f"debug-thesis-{synthetic.position_index:02d}"
    position_id = f"debug-pos-{synthetic.position_index:02d}"
    time_expectation_hours = 24.0
    expected_resolution_at = now + timedelta(hours=time_expectation_hours)
    generation_iso = _isoformat(now)

    component_rows: list[ThesisComponentRow] = []
    components_metadata: dict[str, dict[str, str | None]] = {}
    for component_type in _REQUIRED_COMPONENT_TYPES:
        component_id = _component_id(thesis_id, component_type)
        component_rows.append(
            ThesisComponentRow(
                component_id=component_id,
                thesis_id=thesis_id,
                component_type=component_type.value,
                linked_bracket_leg=None,
                instrument_reference=position_id,
                narrative=f"{component_type.value} for {thesis_id}",
                key_assumptions_json="[]",
                supporting_signals_json="[]",
                resolution_outcome=None,
                resolution_notes=None,
            )
        )
        components_metadata[component_id] = {
            "linked_bracket_leg_type": None,
            "generation_timestamp": generation_iso,
        }

    narrative_payload = {
        "key_catalyst": str(synthetic.headline),
        "age_hours": 0.0,
        "expected_resolution_at": _isoformat(expected_resolution_at),
        "resolution_pnl_usd": None,
        "entry_fill_gap_usd": None,
        "components_metadata": components_metadata,
    }
    thesis_row = ThesisRow(
        thesis_id=thesis_id,
        position_id=position_id,
        status=ThesisRecordStatus.ACTIVE.value,
        resolution_timestamp=None,
        resolution_category=None,
        summary=str(synthetic.rationale),
        time_expectation_hours=time_expectation_hours,
        position_size_rationale=None,
        generation_timestamp=generation_iso,
        narrative_json=json.dumps(narrative_payload),
    )
    return thesis_row, tuple(component_rows)


def _build_cash_ledger_row(starting_cash_usd: float, *, now: datetime) -> CashLedgerRow:
    """Translate ``starting_cash_usd`` into the singleton ``cash_ledger`` row."""
    cash = Decimal(str(starting_cash_usd))
    zero = Decimal(0)
    return CashLedgerRow(
        id=CASH_LEDGER_SINGLETON_ID,
        current_cash_usd=cash,
        settled_cash_usd=cash,
        reserved_capital_usd=zero,
        available_buying_power_usd=cash,
        margin_held_usd=zero,
        unsettled_proceeds_json="[]",
        last_updated_at=_isoformat(now),
    )


def _build_drawdown_state_row(starting_cash_usd: float, *, now: datetime) -> DrawdownStateRow:
    """Seed the ``drawdown_state`` singleton at the synthetic-portfolio HWM.

    The snapshot assembler reads this singleton via
    :meth:`SqlRepository.get_drawdown_state`, which raises on absence (the
    fresh-DB fallback in :func:`alphamind.state.drawdown_reader.read_drawdown_state`
    is only used pre-snapshot for halt-state computation). Seeding it here
    keeps debug-e2e runnable from a fully wiped DB without depending on a
    prior Phase 1 write to bootstrap the row.
    """
    return DrawdownStateRow(
        id=DRAWDOWN_STATE_SINGLETON_ID,
        equity_high_water_mark_usd=float(starting_cash_usd),
        current_drawdown_pct=0.0,
        drawdown_duration_hours=0.0,
        lifetime_max_drawdown_pct=0.0,
        drawdown_by_source_json="{}",
        last_updated_at=_isoformat(now),
    )


async def wipe_and_seed(
    *,
    session: AsyncSession,
    now: datetime,
    db_path: str,
    portfolio: Any,
) -> None:
    """Wipe the nine state-persistence tables and seed the synthetic portfolio.

    Refuses to run unless ``db_path`` ends with ``-debug-e2e.db``. Raises
    :class:`RuntimeError` with the refusing path quoted in the message on
    guard violation.

    ``portfolio`` is typed :class:`typing.Any` because the
    ``SyntheticPortfolio`` dataclass ships in story 02b; this function
    accesses ``portfolio`` structurally (``portfolio.positions``,
    ``portfolio.theses``, ``portfolio.starting_cash_usd``) and the
    per-position attribute names documented in the design.
    """
    if not db_path.endswith(_DEBUG_DB_SUFFIX):
        msg = f"refusing to wipe non-debug DB path {db_path!r}"
        raise RuntimeError(msg)

    # Defer FK enforcement until commit so the wipe can DELETE the
    # state-persistence tables in any order despite the orders↔brackets
    # circular FK (and any other deferred edges the schema may add). At
    # commit time every wiped table is empty and every seed row's parents
    # exist, so FK validation passes cleanly. This is the SQLite-specific
    # ``defer_foreign_keys`` PRAGMA (scoped to the current transaction);
    # it does NOT disable enforcement globally.
    await session.execute(text("PRAGMA defer_foreign_keys = ON"))

    for table in _WIPE_ORDER:
        await session.execute(delete(table))

    for index, synthetic in enumerate(portfolio.positions):
        session.add(_build_position_row(synthetic, position_index=index, now=now))

    for synthetic_thesis in portfolio.theses:
        thesis_row, component_rows = _build_thesis_rows(synthetic_thesis, now=now)
        session.add(thesis_row)
        for component_row in component_rows:
            session.add(component_row)

    session.add(_build_cash_ledger_row(portfolio.starting_cash_usd, now=now))
    session.add(_build_drawdown_state_row(portfolio.starting_cash_usd, now=now))

    await session.commit()
