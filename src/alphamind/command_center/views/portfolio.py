"""Portfolio views — dashboard + position detail + theses (ALP-676, ALP-677, ALP-678).

Dashboard (ALP-676) — GET /api/views/portfolio/dashboard — five panes:

* ``equity_and_pl``    — total value, HWM, drawdown, daily/cumulative/total
                          unrealized P/L from ``drawdown_state`` +
                          ``cash_ledger`` + ``positions``.
* ``cash_and_capital`` — cash, settled cash, reserved capital, buying power,
                          margin held, unsettled proceeds, Reg T excess
                          trailing windows from ``cash_ledger`` +
                          ``fill_records.regt_attribution_json``.
* ``exposure``         — gross/net/sector/delta-adjusted rollup from
                          ``positions`` (aggregated inline; the full snapshot
                          assembler is not invoked — the view is read-only).
* ``positions``        — one row per ``OPEN`` position joined to ``theses``.
* ``pending_orders``   — one row per ``PENDING`` / ``PARTIALLY_FILLED`` order.

Position detail (ALP-677) — GET /api/views/portfolio/positions/{position_id}:

* position row fields
* bracket legs for the position's bracket
* fill history (fills joined through the position's orders)
* linked thesis with all components
* activity_log entries filtered by position_id

Theses (ALP-678):

* GET /api/views/portfolio/theses  — paginated filterable theses list.
* GET /api/views/portfolio/theses/{thesis_id} — full thesis detail with
  components, status history from activity_log, resolution outcome.

Design references:
  docs/design/command-center.md § Portfolio dashboard
  docs/design/command-center.md § Position detail
  docs/design/command-center.md § Theses dashboard
  docs/design/command-center.md § Thesis detail
  docs/design/05-execution-layer/thesis-model.md
  docs/design/05-execution-layer/regt-margin-attribution.md
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# ---------------------------------------------------------------------------
# Pydantic response models (Pydantic at boundaries per ALP-128 invariant)
# ---------------------------------------------------------------------------


class UnsettledProceedsItem(BaseModel):
    """Single in-flight settlement entry."""

    settlement_date: str
    amount_usd: str  # Decimal as string for JSON round-trip fidelity
    source_transaction_id: str


class EquityAndPL(BaseModel):
    """Equity and P/L pane."""

    total_value_usd: str
    high_water_mark_usd: str
    current_drawdown_pct: float
    daily_realized_pl_usd: str
    cumulative_realized_pl_usd: str
    total_unrealized_pl_usd: str


class RegTExcess(BaseModel):
    """Reg T excess trailing windows."""

    trailing_30d_usd: str
    trailing_90d_usd: str
    lifetime_usd: str


class CashAndCapital(BaseModel):
    """Cash and capital pane."""

    cash_usd: str
    settled_cash_usd: str
    reserved_capital_usd: str
    available_buying_power_usd: str
    margin_held_usd: str
    unsettled_proceeds: list[UnsettledProceedsItem]
    regt_excess: RegTExcess


class SectorExposureItem(BaseModel):
    """Per-sector exposure breakdown."""

    sector: str
    long_market_value_usd: str
    short_market_value_usd: str


class Exposure(BaseModel):
    """Exposure pane."""

    gross_exposure_pct: float
    net_long_pct: float
    net_short_pct: float
    sector_breakdown: list[SectorExposureItem]
    delta_adjusted_net_usd: str


class PositionRow(BaseModel):
    """Single row in the positions table."""

    position_id: str
    ticker: str
    instrument_type: str
    direction: str | None
    quantity: float
    market_value_usd: str
    unrealized_pl_usd: str
    thesis_status: str | None
    age_hours: float
    distance_to_target_pct: float | None
    distance_to_nearest_invalidation_pct: float | None


class PendingOrderRow(BaseModel):
    """Single row in the pending orders table."""

    order_id: str
    status: str
    instrument_spec: dict[str, Any]
    order_type: str
    order_role: str
    quantity: float
    filled_quantity: float
    remaining_quantity: float
    direction: str | None
    age_hours: float
    position_id: str | None


class PortfolioDashboard(BaseModel):
    """Full portfolio dashboard response."""

    equity_and_pl: EquityAndPL
    cash_and_capital: CashAndCapital
    exposure: Exposure
    positions: list[PositionRow]
    pending_orders: list[PendingOrderRow]


# ---------------------------------------------------------------------------
# Internal read helpers (frozen dataclasses, not at API boundary)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _DrawdownRow:
    equity_high_water_mark_usd: float
    current_drawdown_pct: float


@dataclass(frozen=True, slots=True)
class _CashRow:
    current_cash_usd: Decimal
    settled_cash_usd: Decimal
    reserved_capital_usd: Decimal
    available_buying_power_usd: Decimal
    margin_held_usd: Decimal
    unsettled_proceeds_json: str


# ---------------------------------------------------------------------------
# Regt excess computation
# ---------------------------------------------------------------------------

_NOW_FMT = "%Y-%m-%dT%H:%M:%S"


def _safe_json_dict(raw: str | None) -> dict[str, Any]:
    """Parse ``raw`` as JSON and return it iff it's a dict; else ``{}``.

    Centralizes the "JSON parse + isinstance(dict) guard" pattern: callers
    that ``json.loads(...)`` then immediately ``.get(key)`` would otherwise
    raise :exc:`AttributeError` on a list / scalar / null JSON value past
    the outer try block (which only catches :exc:`JSONDecodeError` /
    :exc:`TypeError` on the loads itself). Mostly a defensive wrapper —
    every shipping payload here is a dict — but the JSON columns are
    TEXT and a future legacy migration / hand-edit can land a non-dict.
    """
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(obj, dict):
        return {}
    return obj


def _parse_fill_ts(ts: str) -> datetime | None:
    """Parse ISO-8601 fill timestamp; return None on parse failure.

    Tries tz-aware formats first; falls back to naive formats and
    attaches UTC so every returned datetime is tz-aware.
    """
    # Tz-aware formats — strptime %z handles "+00:00" and "Z" suffixes
    # in Python 3.11+; the explicit "Z" variants cover older Pythons.
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        with contextlib.suppress(ValueError):
            return datetime.strptime(ts, fmt)  # noqa: DTZ007 — %z present
    # Naive formats — replace tzinfo to UTC so callers always get tz-aware.
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
    ):
        with contextlib.suppress(ValueError):
            naive = datetime.strptime(ts, fmt)  # noqa: DTZ007 — UTC applied below
            return naive.replace(tzinfo=UTC)
    return None


async def _compute_regt_excess(session: AsyncSession) -> RegTExcess:
    """Aggregate regt_excess_over_pm from fill_records.regt_attribution_json.

    Uses a single-query json_extract per regt-margin-attribution.md:
    ``regt_attribution_json`` is a JSON blob with ``regt_excess_over_pm``
    and the fill's ``fill_timestamp`` column is used for window filtering.
    Fills without the attribution blob contribute zero (pre-module fills).
    """
    result = await session.execute(
        text("""
            SELECT
                fill_timestamp,
                json_extract(regt_attribution_json, '$.regt_excess_over_pm') AS excess
            FROM fill_records
            WHERE regt_attribution_json IS NOT NULL
        """)
    )
    rows = result.fetchall()

    now = datetime.now(UTC)
    cutoff_30d = now - timedelta(days=30)
    cutoff_90d = now - timedelta(days=90)

    total_30d = Decimal(0)
    total_90d = Decimal(0)
    total_lifetime = Decimal(0)

    for fill_ts_str, excess_raw in rows:
        if excess_raw is None:
            continue
        try:
            excess = Decimal(str(excess_raw))
        except Exception:
            continue

        total_lifetime += excess

        fill_dt = _parse_fill_ts(fill_ts_str)
        if fill_dt is not None:
            if fill_dt >= cutoff_90d:
                total_90d += excess
            if fill_dt >= cutoff_30d:
                total_30d += excess

    return RegTExcess(
        trailing_30d_usd=str(total_30d),
        trailing_90d_usd=str(total_90d),
        lifetime_usd=str(total_lifetime),
    )


# ---------------------------------------------------------------------------
# Pane readers
# ---------------------------------------------------------------------------


async def _read_equity_and_pl(session: AsyncSession) -> EquityAndPL:
    """Read equity + P/L pane from drawdown_state, cash_ledger, positions."""
    # Drawdown state — HWM + current drawdown.
    dd_result = await session.execute(
        text("SELECT equity_high_water_mark_usd, current_drawdown_pct FROM drawdown_state LIMIT 1")
    )
    dd_row = dd_result.fetchone()
    hwm = Decimal(str(dd_row[0])) if dd_row else Decimal(0)
    drawdown_pct = float(dd_row[1]) if dd_row else 0.0

    # Cash balance (contributes to total value).
    cash_result = await session.execute(text("SELECT current_cash_usd FROM cash_ledger LIMIT 1"))
    cash_row = cash_result.fetchone()
    cash = Decimal(str(cash_row[0])) if cash_row else Decimal(0)

    # Positions — sum unrealized P/L and market values for OPEN positions.
    pos_result = await session.execute(
        text("""
            SELECT
                realized_pnl_to_date_usd,
                details_json,
                entry_timestamp
            FROM positions
            WHERE status = 'OPEN'
        """)
    )
    pos_rows = pos_result.fetchall()

    total_unrealized = Decimal(0)
    total_market_value = Decimal(0)
    daily_realized = Decimal(0)
    cumulative_realized = Decimal(0)

    today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    for realized_raw, details_json_str, entry_ts_str in pos_rows:
        # Accumulate realized P/L.
        if realized_raw is not None:
            realized = Decimal(str(realized_raw))
            cumulative_realized += realized
            # Attribution of daily vs cumulative: the entry timestamp can't
            # split intraday, so daily_realized = sum of P/L for positions
            # entered today. Full daily realized P/L from an activity log
            # would be authoritative but is not scope for this view; we
            # use realized_pnl_to_date_usd as a proxy.
            if entry_ts_str is not None:
                entry_dt = _parse_fill_ts(entry_ts_str)
                if entry_dt is not None and entry_dt >= today_start:
                    daily_realized += realized

        # Extract market value and unrealized P/L from details_json.
        details = _safe_json_dict(details_json_str)

        mv_raw = details.get("market_value_usd") or details.get("current_market_value_usd")
        if mv_raw is not None:
            with contextlib.suppress(Exception):
                total_market_value += Decimal(str(mv_raw))

        unreal_raw = details.get("unrealized_pnl_usd") or details.get("unrealized_pl_usd")
        if unreal_raw is not None:
            with contextlib.suppress(Exception):
                total_unrealized += Decimal(str(unreal_raw))

    total_value = cash + total_market_value

    return EquityAndPL(
        total_value_usd=str(total_value),
        high_water_mark_usd=str(hwm),
        current_drawdown_pct=drawdown_pct,
        daily_realized_pl_usd=str(daily_realized),
        cumulative_realized_pl_usd=str(cumulative_realized),
        total_unrealized_pl_usd=str(total_unrealized),
    )


async def _read_cash_and_capital(session: AsyncSession) -> CashAndCapital:
    """Read cash + capital pane from cash_ledger + fill_records."""
    cash_result = await session.execute(
        text("""
            SELECT
                current_cash_usd,
                settled_cash_usd,
                reserved_capital_usd,
                available_buying_power_usd,
                margin_held_usd,
                unsettled_proceeds_json
            FROM cash_ledger
            LIMIT 1
        """)
    )
    cash_row = cash_result.fetchone()

    if cash_row is None:
        unsettled: list[UnsettledProceedsItem] = []
        regt = await _compute_regt_excess(session)
        return CashAndCapital(
            cash_usd="0",
            settled_cash_usd="0",
            reserved_capital_usd="0",
            available_buying_power_usd="0",
            margin_held_usd="0",
            unsettled_proceeds=unsettled,
            regt_excess=regt,
        )

    raw_unsettled_json = cash_row[5]
    try:
        parsed_unsettled: Any = json.loads(raw_unsettled_json) if raw_unsettled_json else []
    except (json.JSONDecodeError, TypeError):
        parsed_unsettled = []
    unsettled_raw: list[dict[str, Any]] = (
        parsed_unsettled if isinstance(parsed_unsettled, list) else []
    )

    unsettled_items = []
    for entry in unsettled_raw:
        if not isinstance(entry, dict):
            continue
        sd = entry.get("settlement_date") or entry.get("date") or ""
        if isinstance(sd, datetime):
            sd = sd.isoformat()
        amt = entry.get("amount_usd", 0)
        src = entry.get("source_transaction_id") or entry.get("transaction_id") or ""
        unsettled_items.append(
            UnsettledProceedsItem(
                settlement_date=str(sd),
                amount_usd=str(Decimal(str(amt))),
                source_transaction_id=str(src),
            )
        )

    regt = await _compute_regt_excess(session)

    return CashAndCapital(
        cash_usd=str(Decimal(str(cash_row[0]))),
        settled_cash_usd=str(Decimal(str(cash_row[1]))),
        reserved_capital_usd=str(Decimal(str(cash_row[2]))),
        available_buying_power_usd=str(Decimal(str(cash_row[3]))),
        margin_held_usd=str(Decimal(str(cash_row[4]))),
        unsettled_proceeds=unsettled_items,
        regt_excess=regt,
    )


async def _read_exposure(session: AsyncSession) -> Exposure:
    """Read exposure pane from open positions."""
    pos_result = await session.execute(
        text("""
            SELECT direction, details_json
            FROM positions
            WHERE status = 'OPEN'
        """)
    )
    pos_rows = pos_result.fetchall()

    total_long = Decimal(0)
    total_short = Decimal(0)
    delta_adj_net = Decimal(0)
    sector_long: dict[str, Decimal] = {}
    sector_short: dict[str, Decimal] = {}

    for direction, details_json_str in pos_rows:
        details = _safe_json_dict(details_json_str)

        mv_raw = details.get("market_value_usd") or details.get("current_market_value_usd") or "0"
        try:
            mv = Decimal(str(mv_raw))
        except Exception:
            mv = Decimal(0)

        delta_raw = details.get("delta_adjusted_exposure_usd") or "0"
        try:
            delta = Decimal(str(delta_raw))
        except Exception:
            delta = Decimal(0)
        delta_adj_net += delta

        sector = str(details.get("sector") or "Unknown")

        if direction == "LONG":
            total_long += mv
            sector_long[sector] = sector_long.get(sector, Decimal(0)) + mv
        elif direction == "SHORT":
            total_short += abs(mv)
            sector_short[sector] = sector_short.get(sector, Decimal(0)) + abs(mv)

    gross = total_long + total_short
    # Portfolio-total placeholder: use gross as denominator when positive.
    denom = gross if gross > 0 else Decimal(1)

    gross_pct = float(gross / denom * 100) if gross > 0 else 0.0
    net_long_pct = float(total_long / denom * 100) if gross > 0 else 0.0
    net_short_pct = float(total_short / denom * 100) if gross > 0 else 0.0

    all_sectors = set(sector_long) | set(sector_short)
    sector_breakdown = [
        SectorExposureItem(
            sector=s,
            long_market_value_usd=str(sector_long.get(s, Decimal(0))),
            short_market_value_usd=str(sector_short.get(s, Decimal(0))),
        )
        for s in sorted(all_sectors)
    ]

    return Exposure(
        gross_exposure_pct=gross_pct,
        net_long_pct=net_long_pct,
        net_short_pct=net_short_pct,
        sector_breakdown=sector_breakdown,
        delta_adjusted_net_usd=str(delta_adj_net),
    )


def _position_row_from_db(
    position_id: str,
    instrument_type: str,
    direction: str | None,
    details_json_str: str,
    entry_ts_str: str | None,
    thesis_status: str | None,
    now: datetime,
) -> PositionRow:
    """Build a single PositionRow from raw DB columns."""
    details = _safe_json_dict(details_json_str)

    ticker = str(
        details.get("ticker") or details.get("underlying") or details.get("symbol") or "Unknown"
    )

    mv_raw = details.get("market_value_usd") or details.get("current_market_value_usd") or "0"
    mv = Decimal(0)
    with contextlib.suppress(Exception):
        mv = Decimal(str(mv_raw))

    unreal_raw = details.get("unrealized_pnl_usd") or details.get("unrealized_pl_usd") or "0"
    unreal = Decimal(0)
    with contextlib.suppress(Exception):
        unreal = Decimal(str(unreal_raw))

    qty = 0.0
    with contextlib.suppress(Exception):
        qty = float(details.get("quantity") or details.get("qty") or 0)

    age_hours = 0.0
    if entry_ts_str is not None:
        entry_dt = _parse_fill_ts(entry_ts_str)
        if entry_dt is not None:
            age_hours = (now - entry_dt).total_seconds() / 3600.0

    dist_target: float | None = None
    dist_inval: float | None = None
    if (raw_target := details.get("distance_to_target_pct")) is not None:
        with contextlib.suppress(Exception):
            dist_target = float(raw_target)
    if (raw_inval := details.get("distance_to_nearest_invalidation_pct")) is not None:
        with contextlib.suppress(Exception):
            dist_inval = float(raw_inval)

    return PositionRow(
        position_id=str(position_id),
        ticker=ticker,
        instrument_type=str(instrument_type),
        direction=str(direction) if direction else None,
        quantity=qty,
        market_value_usd=str(mv),
        unrealized_pl_usd=str(unreal),
        thesis_status=str(thesis_status) if thesis_status else None,
        age_hours=age_hours,
        distance_to_target_pct=dist_target,
        distance_to_nearest_invalidation_pct=dist_inval,
    )


async def _read_positions(session: AsyncSession) -> list[PositionRow]:
    """Read open positions joined to theses."""
    result = await session.execute(
        text("""
            SELECT
                p.position_id,
                p.instrument_type,
                p.direction,
                p.details_json,
                p.entry_timestamp,
                t.status AS thesis_status
            FROM positions p
            LEFT JOIN theses t ON t.position_id = p.position_id
            WHERE p.status = 'OPEN'
            ORDER BY p.entry_timestamp
        """)
    )
    rows = result.fetchall()
    now = datetime.now(UTC)

    return [
        _position_row_from_db(
            position_id=row[0],
            instrument_type=row[1],
            direction=row[2],
            details_json_str=row[3],
            entry_ts_str=row[4],
            thesis_status=row[5],
            now=now,
        )
        for row in rows
    ]


async def _read_pending_orders(session: AsyncSession) -> list[PendingOrderRow]:
    """Read pending + partially-filled orders."""
    result = await session.execute(
        text("""
            SELECT
                order_id,
                status,
                instrument_spec_json,
                order_type,
                order_role,
                quantity,
                filled_quantity,
                remaining_quantity,
                direction,
                submission_timestamp,
                position_id
            FROM orders
            WHERE status IN ('PENDING', 'PARTIALLY_FILLED')
            ORDER BY submission_timestamp
        """)
    )
    rows = result.fetchall()
    now = datetime.now(UTC)
    out: list[PendingOrderRow] = []

    for (
        order_id,
        order_status,
        instrument_spec_json,
        order_type,
        order_role,
        quantity,
        filled_quantity,
        remaining_quantity,
        direction,
        submission_ts_str,
        position_id,
    ) in rows:
        instrument_spec = _safe_json_dict(instrument_spec_json)

        age_hours = 0.0
        if submission_ts_str is not None:
            sub_dt = _parse_fill_ts(submission_ts_str)
            if sub_dt is not None:
                age_hours = (now - sub_dt).total_seconds() / 3600.0

        out.append(
            PendingOrderRow(
                order_id=str(order_id),
                status=str(order_status),
                instrument_spec=instrument_spec,
                order_type=str(order_type),
                order_role=str(order_role),
                quantity=float(quantity),
                filled_quantity=float(filled_quantity),
                remaining_quantity=float(remaining_quantity),
                direction=str(direction) if direction else None,
                age_hours=age_hours,
                position_id=str(position_id) if position_id else None,
            )
        )

    return out


# ---------------------------------------------------------------------------
# Position detail + theses — Pydantic response models (ALP-677, ALP-678)
# ---------------------------------------------------------------------------


class BracketLegDetail(BaseModel):
    """One bracket leg row for the position detail view."""

    bracket_leg_id: str
    leg_index: int
    leg_type: str
    trigger_kind: str
    trigger_payload: dict[str, Any]
    pl_anchor: dict[str, Any] | None
    enforcement: str
    leg_status: str
    order_id: str | None


class FillDetail(BaseModel):
    """One fill record row for the position detail view."""

    fill_id: str
    order_id: str
    fill_timestamp: str
    fill_price: str
    fill_quantity: float
    remaining_quantity_after: float
    order_status_after: str
    slippage_usd: str | None
    fees_usd: str
    execution_venue: str | None


class ThesisRow(BaseModel):
    """Single row in the theses dashboard list (ALP-678)."""

    thesis_id: str
    position_id: str
    status: str  # ACTIVE | RESOLVED | CANCELLED
    thesis_status: str | None  # ON_TRACK | PARTIALLY_REALIZED | AT_RISK | STALE | INVALIDATED
    resolution_category: str | None
    summary: str
    generation_timestamp: str
    resolution_timestamp: str | None
    age_hours: float
    position_unrealized_pl_usd: str | None


class ThesesPage(BaseModel):
    """Paginated theses list response (ALP-678)."""

    theses: list[ThesisRow]
    total: int
    page: int
    page_size: int


class ThesisComponentDetail(BaseModel):
    """One thesis component (ALP-677 / ALP-678).

    Shared by the position-detail (ALP-677) and thesis-detail (ALP-678)
    views. ``supporting_signals`` is populated from
    ``thesis_components.supporting_signals_json`` for position-detail
    payloads and defaults to ``[]`` for the thesis-detail view.
    """

    component_id: str
    component_type: str
    linked_bracket_leg: str | None
    instrument_reference: str | None
    narrative: str
    key_assumptions: list[str]
    supporting_signals: list[str] = []
    resolution_outcome: str | None
    resolution_notes: str | None


class PositionThesisDetail(BaseModel):
    """Thesis embedded in a position-detail payload (ALP-677 view C-2).

    Narrower than :class:`ThesisDetail` (ALP-678) because the
    position-detail view doesn't need the status history or per-component
    resolution-outcome map — those are surfaced by the thesis-detail
    endpoint instead.
    """

    thesis_id: str
    status: str
    summary: str
    time_expectation_hours: float | None
    position_size_rationale: str | None
    generation_timestamp: str
    resolution_timestamp: str | None
    resolution_category: str | None
    components: list[ThesisComponentDetail]


class ActivityLogEntry(BaseModel):
    """One activity log row for the position detail history tab."""

    entry_id: str
    invocation_id: str
    entry_at: str
    event_type: str
    event_group: str
    order_id: str | None
    thesis_id: str | None
    source: str
    detail_json: str


class PositionDetail(BaseModel):
    """Full position detail response (ALP-677 view C-2).

    Aggregates: position fields, bracket legs, fill history, thesis +
    components, activity_log entries filtered by position_id.
    """

    position_id: str
    ticker: str
    instrument_type: str
    direction: str | None
    status: str
    quantity: float
    market_value_usd: str
    unrealized_pl_usd: str
    realized_pl_usd: str | None
    age_hours: float
    distance_to_target_pct: float | None
    distance_to_nearest_invalidation_pct: float | None
    bracket_id: str | None
    bracket_legs: list[BracketLegDetail]
    fills: list[FillDetail]
    thesis: PositionThesisDetail | None
    activity_log: list[ActivityLogEntry]


# ---------------------------------------------------------------------------
# Position detail — internal read helpers (ALP-677)
# ---------------------------------------------------------------------------


async def _read_bracket_legs(
    session: AsyncSession,
    bracket_id: str,
) -> list[BracketLegDetail]:
    """Read all bracket legs for the given bracket_id ordered by leg_index."""
    result = await session.execute(
        text("""
            SELECT
                bracket_leg_id,
                leg_index,
                leg_type,
                trigger_kind,
                trigger_payload_json,
                pl_anchor_json,
                enforcement,
                leg_status,
                order_id
            FROM bracket_legs
            WHERE bracket_id = :bracket_id
            ORDER BY leg_index
        """),
        {"bracket_id": bracket_id},
    )
    rows = result.fetchall()
    legs: list[BracketLegDetail] = []
    for (
        bracket_leg_id,
        leg_index,
        leg_type,
        trigger_kind,
        trigger_payload_json,
        pl_anchor_json,
        enforcement,
        leg_status,
        order_id,
    ) in rows:
        trigger_payload = _safe_json_dict(trigger_payload_json)
        pl_anchor: dict[str, Any] | None = None
        if pl_anchor_json is not None:
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                candidate = json.loads(pl_anchor_json)
                if isinstance(candidate, dict):
                    pl_anchor = candidate
        legs.append(
            BracketLegDetail(
                bracket_leg_id=str(bracket_leg_id),
                leg_index=int(leg_index),
                leg_type=str(leg_type),
                trigger_kind=str(trigger_kind),
                trigger_payload=trigger_payload,
                pl_anchor=pl_anchor,
                enforcement=str(enforcement),
                leg_status=str(leg_status),
                order_id=str(order_id) if order_id is not None else None,
            )
        )
    return legs


async def _read_fills_for_position(
    session: AsyncSession,
    position_id: str,
) -> list[FillDetail]:
    """Read fill records joined through orders for the given position_id."""
    result = await session.execute(
        text("""
            SELECT
                fr.fill_id,
                fr.order_id,
                fr.fill_timestamp,
                fr.fill_price,
                fr.fill_quantity,
                fr.remaining_quantity_after,
                fr.order_status_after,
                fr.slippage_usd,
                fr.fees_usd,
                fr.execution_venue
            FROM fill_records fr
            INNER JOIN orders o ON o.order_id = fr.order_id
            WHERE o.position_id = :position_id
            ORDER BY fr.fill_timestamp
        """),
        {"position_id": position_id},
    )
    rows = result.fetchall()
    fills: list[FillDetail] = []
    for (
        fill_id,
        order_id,
        fill_timestamp,
        fill_price,
        fill_quantity,
        remaining_quantity_after,
        order_status_after,
        slippage_usd,
        fees_usd,
        execution_venue,
    ) in rows:
        fills.append(
            FillDetail(
                fill_id=str(fill_id),
                order_id=str(order_id),
                fill_timestamp=str(fill_timestamp),
                fill_price=str(fill_price),
                fill_quantity=float(fill_quantity),
                remaining_quantity_after=float(remaining_quantity_after),
                order_status_after=str(order_status_after),
                slippage_usd=str(slippage_usd) if slippage_usd is not None else None,
                fees_usd=str(fees_usd),
                execution_venue=str(execution_venue) if execution_venue is not None else None,
            )
        )
    return fills


def _parse_json_list(raw: str | None) -> list[str]:
    """Deserialize a JSON array column to a list of strings.

    Falls back to empty list on any parse error or non-array result.
    """
    if raw is None:
        return []
    with contextlib.suppress(json.JSONDecodeError, TypeError, AttributeError):
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    return []


async def _read_thesis_for_position(
    session: AsyncSession,
    position_id: str,
) -> PositionThesisDetail | None:
    """Read the thesis + all components for the given position_id.

    Returns ``None`` if no thesis exists for this position.
    """
    thesis_result = await session.execute(
        text("""
            SELECT
                thesis_id,
                status,
                summary,
                time_expectation_hours,
                position_size_rationale,
                generation_timestamp,
                resolution_timestamp,
                resolution_category
            FROM theses
            WHERE position_id = :position_id
            LIMIT 1
        """),
        {"position_id": position_id},
    )
    thesis_row = thesis_result.fetchone()
    if thesis_row is None:
        return None

    (
        thesis_id,
        status,
        summary,
        time_expectation_hours,
        position_size_rationale,
        generation_timestamp,
        resolution_timestamp,
        resolution_category,
    ) = thesis_row

    components_result = await session.execute(
        text("""
            SELECT
                component_id,
                component_type,
                linked_bracket_leg,
                instrument_reference,
                narrative,
                key_assumptions_json,
                supporting_signals_json,
                resolution_outcome,
                resolution_notes
            FROM thesis_components
            WHERE thesis_id = :thesis_id
            ORDER BY component_id
        """),
        {"thesis_id": thesis_id},
    )
    components_rows = components_result.fetchall()

    components: list[ThesisComponentDetail] = []
    for (
        component_id,
        component_type,
        linked_bracket_leg,
        instrument_reference,
        narrative,
        key_assumptions_json,
        supporting_signals_json,
        resolution_outcome,
        resolution_notes,
    ) in components_rows:
        components.append(
            ThesisComponentDetail(
                component_id=str(component_id),
                component_type=str(component_type),
                linked_bracket_leg=str(linked_bracket_leg) if linked_bracket_leg else None,
                instrument_reference=str(instrument_reference) if instrument_reference else None,
                narrative=str(narrative),
                key_assumptions=_parse_json_list(key_assumptions_json),
                supporting_signals=_parse_json_list(supporting_signals_json),
                resolution_outcome=str(resolution_outcome) if resolution_outcome else None,
                resolution_notes=str(resolution_notes) if resolution_notes else None,
            )
        )

    return PositionThesisDetail(
        thesis_id=str(thesis_id),
        status=str(status),
        summary=str(summary),
        time_expectation_hours=float(time_expectation_hours)
        if time_expectation_hours is not None
        else None,
        position_size_rationale=str(position_size_rationale) if position_size_rationale else None,
        generation_timestamp=str(generation_timestamp),
        resolution_timestamp=str(resolution_timestamp) if resolution_timestamp else None,
        resolution_category=str(resolution_category) if resolution_category else None,
        components=components,
    )


# ---------------------------------------------------------------------------
# Theses dashboard / detail — Pydantic response models (ALP-678)
# ---------------------------------------------------------------------------


class ThesisStatusTransition(BaseModel):
    """One entry in the status-history vertical timeline."""

    entry_id: str
    entry_at: str
    old_status: str
    new_status: str
    cited_reference_ids: list[str]  # reference-IDs extracted from detail_json


class ThesisDetail(BaseModel):
    """Full thesis detail — row + components + status history + resolution."""

    thesis_id: str
    position_id: str
    status: str
    thesis_status: str | None
    resolution_category: str | None
    summary: str
    generation_timestamp: str
    resolution_timestamp: str | None
    age_hours: float
    components: list[ThesisComponentDetail]
    status_history: list[ThesisStatusTransition]
    resolution_component_outcomes: dict[str, str]  # component_id → outcome


# ---------------------------------------------------------------------------
# Theses internal helpers (frozen dataclasses)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ThesisDbRow:
    thesis_id: str
    position_id: str
    status: str
    resolution_category: str | None
    summary: str
    generation_timestamp: str
    resolution_timestamp: str | None
    narrative_json: str


_REF_ID_PATTERN_PARTS = [
    "SA-",
    "QR-",
    "AR-",
    "CR-",
    "DR-",
    "INV-",
]


def _extract_reference_ids(detail_json_str: str) -> list[str]:
    """Extract reference-ID strings from a detail_json blob.

    Reference IDs follow the command-center naming scheme (SA-N, QR-N,
    AR-N, CR-N, etc.).  We look for them as string values anywhere in the
    JSON object and return deduplicated, order-preserving list.
    """
    try:
        obj: Any = json.loads(detail_json_str)
    except (json.JSONDecodeError, TypeError):
        return []

    ids: list[str] = []
    seen: set[str] = set()

    def _visit(value: Any) -> None:
        if isinstance(value, str):
            candidate = value.strip()
            if (
                any(candidate.startswith(prefix) for prefix in _REF_ID_PATTERN_PARTS)
                and candidate not in seen
            ):
                seen.add(candidate)
                ids.append(candidate)
        elif isinstance(value, dict):
            for v in value.values():
                _visit(v)
        elif isinstance(value, list):
            for item in value:
                _visit(item)

    _visit(obj)
    return ids


async def _read_theses_page(
    session: AsyncSession,
    *,
    status: str | None,
    classification: str | None,
    sector: str | None,
    age_days_gt: int | None,
    resolution_category: str | None,
    page: int,
    page_size: int,
) -> ThesesPage:
    """Read paginated theses list with filter dimensions."""
    now = datetime.now(UTC)

    # Build WHERE clauses dynamically.
    where_parts: list[str] = []
    params: dict[str, Any] = {}

    if status is not None:
        where_parts.append("t.status = :status")
        params["status"] = status.upper()

    if resolution_category is not None:
        where_parts.append("t.resolution_category = :resolution_category")
        params["resolution_category"] = resolution_category.upper()

    if age_days_gt is not None:
        # generation_timestamp is stored as ISO-8601 text; SQLite datetime()
        # comparison against a computed cutoff ISO string.
        cutoff = (now - timedelta(days=age_days_gt)).strftime("%Y-%m-%dT%H:%M:%S")
        where_parts.append("t.generation_timestamp <= :age_cutoff")
        params["age_cutoff"] = cutoff

    # ``classification`` maps to thesis_status (ON_TRACK, etc.) stored in the
    # narrative_json blob; use json_extract for the filter.
    if classification is not None:
        where_parts.append("json_extract(t.narrative_json, '$.thesis_status') = :classification")
        params["classification"] = classification.upper()

    # ``sector`` joins via position details_json.
    if sector is not None:
        where_parts.append("json_extract(p.details_json, '$.sector') = :sector")
        params["sector"] = sector

    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    count_result = await session.execute(
        text(f"""  -- noqa: S608
            SELECT COUNT(*)
            FROM theses t
            LEFT JOIN positions p ON p.position_id = t.position_id
            {where_sql}
        """),
        params,
    )
    total: int = count_result.scalar_one()

    offset = (page - 1) * page_size
    params["limit"] = page_size
    params["offset"] = offset

    rows_result = await session.execute(
        text(f"""  -- noqa: S608
            SELECT
                t.thesis_id,
                t.position_id,
                t.status,
                t.resolution_category,
                t.summary,
                t.generation_timestamp,
                t.resolution_timestamp,
                json_extract(t.narrative_json, '$.thesis_status') AS thesis_status,
                p.details_json AS pos_details_json
            FROM theses t
            LEFT JOIN positions p ON p.position_id = t.position_id
            {where_sql}
            ORDER BY t.generation_timestamp DESC
            LIMIT :limit OFFSET :offset
        """),
        params,
    )
    rows = rows_result.fetchall()

    result_rows: list[ThesisRow] = []
    for (
        thesis_id,
        position_id,
        db_status,
        db_resolution_category,
        summary,
        generation_timestamp,
        resolution_timestamp,
        thesis_status_raw,
        pos_details_json,
    ) in rows:
        age_hours = 0.0
        gen_dt = _parse_fill_ts(str(generation_timestamp))
        if gen_dt is not None:
            age_hours = (now - gen_dt).total_seconds() / 3600.0

        pos_unrealized: str | None = None
        if pos_details_json:
            with contextlib.suppress(Exception):
                pos_details = _safe_json_dict(pos_details_json)
                raw_unreal = pos_details.get("unrealized_pnl_usd") or pos_details.get(
                    "unrealized_pl_usd"
                )
                if raw_unreal is not None:
                    pos_unrealized = str(Decimal(str(raw_unreal)))

        result_rows.append(
            ThesisRow(
                thesis_id=str(thesis_id),
                position_id=str(position_id),
                status=str(db_status),
                thesis_status=str(thesis_status_raw) if thesis_status_raw else None,
                resolution_category=str(db_resolution_category) if db_resolution_category else None,
                summary=str(summary),
                generation_timestamp=str(generation_timestamp),
                resolution_timestamp=str(resolution_timestamp) if resolution_timestamp else None,
                age_hours=age_hours,
                position_unrealized_pl_usd=pos_unrealized,
            )
        )

    return ThesesPage(
        theses=result_rows,
        total=total,
        page=page,
        page_size=page_size,
    )


def _parse_key_assumptions(key_assumptions_json: str | None) -> list[str]:
    """Parse key_assumptions_json into a flat list of strings."""
    try:
        raw: Any = json.loads(key_assumptions_json or "[]")
        if not isinstance(raw, list):
            return []
        out: list[str] = []
        for item in raw:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                out.append(str(item.get("text") or item.get("assumption") or item))
    except (json.JSONDecodeError, TypeError):
        return []
    else:
        return out


async def _fetch_thesis_components(
    session: AsyncSession,
    thesis_id: str,
) -> tuple[list[ThesisComponentDetail], dict[str, str]]:
    """Fetch thesis_components rows; return (components, resolution_outcomes)."""
    comp_result = await session.execute(
        text("""
            SELECT
                component_id,
                component_type,
                linked_bracket_leg,
                instrument_reference,
                narrative,
                key_assumptions_json,
                resolution_outcome,
                resolution_notes
            FROM thesis_components
            WHERE thesis_id = :thesis_id
            ORDER BY component_type, component_id
        """),
        {"thesis_id": thesis_id},
    )
    comp_rows = comp_result.fetchall()

    components: list[ThesisComponentDetail] = []
    resolution_component_outcomes: dict[str, str] = {}

    for (
        comp_id,
        comp_type,
        linked_bracket_leg,
        instrument_reference,
        narrative,
        key_assumptions_json,
        resolution_outcome,
        resolution_notes,
    ) in comp_rows:
        flat_assumptions = _parse_key_assumptions(key_assumptions_json)

        if resolution_outcome:
            resolution_component_outcomes[str(comp_id)] = str(resolution_outcome)

        components.append(
            ThesisComponentDetail(
                component_id=str(comp_id),
                component_type=str(comp_type),
                linked_bracket_leg=str(linked_bracket_leg) if linked_bracket_leg else None,
                instrument_reference=str(instrument_reference) if instrument_reference else None,
                narrative=str(narrative),
                key_assumptions=flat_assumptions,
                resolution_outcome=str(resolution_outcome) if resolution_outcome else None,
                resolution_notes=str(resolution_notes) if resolution_notes else None,
            )
        )

    return components, resolution_component_outcomes


async def _read_activity_log_for_position(
    session: AsyncSession,
    position_id: str,
) -> list[ActivityLogEntry]:
    """Read activity_log rows filtered by position_id, newest first."""
    result = await session.execute(
        text("""
            SELECT
                entry_id,
                invocation_id,
                entry_at,
                event_type,
                event_group,
                order_id,
                thesis_id,
                source,
                detail_json
            FROM activity_log
            WHERE position_id = :position_id
            ORDER BY entry_at DESC
        """),
        {"position_id": position_id},
    )
    rows = result.fetchall()
    entries: list[ActivityLogEntry] = []
    for (
        entry_id,
        invocation_id,
        entry_at,
        event_type,
        event_group,
        order_id,
        thesis_id,
        source,
        detail_json,
    ) in rows:
        entries.append(
            ActivityLogEntry(
                entry_id=str(entry_id),
                invocation_id=str(invocation_id),
                entry_at=str(entry_at),
                event_type=str(event_type),
                event_group=str(event_group),
                order_id=str(order_id) if order_id is not None else None,
                thesis_id=str(thesis_id) if thesis_id is not None else None,
                source=str(source),
                detail_json=str(detail_json),
            )
        )
    return entries


async def _read_position_detail(
    session: AsyncSession,
    position_id: str,
    now: datetime,
) -> PositionDetail:
    """Read the full position detail for view C-2.

    Raises ``HTTPException(404)`` if the position does not exist.
    """
    pos_result = await session.execute(
        text("""
            SELECT
                position_id,
                instrument_type,
                direction,
                status,
                details_json,
                entry_timestamp,
                realized_pnl_to_date_usd,
                bracket_id
            FROM positions
            WHERE position_id = :position_id
        """),
        {"position_id": position_id},
    )
    pos_row = pos_result.fetchone()
    if pos_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Position {position_id!r} not found",
        )

    (
        pos_id,
        instrument_type,
        direction,
        pos_status,
        details_json_str,
        entry_ts_str,
        realized_pnl_raw,
        bracket_id,
    ) = pos_row

    details = _safe_json_dict(details_json_str)

    ticker = str(
        details.get("ticker") or details.get("underlying") or details.get("symbol") or "Unknown"
    )
    mv_raw = details.get("market_value_usd") or details.get("current_market_value_usd") or "0"
    mv = Decimal(0)
    with contextlib.suppress(Exception):
        mv = Decimal(str(mv_raw))

    unreal_raw = details.get("unrealized_pnl_usd") or details.get("unrealized_pl_usd") or "0"
    unreal = Decimal(0)
    with contextlib.suppress(Exception):
        unreal = Decimal(str(unreal_raw))

    qty = 0.0
    with contextlib.suppress(Exception):
        qty = float(details.get("quantity") or details.get("qty") or 0)

    age_hours = 0.0
    if entry_ts_str is not None:
        entry_dt = _parse_fill_ts(entry_ts_str)
        if entry_dt is not None:
            age_hours = (now - entry_dt).total_seconds() / 3600.0

    dist_target: float | None = None
    dist_inval: float | None = None
    if (raw_target := details.get("distance_to_target_pct")) is not None:
        with contextlib.suppress(Exception):
            dist_target = float(raw_target)
    if (raw_inval := details.get("distance_to_nearest_invalidation_pct")) is not None:
        with contextlib.suppress(Exception):
            dist_inval = float(raw_inval)

    bracket_legs: list[BracketLegDetail] = []
    if bracket_id is not None:
        bracket_legs = await _read_bracket_legs(session, str(bracket_id))

    fills = await _read_fills_for_position(session, position_id)
    thesis = await _read_thesis_for_position(session, position_id)
    activity_log = await _read_activity_log_for_position(session, position_id)

    return PositionDetail(
        position_id=str(pos_id),
        ticker=ticker,
        instrument_type=str(instrument_type),
        direction=str(direction) if direction else None,
        status=str(pos_status),
        quantity=qty,
        market_value_usd=str(mv),
        unrealized_pl_usd=str(unreal),
        realized_pl_usd=(
            str(Decimal(str(realized_pnl_raw))) if realized_pnl_raw is not None else None
        ),
        age_hours=age_hours,
        distance_to_target_pct=dist_target,
        distance_to_nearest_invalidation_pct=dist_inval,
        bracket_id=str(bracket_id) if bracket_id is not None else None,
        bracket_legs=bracket_legs,
        fills=fills,
        thesis=thesis,
        activity_log=activity_log,
    )


async def _fetch_thesis_status_history(
    session: AsyncSession,
    thesis_id: str,
) -> list[ThesisStatusTransition]:
    """Fetch THESIS_STATUS_CHANGED activity_log entries for the given thesis."""
    history_result = await session.execute(
        text("""
            SELECT entry_id, entry_at, detail_json
            FROM activity_log
            WHERE thesis_id = :thesis_id
              AND event_type = 'THESIS_STATUS_CHANGED'
            ORDER BY entry_at
        """),
        {"thesis_id": thesis_id},
    )
    history_rows = history_result.fetchall()

    status_history: list[ThesisStatusTransition] = []
    for entry_id, entry_at, detail_json_str in history_rows:
        old_status = ""
        new_status = ""
        with contextlib.suppress(Exception):
            detail = _safe_json_dict(detail_json_str)
            old_status = str(detail.get("old_status", ""))
            new_status = str(detail.get("new_status", ""))

        status_history.append(
            ThesisStatusTransition(
                entry_id=str(entry_id),
                entry_at=str(entry_at),
                old_status=old_status,
                new_status=new_status,
                cited_reference_ids=_extract_reference_ids(detail_json_str),
            )
        )

    return status_history


async def _read_thesis_detail(
    session: AsyncSession,
    thesis_id: str,
) -> ThesisDetail:
    """Read full thesis detail: row + components + status history."""
    now = datetime.now(UTC)

    # -- Thesis row --
    thesis_result = await session.execute(
        text("""
            SELECT
                t.thesis_id,
                t.position_id,
                t.status,
                t.resolution_category,
                t.summary,
                t.generation_timestamp,
                t.resolution_timestamp,
                json_extract(t.narrative_json, '$.thesis_status') AS thesis_status
            FROM theses t
            WHERE t.thesis_id = :thesis_id
        """),
        {"thesis_id": thesis_id},
    )
    thesis_row = thesis_result.fetchone()
    if thesis_row is None:
        raise HTTPException(status_code=404, detail=f"Thesis {thesis_id!r} not found")

    (
        db_thesis_id,
        position_id,
        db_status,
        db_resolution_category,
        summary,
        generation_timestamp,
        resolution_timestamp,
        thesis_status_raw,
    ) = thesis_row

    age_hours = 0.0
    gen_dt = _parse_fill_ts(str(generation_timestamp))
    if gen_dt is not None:
        age_hours = (now - gen_dt).total_seconds() / 3600.0

    components, resolution_component_outcomes = await _fetch_thesis_components(session, thesis_id)
    status_history = await _fetch_thesis_status_history(session, thesis_id)

    return ThesisDetail(
        thesis_id=str(db_thesis_id),
        position_id=str(position_id),
        status=str(db_status),
        thesis_status=str(thesis_status_raw) if thesis_status_raw else None,
        resolution_category=str(db_resolution_category) if db_resolution_category else None,
        summary=str(summary),
        generation_timestamp=str(generation_timestamp),
        resolution_timestamp=str(resolution_timestamp) if resolution_timestamp else None,
        age_hours=age_hours,
        components=components,
        status_history=status_history,
        resolution_component_outcomes=resolution_component_outcomes,
    )


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------


def _foreign_reader_session(request: Request) -> async_sessionmaker[AsyncSession]:
    """Pull the foreign-reader session factory off app.state."""
    return request.app.state.foreign_reader_session_factory  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def build_portfolio_router() -> APIRouter:
    """Return a fresh ``APIRouter`` for portfolio views.

    Mount under ``/api/views/portfolio`` in ``app.py``.

    Endpoints:

    * ``GET /dashboard``                       — portfolio dashboard (ALP-676)
    * ``GET /positions/{position_id}``         — position detail (ALP-677)
    * ``GET /theses``                          — paginated theses list (ALP-678)
    * ``GET /theses/{thesis_id}``              — thesis detail (ALP-678)
    """
    router = APIRouter(tags=["views:portfolio"])

    @router.get("/dashboard", response_model=PortfolioDashboard)
    async def get_portfolio_dashboard(
        session_factory: Annotated[
            async_sessionmaker[AsyncSession], Depends(_foreign_reader_session)
        ],
    ) -> PortfolioDashboard:
        """Return all five portfolio-dashboard panes in one response.

        Reads: ``drawdown_state``, ``cash_ledger``, ``positions``, ``theses``,
        ``orders``, ``fill_records``. All reads via the foreign-reader
        (read-only) session factory. No mutations.
        """
        async with session_factory() as session:
            equity_and_pl, cash_and_capital, exposure, positions, pending_orders = (
                await _read_equity_and_pl(session),
                await _read_cash_and_capital(session),
                await _read_exposure(session),
                await _read_positions(session),
                await _read_pending_orders(session),
            )

        return PortfolioDashboard(
            equity_and_pl=equity_and_pl,
            cash_and_capital=cash_and_capital,
            exposure=exposure,
            positions=positions,
            pending_orders=pending_orders,
        )

    @router.get("/positions/{position_id}", response_model=PositionDetail)
    async def get_position_detail(
        position_id: str,
        session_factory: Annotated[
            async_sessionmaker[AsyncSession], Depends(_foreign_reader_session)
        ],
    ) -> PositionDetail:
        """Return full detail for a single position (view C-2, ALP-677).

        Aggregates: position fields, bracket legs, fill history, thesis +
        components, activity_log entries filtered by ``position_id``.
        Returns 404 when the position does not exist.
        """
        now = datetime.now(UTC)
        async with session_factory() as session:
            return await _read_position_detail(session, position_id, now)

    @router.get("/theses", response_model=ThesesPage)
    async def get_theses(
        session_factory: Annotated[
            async_sessionmaker[AsyncSession], Depends(_foreign_reader_session)
        ],
        status: Annotated[str | None, Query(description="ACTIVE | RESOLVED | CANCELLED")] = None,
        classification: Annotated[
            str | None,
            Query(
                description=(
                    "Thesis-status classification: ON_TRACK | PARTIALLY_REALIZED"
                    " | AT_RISK | STALE | INVALIDATED"
                )
            ),
        ] = None,
        sector: Annotated[
            str | None, Query(description="Sector filter from position details")
        ] = None,
        age_days_gt: Annotated[int | None, Query(description="Minimum age in days")] = None,
        resolution_category: Annotated[
            str | None,
            Query(
                description=(
                    "VALIDATED | PROFITABLE_BUT_WRONG"
                    " | INVALIDATED_STOPPED_CORRECTLY"
                    " | INVALIDATED_WRONG_ON_EXIT"
                    " | CANCELLED_NEVER_ENTERED"
                )
            ),
        ] = None,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> ThesesPage:
        """Return paginated, filterable theses list.

        Filter dimensions: ``status``, ``classification`` (thesis-status
        from narrative_json), ``sector`` (position details_json),
        ``age_days_gt``, ``resolution_category``.
        """
        async with session_factory() as session:
            return await _read_theses_page(
                session,
                status=status,
                classification=classification,
                sector=sector,
                age_days_gt=age_days_gt,
                resolution_category=resolution_category,
                page=page,
                page_size=page_size,
            )

    @router.get("/theses/{thesis_id}", response_model=ThesisDetail)
    async def get_thesis_detail(
        thesis_id: str,
        session_factory: Annotated[
            async_sessionmaker[AsyncSession], Depends(_foreign_reader_session)
        ],
    ) -> ThesisDetail:
        """Return full thesis detail.

        Includes: thesis row, all components with narratives and key
        assumptions, status-history timeline (THESIS_STATUS_CHANGED
        activity-log entries with cited reference-IDs), and
        per-component resolution outcomes for resolved theses.
        """
        async with session_factory() as session:
            return await _read_thesis_detail(session, thesis_id)

    return router
