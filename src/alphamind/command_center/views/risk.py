"""Risk views — guardrail dashboard, regime timeline, calibration mix (ALP-680).

Exposes three read-only endpoints under ``/api/views/risk``:

* ``GET /api/views/risk/guardrail-dashboard`` — per-rule status (name,
  current, limit, headroom_pct, zone), active multipliers/overlays, drawdown
  state with progressive-tier classification, recent breach events (last 24 h).

* ``GET /api/views/risk/regime-timeline?from=&to=`` — chronological list of
  regime-classification, overlay-activation, and breach events within the
  requested time window.

* ``GET /api/views/risk/calibration-mix?invocations_window=`` — per-invocation
  calibration-state reductions from
  ``data/provenance/invocations/{id}/data_calibration_state.json``; trailing
  7-day mix trend; stuck-in-non-calibrated entries with warm-up duration
  cross-reference.

Design references:
  docs/design/command-center.md § Guardrail dashboard, § Regime timeline,
                                  § Calibration mix
  docs/design/06-risk-guardrails/breach-behavior.md   — zone thresholds
  docs/design/06-risk-guardrails/rules-and-limits.md  — rule registry
  docs/design/02-distillation-layer/threshold-calibration.md
    § Calibration-state snapshot file
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.portfolio_state.events.types import EventType
from alphamind.state.tables.activity_log import ActivityLogRow
from alphamind.state.tables.drawdown_state import DrawdownStateRow
from alphamind.state.tables.invocations import InvocationRow
from alphamind.state.tables.monitor_halt_mode import MonitorHaltModeRow
from alphamind.state.tables.positions import PositionRow

log = logging.getLogger(__name__)

__all__ = ["build_risk_router"]

# ---------------------------------------------------------------------------
# Zone classification thresholds (breach-behavior.md)
# ---------------------------------------------------------------------------

_ZONE_WARNING_PCT = 70.0
_ZONE_CRITICAL_PCT = 85.0
_ZONE_HARD_BLOCK_PCT = 95.0

# Drawdown rules use tighter per-rule overrides per breach-behavior.md
_DAILY_DD_WARNING_PCT = 60.0
_DAILY_DD_CRITICAL_PCT = 80.0
_DAILY_DD_HARD_BLOCK_PCT = 90.0
_CUMUL_DD_WARNING_PCT = 50.0
_CUMUL_DD_CRITICAL_PCT = 70.0
_CUMUL_DD_HARD_BLOCK_PCT = 85.0

# Drawdown rule limits (rules-and-limits.md)
_DAILY_DRAWDOWN_LIMIT_PCT = 2.5
_CUMULATIVE_DRAWDOWN_LIMIT_PCT = 8.0

# Cumulative drawdown progressive tiers (breach-behavior.md)
_CUMUL_DD_TIER2_PCT = 10.0  # 125 % of limit
_CUMUL_DD_TIER3_PCT = 12.0  # 150 % of limit

# Warm-up duration estimate (threshold-calibration.md § Warm-up duration)
WARMUP_DURATION_ESTIMATE = (
    "Volume/ATR/spread baselines: ~20 trading days (~1 month). "
    "Sentiment baseline: ~30 trading days after vendor backfill. "
    "Gap-fill rate: months (event-driven, 252-day resolution window). "
    "Extended-hours confirmation: similar to gap-fill. "
    "Lead-lag pair estimates: ~1-2 months (10 cycle minimum)."
)


# ---------------------------------------------------------------------------
# Pydantic response models (Pydantic at boundaries per ALP-128 invariant)
# ---------------------------------------------------------------------------


class RuleStatus(BaseModel):
    """Per-rule guardrail status row."""

    rule_name: str
    current_value: float
    limit_value: float
    headroom_pct: float
    zone: str  # "normal" | "warning" | "critical" | "hard_block"


class MultiplierEntry(BaseModel):
    """Single rule → multiplier mapping from the active regime."""

    rule_name: str
    multiplier: float
    effective_limit: float


class ActiveMultipliersAndOverlays(BaseModel):
    """Active regime label, per-rule multiplier table, overlay list, halt flag."""

    regime_label: str | None
    multipliers: list[MultiplierEntry]
    active_overlays: list[str]
    halt_mode_engaged: bool


class DrawdownStatus(BaseModel):
    """Drawdown state with progressive-tier classification."""

    current_drawdown_pct: float
    equity_high_water_mark_usd: str
    daily_drawdown_pct: float
    daily_drawdown_limit_pct: float
    daily_zone: str
    cumulative_drawdown_pct: float
    cumulative_drawdown_limit_pct: float
    cumulative_zone: str
    progressive_tier: int  # 0 = normal, 1 = tier 1 (8 %), 2 = tier 2 (10 %), 3 = tier 3 (12 %)
    halt_mode_engaged: bool


class RecentBreachItem(BaseModel):
    """Single breach event from the activity_log."""

    entry_id: str
    entry_at: str
    event_type: str
    position_id: str | None
    detail_json: str


class GuardrailDashboard(BaseModel):
    """Full guardrail-dashboard response."""

    rules: list[RuleStatus]
    active_multipliers_and_overlays: ActiveMultipliersAndOverlays
    drawdown: DrawdownStatus
    recent_breaches: list[RecentBreachItem]


class RegimeTimelineEvent(BaseModel):
    """Single event entry in the regime/overlay timeline."""

    entry_at: str
    event_type: str
    event_group: str
    detail_json: str
    position_id: str | None


class RegimeTimeline(BaseModel):
    """Full regime/overlay timeline response."""

    events: list[RegimeTimelineEvent]
    from_ts: str
    to_ts: str


class CalibrationStateCount(BaseModel):
    """Per-invocation calibration-state summary."""

    invocation_id: str
    as_of: str
    total_blocks: int
    calibrated: int
    accumulating: int
    unavailable: int
    calibrated_pct: float


class StuckBlockEntry(BaseModel):
    """Single block that is stuck in non-calibrated state."""

    block_id: str
    reason: str
    last_calibrated_invocation_id: str | None


class SevenDayTrendPoint(BaseModel):
    """One day in the 7-day calibration trend."""

    date: str
    calibrated_pct: float
    total_blocks: int


class CalibrationMix(BaseModel):
    """Full calibration-mix response."""

    per_invocation: list[CalibrationStateCount]
    seven_day_trend: list[SevenDayTrendPoint]
    stuck_blocks: list[StuckBlockEntry]
    warmup_duration_estimate: str


# ---------------------------------------------------------------------------
# Internal frozen dataclasses (not at API boundary)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _DrawdownRow:
    equity_high_water_mark_usd: float
    current_drawdown_pct: float


@dataclass(frozen=True, slots=True)
class _HaltModeRow:
    enabled: int


# ---------------------------------------------------------------------------
# Zone classification helpers
# ---------------------------------------------------------------------------


def _classify_zone(
    consumed_pct: float,
    *,
    warning_pct: float = _ZONE_WARNING_PCT,
    critical_pct: float = _ZONE_CRITICAL_PCT,
    hard_block_pct: float = _ZONE_HARD_BLOCK_PCT,
) -> str:
    """Map ``consumed_pct`` to a zone label per breach-behavior.md."""
    if consumed_pct >= hard_block_pct:
        return "hard_block"
    if consumed_pct >= critical_pct:
        return "critical"
    if consumed_pct >= warning_pct:
        return "warning"
    return "normal"


def _classify_drawdown_tier(cumulative_pct: float) -> int:
    """Return the progressive tier integer (0-3) for cumulative drawdown."""
    if cumulative_pct >= _CUMUL_DD_TIER3_PCT:
        return 3
    if cumulative_pct >= _CUMUL_DD_TIER2_PCT:
        return 2
    if cumulative_pct >= _CUMULATIVE_DRAWDOWN_LIMIT_PCT:
        return 1
    return 0


# ---------------------------------------------------------------------------
# Dependency — extract the foreign_reader session factory from app.state
# ---------------------------------------------------------------------------


def _get_reader_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    return request.app.state.foreign_reader_session_factory  # type: ignore[no-any-return]


ReaderFactory = Annotated[async_sessionmaker[AsyncSession], Depends(_get_reader_factory)]


# ---------------------------------------------------------------------------
# Guardrail dashboard read helpers
# ---------------------------------------------------------------------------


async def _read_drawdown(session: AsyncSession) -> _DrawdownRow | None:
    result = await session.execute(select(DrawdownStateRow))
    row = result.scalars().first()
    if row is None:
        return None
    return _DrawdownRow(
        equity_high_water_mark_usd=row.equity_high_water_mark_usd,
        current_drawdown_pct=row.current_drawdown_pct,
    )


async def _read_halt_mode(session: AsyncSession) -> bool:
    result = await session.execute(select(MonitorHaltModeRow))
    row = result.scalars().first()
    if row is None:
        return False
    return bool(row.enabled)


async def _read_recent_breaches(session: AsyncSession) -> list[dict[str, Any]]:
    """Return activity_log rows matching breach event types in the last 24 h."""
    breach_types = [
        EventType.GUARDRAIL_REJECTION.value,
        EventType.RISK_LIMIT_APPROACHED.value,
        EventType.RISK_PARAMETER_CHANGED.value,
        EventType.HALT_ACTIVATED.value,
        EventType.HALT_LIFTED.value,
    ]
    since = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
    stmt = (
        select(ActivityLogRow)
        .where(
            and_(
                ActivityLogRow.event_type.in_(breach_types),
                ActivityLogRow.entry_at >= since,
            )
        )
        .order_by(ActivityLogRow.entry_at.desc())
        .limit(50)
    )
    result = await session.execute(stmt)
    rows = result.scalars().all()
    return [
        {
            "entry_id": r.entry_id,
            "entry_at": r.entry_at,
            "event_type": r.event_type,
            "position_id": r.position_id,
            "detail_json": r.detail_json,
        }
        for r in rows
    ]


async def _read_open_positions(session: AsyncSession) -> list[dict[str, Any]]:
    """Return all OPEN positions for per-rule exposure computation.

    Market value is not stored as a direct column — it is derived from
    ``details_json`` which carries the per-variant payload.  We return
    direction + details_json so the exposure computation can parse what
    it needs.
    """
    stmt = select(PositionRow).where(PositionRow.status == "OPEN")
    result = await session.execute(stmt)
    rows = result.scalars().all()
    return [
        {
            "position_id": r.position_id,
            "instrument_type": r.instrument_type,
            "direction": r.direction,
            "details_json": r.details_json,
        }
        for r in rows
    ]


def _extract_market_value(details_json: str) -> float:
    """Best-effort extraction of a market-value proxy from ``details_json``.

    The details JSON shape varies by instrument type (equity / option /
    strategy).  Common numeric fields tried in order:
    ``current_market_value_usd``, ``market_value_usd``, ``net_premium_usd``,
    ``entry_value_usd``.  Returns 0.0 when no recognized field is found or
    parsing fails.
    """
    try:
        obj = json.loads(details_json)
    except (json.JSONDecodeError, TypeError):
        return 0.0
    for key in (
        "current_market_value_usd",
        "market_value_usd",
        "net_premium_usd",
        "entry_value_usd",
    ):
        val = obj.get(key)
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                pass
    return 0.0


def _build_exposure_rules(
    positions: list[dict[str, Any]],
    portfolio_value_usd: float,
) -> list[RuleStatus]:
    """Compute exposure-based rule statuses from open positions.

    Produces rule rows for:
    - gross_exposure (limit 120 %)
    - net_long_exposure (limit 60 %)
    - net_short_exposure (limit 30 %)

    Uses best-effort market-value extraction from ``details_json``.
    direction=LONG → long side, SHORT → short side.
    """
    if portfolio_value_usd <= 0:
        return []

    total_long = 0.0
    total_short = 0.0
    for pos in positions:
        mv = _extract_market_value(pos.get("details_json") or "{}")
        direction = (pos.get("direction") or "").upper()
        if direction == "LONG":
            total_long += mv
        elif direction == "SHORT":
            total_short += mv

    pv = portfolio_value_usd
    gross_pct = ((total_long + total_short) / pv) * 100.0
    net_long_pct = (total_long / pv) * 100.0
    net_short_pct = (total_short / pv) * 100.0

    rules_data = [
        ("gross_exposure", gross_pct, 120.0),
        ("net_long_exposure", net_long_pct, 60.0),
        ("net_short_exposure", net_short_pct, 30.0),
    ]

    result = []
    for rule_name, current, limit in rules_data:
        consumed = (current / limit * 100.0) if limit > 0 else 0.0
        headroom_pct = max(0.0, 100.0 - consumed)
        result.append(
            RuleStatus(
                rule_name=rule_name,
                current_value=round(current, 4),
                limit_value=limit,
                headroom_pct=round(headroom_pct, 4),
                zone=_classify_zone(consumed),
            )
        )
    return result


def _build_drawdown_rules(
    current_drawdown_pct: float,
    daily_drawdown_pct: float,
) -> list[RuleStatus]:
    """Build drawdown rule status rows with their per-rule zone overrides."""
    daily_consumed = (
        daily_drawdown_pct / _DAILY_DRAWDOWN_LIMIT_PCT * 100.0
        if _DAILY_DRAWDOWN_LIMIT_PCT > 0
        else 0.0
    )
    daily_headroom = max(0.0, 100.0 - daily_consumed)

    cumul_consumed = (
        current_drawdown_pct / _CUMULATIVE_DRAWDOWN_LIMIT_PCT * 100.0
        if _CUMULATIVE_DRAWDOWN_LIMIT_PCT > 0
        else 0.0
    )
    cumul_headroom = max(0.0, 100.0 - cumul_consumed)

    return [
        RuleStatus(
            rule_name="daily_drawdown",
            current_value=round(daily_drawdown_pct, 4),
            limit_value=_DAILY_DRAWDOWN_LIMIT_PCT,
            headroom_pct=round(daily_headroom, 4),
            zone=_classify_zone(
                daily_consumed,
                warning_pct=_DAILY_DD_WARNING_PCT,
                critical_pct=_DAILY_DD_CRITICAL_PCT,
                hard_block_pct=_DAILY_DD_HARD_BLOCK_PCT,
            ),
        ),
        RuleStatus(
            rule_name="cumulative_drawdown",
            current_value=round(current_drawdown_pct, 4),
            limit_value=_CUMULATIVE_DRAWDOWN_LIMIT_PCT,
            headroom_pct=round(cumul_headroom, 4),
            zone=_classify_zone(
                cumul_consumed,
                warning_pct=_CUMUL_DD_WARNING_PCT,
                critical_pct=_CUMUL_DD_CRITICAL_PCT,
                hard_block_pct=_CUMUL_DD_HARD_BLOCK_PCT,
            ),
        ),
    ]


# ---------------------------------------------------------------------------
# Regime timeline read helper
# ---------------------------------------------------------------------------

_TIMELINE_EVENT_TYPES = [
    EventType.GUARDRAIL_REJECTION.value,
    EventType.RISK_LIMIT_APPROACHED.value,
    EventType.RISK_PARAMETER_CHANGED.value,
    EventType.HALT_ACTIVATED.value,
    EventType.HALT_LIFTED.value,
    EventType.EMERGENCY_INVOCATION_REQUESTED.value,
    EventType.PROFILE_SWITCHED.value,
]


async def _read_timeline_events(
    session: AsyncSession,
    from_ts: str,
    to_ts: str,
) -> list[dict[str, Any]]:
    stmt = (
        select(ActivityLogRow)
        .where(
            and_(
                ActivityLogRow.event_type.in_(_TIMELINE_EVENT_TYPES),
                ActivityLogRow.entry_at >= from_ts,
                ActivityLogRow.entry_at <= to_ts,
            )
        )
        .order_by(ActivityLogRow.entry_at.asc())
        .limit(500)
    )
    result = await session.execute(stmt)
    rows = result.scalars().all()
    return [
        {
            "entry_at": r.entry_at,
            "event_type": r.event_type,
            "event_group": r.event_group,
            "detail_json": r.detail_json,
            "position_id": r.position_id,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Calibration mix helpers
# ---------------------------------------------------------------------------


def _load_calibration_state(
    invocation_id: str,
    data_dir: Path,
) -> dict[str, Any] | None:
    """Load ``data_calibration_state.json`` for a single invocation.

    Returns None if the file is absent or malformed.
    """
    path = data_dir / "invocations" / invocation_id / "data_calibration_state.json"
    try:
        with path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return raw if isinstance(raw, dict) else None


def _resolve_data_dir(request: Request) -> Path:
    """Resolve the provenance data directory from app.state or a sensible default."""
    cfg = getattr(request.app.state, "command_center_config", None)
    if cfg is not None:
        db_path = str(getattr(getattr(cfg, "db", None), "alphamind_db_path", "") or "")
        if db_path and db_path != ":memory:":
            return Path(db_path).expanduser().parent / "provenance"
    return Path("data/provenance")


async def _read_recent_invocations(
    session: AsyncSession,
    limit: int,
) -> list[str]:
    """Return the ``invocation_id`` values for the most-recent completed invocations.

    Uses ``phase2_completed_at`` as the completion timestamp (nullable;
    completed invocations have this set).  Falls back to ``start_at`` ordering
    when ``phase2_completed_at`` is NULL so in-flight invocations sort last.
    """
    stmt = (
        select(InvocationRow.invocation_id)
        .where(InvocationRow.phase2_completed_at.is_not(None))
        .order_by(InvocationRow.phase2_completed_at.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return [row[0] for row in result.all()]


def _build_calibration_count(
    invocation_id: str,
    raw: dict[str, Any],
) -> CalibrationStateCount:
    """Convert a raw calibration-state JSON object to the response model."""
    summary = raw.get("summary") or {}
    by_state = summary.get("by_state") or {}
    total = int(summary.get("total_blocks") or 0)
    calibrated = int(by_state.get("calibrated") or 0)
    accumulating = int(by_state.get("accumulating") or 0)
    unavailable = int(by_state.get("unavailable") or 0)
    calibrated_pct = (calibrated / total * 100.0) if total > 0 else 0.0
    return CalibrationStateCount(
        invocation_id=invocation_id,
        as_of=str(raw.get("as_of") or ""),
        total_blocks=total,
        calibrated=calibrated,
        accumulating=accumulating,
        unavailable=unavailable,
        calibrated_pct=round(calibrated_pct, 2),
    )


def _build_seven_day_trend(
    per_invocation: list[CalibrationStateCount],
) -> list[SevenDayTrendPoint]:
    """Aggregate per-invocation counts into daily buckets for the trailing 7 days.

    Groups by the date prefix of ``as_of``; picks the last invocation per day.
    Returns points sorted ascending by date.
    """
    by_date: dict[str, CalibrationStateCount] = {}
    cutoff = (datetime.now(UTC) - timedelta(days=7)).date()
    for item in per_invocation:
        date_str = item.as_of[:10] if item.as_of else ""
        if not date_str:
            continue
        try:
            d = date.fromisoformat(date_str)
        except ValueError:
            continue
        if d < cutoff:
            continue
        # keep latest per day
        if date_str not in by_date or item.as_of > by_date[date_str].as_of:
            by_date[date_str] = item

    return [
        SevenDayTrendPoint(
            date=date_str,
            calibrated_pct=item.calibrated_pct,
            total_blocks=item.total_blocks,
        )
        for date_str, item in sorted(by_date.items())
    ]


def _build_stuck_blocks(
    all_raws: list[dict[str, Any]],
    invocation_ids: list[str],
) -> list[StuckBlockEntry]:
    """Identify blocks stuck in accumulating/unavailable across all invocations.

    A block is 'stuck' if it appears in ``accumulating_reasons`` or
    ``unavailable_reasons`` of the most recent invocation.  Provides
    the last invocation where the block was ``calibrated`` (absent from the
    reasons maps) by scanning backwards.

    Returns at most 50 stuck entries.
    """
    if not all_raws or not invocation_ids:
        return []

    most_recent_raw = all_raws[0]
    accumulating = dict(most_recent_raw.get("accumulating_reasons") or {})
    unavailable = dict(most_recent_raw.get("unavailable_reasons") or {})

    stuck: dict[str, tuple[str, str]] = {}
    for block_id, reason in accumulating.items():
        stuck[block_id] = (reason, "accumulating")
    for block_id, reason in unavailable.items():
        if block_id not in stuck:
            stuck[block_id] = (reason, "unavailable")

    if not stuck:
        return []

    # Find last invocation where each stuck block was calibrated (absent from reasons maps)
    last_calibrated: dict[str, str | None] = {bid: None for bid in stuck}
    for inv_id, raw in zip(invocation_ids[1:], all_raws[1:], strict=False):
        acc = set(raw.get("accumulating_reasons") or {})
        unav = set(raw.get("unavailable_reasons") or {})
        non_calibrated = acc | unav
        for block_id in list(last_calibrated.keys()):
            if last_calibrated[block_id] is None and block_id not in non_calibrated:
                last_calibrated[block_id] = inv_id

    result = []
    for block_id, (reason, _kind) in list(stuck.items())[:50]:
        result.append(
            StuckBlockEntry(
                block_id=block_id,
                reason=reason,
                last_calibrated_invocation_id=last_calibrated.get(block_id),
            )
        )
    return result


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_risk_router() -> APIRouter:
    """Return the risk views APIRouter.

    All three endpoints are read-only and pull their data from the
    ``foreign_reader_session_factory`` wired onto ``app.state`` by the
    lifespan.
    """
    router = APIRouter()

    @router.get("/guardrail-dashboard", response_model=GuardrailDashboard)
    async def guardrail_dashboard(
        reader_factory: ReaderFactory,
    ) -> GuardrailDashboard:
        """Per-rule status + multipliers/overlays + drawdown + recent breaches."""
        async with reader_factory() as session:
            dd_row = await _read_drawdown(session)
            halt_mode = await _read_halt_mode(session)
            positions = await _read_open_positions(session)
            recent_breaches_raw = await _read_recent_breaches(session)

        # Determine portfolio value for %-based rules.
        portfolio_value_usd = dd_row.equity_high_water_mark_usd if dd_row else 0.0

        # Drawdown values — daily drawdown is best-effort from drawdown_state.
        # drawdown_state stores current_drawdown_pct (cumulative from HWM).
        # Daily drawdown is not separately stored; surface 0.0 when unavailable.
        current_dd_pct = dd_row.current_drawdown_pct if dd_row else 0.0
        daily_dd_pct = 0.0  # daily intraday figure requires live tick data

        exposure_rules = _build_exposure_rules(positions, portfolio_value_usd)
        drawdown_rules = _build_drawdown_rules(current_dd_pct, daily_dd_pct)

        all_rules = [*exposure_rules, *drawdown_rules]

        # Drawdown status block
        daily_consumed = (
            daily_dd_pct / _DAILY_DRAWDOWN_LIMIT_PCT * 100.0
            if _DAILY_DRAWDOWN_LIMIT_PCT > 0
            else 0.0
        )
        cumul_consumed = (
            current_dd_pct / _CUMULATIVE_DRAWDOWN_LIMIT_PCT * 100.0
            if _CUMULATIVE_DRAWDOWN_LIMIT_PCT > 0
            else 0.0
        )
        drawdown_status = DrawdownStatus(
            current_drawdown_pct=round(current_dd_pct, 4),
            equity_high_water_mark_usd=str(dd_row.equity_high_water_mark_usd if dd_row else 0.0),
            daily_drawdown_pct=round(daily_dd_pct, 4),
            daily_drawdown_limit_pct=_DAILY_DRAWDOWN_LIMIT_PCT,
            daily_zone=_classify_zone(
                daily_consumed,
                warning_pct=_DAILY_DD_WARNING_PCT,
                critical_pct=_DAILY_DD_CRITICAL_PCT,
                hard_block_pct=_DAILY_DD_HARD_BLOCK_PCT,
            ),
            cumulative_drawdown_pct=round(current_dd_pct, 4),
            cumulative_drawdown_limit_pct=_CUMULATIVE_DRAWDOWN_LIMIT_PCT,
            cumulative_zone=_classify_zone(
                cumul_consumed,
                warning_pct=_CUMUL_DD_WARNING_PCT,
                critical_pct=_CUMUL_DD_CRITICAL_PCT,
                hard_block_pct=_CUMUL_DD_HARD_BLOCK_PCT,
            ),
            progressive_tier=_classify_drawdown_tier(current_dd_pct),
            halt_mode_engaged=halt_mode,
        )

        # Active multipliers/overlays — surface empty list when no regime data is
        # available (the regime adaptation state is not yet exposed as a separate
        # DB table in this worktree; the dashboard surfaces what is persisted).
        active_mults = ActiveMultipliersAndOverlays(
            regime_label=None,
            multipliers=[],
            active_overlays=[],
            halt_mode_engaged=halt_mode,
        )

        return GuardrailDashboard(
            rules=all_rules,
            active_multipliers_and_overlays=active_mults,
            drawdown=drawdown_status,
            recent_breaches=[
                RecentBreachItem(
                    entry_id=b["entry_id"],
                    entry_at=b["entry_at"],
                    event_type=b["event_type"],
                    position_id=b["position_id"],
                    detail_json=b["detail_json"],
                )
                for b in recent_breaches_raw
            ],
        )

    @router.get("/regime-timeline", response_model=RegimeTimeline)
    async def regime_timeline(
        reader_factory: ReaderFactory,
        from_ts: Annotated[str | None, Query(alias="from")] = None,
        to_ts: Annotated[str | None, Query(alias="to")] = None,
    ) -> RegimeTimeline:
        """Chronological regime + overlay + breach events within the time window."""
        now = datetime.now(UTC)
        resolved_to = to_ts or now.isoformat()
        resolved_from = from_ts or (now - timedelta(hours=24)).isoformat()

        async with reader_factory() as session:
            events_raw = await _read_timeline_events(session, resolved_from, resolved_to)

        return RegimeTimeline(
            events=[
                RegimeTimelineEvent(
                    entry_at=e["entry_at"],
                    event_type=e["event_type"],
                    event_group=e["event_group"],
                    detail_json=e["detail_json"],
                    position_id=e["position_id"],
                )
                for e in events_raw
            ],
            from_ts=resolved_from,
            to_ts=resolved_to,
        )

    @router.get("/calibration-mix", response_model=CalibrationMix)
    async def calibration_mix(
        reader_factory: ReaderFactory,
        request: Request,
        invocations_window: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> CalibrationMix:
        """Per-invocation calibration-state mix + 7-day trend + stuck blocks."""
        async with reader_factory() as session:
            inv_ids = await _read_recent_invocations(session, invocations_window)

        data_dir = _resolve_data_dir(request)
        raws: list[dict[str, Any]] = []
        valid_ids: list[str] = []
        for inv_id in inv_ids:
            raw = _load_calibration_state(inv_id, data_dir)
            if raw is not None:
                raws.append(raw)
                valid_ids.append(inv_id)

        per_invocation = [
            _build_calibration_count(inv_id, raw)
            for inv_id, raw in zip(valid_ids, raws, strict=True)
        ]
        seven_day_trend = _build_seven_day_trend(per_invocation)
        stuck_blocks = _build_stuck_blocks(raws, valid_ids)

        return CalibrationMix(
            per_invocation=per_invocation,
            seven_day_trend=seven_day_trend,
            stuck_blocks=stuck_blocks,
            warmup_duration_estimate=WARMUP_DURATION_ESTIMATE,
        )

    return router
