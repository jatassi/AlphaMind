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

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from alphamind.persistence.models import Base
from alphamind.portfolio_state.records.positions import (
    Direction,
    InstrumentType,
    OptionContractType,
    PositionStatus,
)
from alphamind.portfolio_state.records.theses import ThesisRecordStatus
from alphamind.state.tables import (
    ActivityLogRow,
    BracketLegRow,
    BracketRow,
    CashLedgerRow,
    FillRecordRow,
    InvocationRow,
    PositionRow,
    ProcessLifetimeRow,
    ThesisRow,
)
from alphamind.state.tables.cash_ledger import CASH_LEDGER_SINGLETON_ID

_DEBUG_DB_SUFFIX = "-debug-e2e.db"


# Tables wiped in FK-safe order — children before parents.
#
# The list is the explicit 9 enumerated in the design (§ 6.3) and
# parent issue ALP-493 pre-resolved decision § (L). The order maps to
# the design's logical names: PositionFills → FillRecordRow,
# PositionTheses → ThesisRow.
_WIPE_ORDER: tuple[type[Base], ...] = (
    ActivityLogRow,
    BracketLegRow,
    BracketRow,
    FillRecordRow,
    ThesisRow,
    PositionRow,
    CashLedgerRow,
    InvocationRow,
    ProcessLifetimeRow,
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
                    "premium_paid_per_contract": 0.0,
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


def _build_thesis_row(synthetic: Any, *, now: datetime) -> ThesisRow:
    """Translate a ``SyntheticThesis`` into a ``ThesisRow``.

    The synthetic thesis is a flat ``(position_index, headline, rationale)``
    triple; the ``ThesisRow`` carries a richer narrative-JSON payload that
    the codec round-trip reads (see
    :func:`alphamind.state.tables.theses_codec.rows_to_record`). We stamp
    the minimum fields needed for SELECT-shape tests against the seeded
    DB to succeed.
    """
    thesis_id = f"debug-thesis-{synthetic.position_index:02d}"
    position_id = f"debug-pos-{synthetic.position_index:02d}"
    time_expectation_hours = 24.0
    expected_resolution_at = now + timedelta(hours=time_expectation_hours)
    narrative_payload = {
        "key_catalyst": str(synthetic.headline),
        "age_hours": 0.0,
        "expected_resolution_at": _isoformat(expected_resolution_at),
        "resolution_pnl_usd": None,
        "entry_fill_gap_usd": None,
        "components_metadata": {},
    }
    return ThesisRow(
        thesis_id=thesis_id,
        position_id=position_id,
        status=ThesisRecordStatus.ACTIVE.value,
        resolution_timestamp=None,
        resolution_category=None,
        summary=str(synthetic.rationale),
        time_expectation_hours=time_expectation_hours,
        position_size_rationale=None,
        generation_timestamp=_isoformat(now),
        narrative_json=json.dumps(narrative_payload),
    )


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

    for table in _WIPE_ORDER:
        await session.execute(delete(table))

    for index, synthetic in enumerate(portfolio.positions):
        session.add(_build_position_row(synthetic, position_index=index, now=now))

    for synthetic_thesis in portfolio.theses:
        session.add(_build_thesis_row(synthetic_thesis, now=now))

    session.add(_build_cash_ledger_row(portfolio.starting_cash_usd, now=now))

    await session.commit()
