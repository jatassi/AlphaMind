"""Portfolio dashboard view — GET /api/views/portfolio/dashboard (ALP-676).

Composed read of five panes from the foreign-reader session:

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

Design references:
  docs/design/command-center.md § Portfolio dashboard
  docs/design/05-execution-layer/regt-margin-attribution.md
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
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
        try:
            details: dict[str, Any] = json.loads(details_json_str)
        except (json.JSONDecodeError, TypeError):
            details = {}

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
        unsettled_raw: list[dict[str, Any]] = json.loads(raw_unsettled_json) or []
    except (json.JSONDecodeError, TypeError):
        unsettled_raw = []

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
        try:
            details: dict[str, Any] = json.loads(details_json_str)
        except (json.JSONDecodeError, TypeError):
            details = {}

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
    try:
        details: dict[str, Any] = json.loads(details_json_str)
    except (json.JSONDecodeError, TypeError):
        details = {}

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
        status,
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
        try:
            instrument_spec: dict[str, Any] = json.loads(instrument_spec_json)
        except (json.JSONDecodeError, TypeError):
            instrument_spec = {}

        age_hours = 0.0
        if submission_ts_str is not None:
            sub_dt = _parse_fill_ts(submission_ts_str)
            if sub_dt is not None:
                age_hours = (now - sub_dt).total_seconds() / 3600.0

        out.append(
            PendingOrderRow(
                order_id=str(order_id),
                status=str(status),
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
# FastAPI dependency
# ---------------------------------------------------------------------------


def _foreign_reader_session(request: Request) -> async_sessionmaker[AsyncSession]:
    """Pull the foreign-reader session factory off app.state."""
    return request.app.state.foreign_reader_session_factory  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def build_portfolio_router() -> APIRouter:
    """Return a fresh ``APIRouter`` for the portfolio-dashboard view.

    Mount under ``/api/views/portfolio`` in ``app.py``.
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

    return router
