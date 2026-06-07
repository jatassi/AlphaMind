"""Synthetic-portfolio seeder for ``--debug-e2e`` mode (story 01c / ALP-496).

``wipe_and_seed`` wipes the state-persistence tables enumerated in
``_WIPE_ORDER`` and seeds the synthetic portfolio (positions + theses +
cash-ledger singleton) via the canonical SQLAlchemy ORM models.

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

from alphamind._kernel.ids import AlpacaOrderId
from alphamind.persistence.models import (
    Base,
    Brief,
    RegimeAdaptationStateRow,
    TickerRealizedVolRow,
)
from alphamind.portfolio_state.records.orders import (
    BracketLegEnforcement,
    BracketLegStatus,
    BracketLegType,
    BracketStatus,
    EnforcementBinding,
    OrderClass,
    OrderDirection,
    OrderDuration,
    OrderRole,
    OrderStatus,
    OrderType,
)
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

# Standard listed-option contract multiplier — applied to every synthetic
# option and strategy leg.
_OPTION_CONTRACT_MULTIPLIER = 100.0

# A ``SyntheticStrategyLeg`` carries no per-leg premium, so the seeder
# synthesizes one: SHORT legs are marked at this fixed positive premium and
# LONG legs absorb the remainder (see :func:`_strategy_leg_premiums`).
_STRATEGY_SHORT_LEG_MARK_USD = 1.0


# State-persistence tables wiped at the start of every debug-e2e
# invocation, in FK-safe order — children before parents.
#
# The snapshot-from-prod workflow (``scripts/snapshot_prod_for_debug_e2e.py``)
# means the bootstrap DB carries real production rows for every table below,
# so the wipe list MUST cover all state-persistence tables — not just the
# narrower set the original design enumerated when the seeder ran against a
# truly fresh DB. Tables managed by collectors / the data layer
# (``asset_universe``, distillation calibration, news, event calendar, ...)
# are deliberately preserved by the snapshot and are NOT wiped here.
#
# ``ticker_realized_vol`` is the one entry that is not itself
# state-persistence: it is distillation output, but each row carries a
# NOT-NULL FK to the producing ``invocations`` row, so wiping
# ``invocations`` without it leaves orphans that fail the commit-time FK
# check. Distillation regenerates it every run, so wiping it is harmless
# (ALP-616).
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
    TickerRealizedVolRow,
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
        "contract_multiplier": _OPTION_CONTRACT_MULTIPLIER,
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


def _strategy_leg_premiums(legs: tuple[Any, ...], net_premium: float) -> list[float]:
    """Return each leg's ``premium_paid_per_contract`` for a synthetic strategy.

    A ``SyntheticStrategyLeg`` carries no per-leg premium, and debug-e2e seeds
    no option-price snapshots for the synthetic strikes — so the snapshot
    assembler marks each leg to its ``premium_paid_per_contract`` fallback.
    Choosing those premiums so the *direction-aware* leg sum equals
    ``net_premium`` makes a freshly-placed spread mark to its cost basis
    (≈ $0 P/L), matching every other "just placed" synthetic position. An even
    split instead nets a debit spread to ≈ $0 market value against a non-zero
    cost basis — the -100% P/L artifact on debug-pos-07 (ALP-582).

    SHORT legs take a fixed positive mark; LONG legs split the remainder. Every
    premium stays > 0 — ``PriceQuote.__post_init__`` rejects ``price_usd <= 0``.
    This requires a net *debit* (``net_premium >= 0``); a net-credit strategy
    has no positive long-leg premium under this scheme and is rejected.
    """
    directions = [Direction(leg.direction.value) for leg in legs]
    units = [float(leg.contracts) * _OPTION_CONTRACT_MULTIPLIER for leg in legs]
    long_units = sum(u for u, d in zip(units, directions, strict=True) if d is Direction.LONG)
    short_units = sum(u for u, d in zip(units, directions, strict=True) if d is Direction.SHORT)
    if long_units == 0.0:
        msg = "synthetic strategy must carry at least one LONG leg"
        raise ValueError(msg)
    long_premium = (net_premium + _STRATEGY_SHORT_LEG_MARK_USD * short_units) / long_units
    if long_premium <= 0.0:
        msg = (
            "net-credit synthetic strategy is unsupported: the even-mark scheme "
            f"yields a non-positive long-leg premium ({long_premium}); "
            "net_premium must be a debit (>= 0)"
        )
        raise ValueError(msg)
    return [
        long_premium if d is Direction.LONG else _STRATEGY_SHORT_LEG_MARK_USD for d in directions
    ]


def _build_strategy_position_row(
    synthetic: Any,
    *,
    position_id: str,
    now: datetime,
) -> PositionRow:
    expiration = (now + timedelta(days=int(synthetic.expiration_offset_days))).date()
    legs_payload = []
    leg_premiums = _strategy_leg_premiums(synthetic.legs, float(synthetic.net_premium))
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
                    "contract_multiplier": _OPTION_CONTRACT_MULTIPLIER,
                    "premium_paid_per_contract": leg_premiums[index],
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
        # A multi-leg strategy has no position-level direction (ALP-610) —
        # the column is NULL; per-leg direction lives in ``details_json``.
        direction=None,
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


def _bracket_id_for_position(position_index: int) -> str:
    return f"debug-bracket-{position_index:02d}"


def _entry_order_id_for_position(position_index: int) -> str:
    return f"debug-entry-order-{position_index:02d}"


def _build_entry_order_row(
    *,
    position_row: PositionRow,
    order_id: str,
    bracket_id: str,
    now: datetime,
) -> OrderRow:
    """Synthesize the entry order each synthetic position is paired with.

    The synthetic portfolio is shaped like a post-fill snapshot — every
    position represents an entry that already filled — so the entry order
    rides at ``FILLED`` status with the position's directional sign
    encoded as the order direction. The position's
    ``execution_history_json`` is left empty (the seed does not fabricate
    a matching ``fill_records`` row); the close write-path in
    :mod:`alphamind.execution.write_paths.command_execution.close` reads only
    ``position.share_count`` / ``position.contract_count`` to size the
    close, so a missing fill record doesn't affect command persistence.

    A strategy row carries ``direction = NULL`` (ALP-610) — it has no
    position-level side — so its entry order takes an inert ``BUY``
    placeholder; the order-level direction is not a meaningful side for a
    strategy (strategy order-level direction is the separate ALP-614 follow-on).
    """
    timestamp = _isoformat(now)
    is_short = position_row.direction == Direction.SHORT.value
    order_direction = OrderDirection.SELL if is_short else OrderDirection.BUY
    return OrderRow(
        order_id=order_id,
        position_id=position_row.position_id,
        bracket_id=bracket_id,
        order_role=OrderRole.ENTRY.value,
        order_class=OrderClass.SIMPLE.value,
        instrument_spec_json=position_row.details_json,
        direction=order_direction.value,
        order_type=OrderType.MARKET.value,
        quantity=1.0,
        price_parameters_json="{}",
        duration=OrderDuration.DAY.value,
        status=OrderStatus.FILLED.value,
        # A FILLED entry had a real broker order — carry a deterministic
        # broker-style id (ALP-847 deleted the synthetic ``alp-`` placeholder).
        alpaca_order_id=AlpacaOrderId(f"brk-{order_id}"),
        alpaca_order_id_chain_json=f'["brk-{order_id}"]',
        submission_timestamp=timestamp,
        last_update_timestamp=timestamp,
        filled_quantity=1.0,
        average_fill_price=None,
        remaining_quantity=0.0,
        modification_count=0,
        metadata_json=(
            '{"originating_thesis_id":null,"originating_pm_command_id":null,"age_hours":0.0}'
        ),
    )


def _build_entry_bracket_row(
    *,
    bracket_id: str,
    position_id: str,
    entry_order_id: str,
) -> BracketRow:
    """Bracket parent for a synthetic position's entry order — ``ACTIVE`` status."""
    return BracketRow(
        bracket_id=bracket_id,
        position_id=position_id,
        status=BracketStatus.ACTIVE.value,
        entry_order_id=entry_order_id,
        entry_window_deadline=None,
        corporate_action_cancellation_reason=None,
        modification_history_json="[]",
    )


def _build_time_expiration_leg_row(
    *,
    bracket_id: str,
    now: datetime,
) -> BracketLegRow:
    """Synthesize one ``TIME_EXPIRATION`` mechanical leg per bracket.

    :class:`BracketRecord` validation requires ``protective_legs`` to be
    non-empty and to carry at least one MECHANICAL leg in
    ``{TAKE_PROFIT, PRICE_STOP, TIME_EXPIRATION}`` (the hard-backstop
    requirement in ``portfolio_state/records/orders.py``). A
    ``TIME_EXPIRATION`` leg with a future deadline is the simplest
    satisfying option — no order_id, no underlying ticker, no pricing
    needed; the trigger payload is just a timestamp.

    Single leg per bracket: the seed needs validity, not richness.
    Real production brackets carry the analyst's full leg set; the
    debug-e2e seed only requires the FK + invariant minimum.
    """
    deadline = now + timedelta(days=30)
    return BracketLegRow(
        bracket_leg_id=f"{bracket_id}-leg-0",
        bracket_id=bracket_id,
        leg_index=0,
        leg_type=BracketLegType.TIME_EXPIRATION.value,
        order_id=None,
        trigger_kind="TIME",
        trigger_payload_json=json.dumps({"trigger_type": "time", "deadline": deadline.isoformat()}),
        pl_anchor_json=None,
        enforcement=BracketLegEnforcement.MECHANICAL.value,
        enforcement_binding=EnforcementBinding.MONITOR_ENFORCED.value,
        leg_status=BracketLegStatus.ACTIVE.value,
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


def _build_drawdown_state_row(starting_cash_usd: float, *, now: datetime) -> DrawdownStateRow:
    """Seed the ``drawdown_state`` singleton at the synthetic-portfolio HWM.

    The snapshot assembler reads this singleton via
    :meth:`SqlRepository.get_drawdown_state`, which raises on absence (the
    fresh-DB fallback in :func:`alphamind.state.drawdown_reader.read_drawdown_state`
    is only used pre-snapshot for halt-state computation). Seeding it here
    keeps debug-e2e runnable from a fully wiped DB without depending on a
    prior fill-collection write to bootstrap the row.
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
    """Wipe the ``_WIPE_ORDER`` tables and seed the synthetic portfolio.

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
        position_row = _build_position_row(synthetic, position_index=index, now=now)
        bracket_id = _bracket_id_for_position(index)
        entry_order_id = _entry_order_id_for_position(index)
        # Link the position into its bracket + thesis cluster. The
        # synthetic ``_build_position_row`` helpers leave ``bracket_id`` /
        # ``thesis_id`` as ``None`` because they don't know the cluster
        # ids; we fill them in here so the post-fill snapshot the seeder
        # produces is internally consistent (every position references a
        # real bracket and a real thesis).
        position_row.bracket_id = bracket_id
        position_row.thesis_id = f"debug-thesis-{index:02d}"
        session.add(position_row)
        # Cyclic FKs (orders.bracket_id ↔ brackets.entry_order_id) are
        # deferred until commit, so the insertion order here doesn't
        # matter — both rows exist by the time the FK check fires.
        session.add(
            _build_entry_order_row(
                position_row=position_row,
                order_id=entry_order_id,
                bracket_id=bracket_id,
                now=now,
            )
        )
        session.add(
            _build_entry_bracket_row(
                bracket_id=bracket_id,
                position_id=position_row.position_id,
                entry_order_id=entry_order_id,
            )
        )
        session.add(_build_time_expiration_leg_row(bracket_id=bracket_id, now=now))

    for synthetic_thesis in portfolio.theses:
        thesis_row, component_rows = _build_thesis_rows(synthetic_thesis, now=now)
        session.add(thesis_row)
        for component_row in component_rows:
            session.add(component_row)

    session.add(_build_cash_ledger_row(portfolio.starting_cash_usd, now=now))
    session.add(_build_drawdown_state_row(portfolio.starting_cash_usd, now=now))

    await session.commit()
