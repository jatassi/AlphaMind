"""Q12 corporate-actions deterministic signals.

Implements the four detection paths in
``docs/implementation/02-distillation-layer/08e-q12-corporate-actions-signals.md``
§ Scope:

- **Event novelty detection** — per ``quant 12d`` (event-novelty signal):
  - ``unusual_event_cadence``: a scheduled ``investor_day`` / ``conference``
    / ``product_launch`` whose prior matching event for the same ticker is
    older than :data:`UNUSUAL_EVENT_CADENCE_LOOKBACK_DAYS` (or the ticker
    has no prior matching event at all).
  - ``event_clustering``: at least :data:`CLUSTERING_MIN_EVENTS` events of
    the documented event-type subset scheduled within the next
    :data:`CLUSTERING_WINDOW_DAYS` for tickers in a single sector.
- **ETF flow vs. single-name flow divergence** — per ``quant 12f``: a
  sector-ETF weakness leg (volume below the trailing-baseline 1-sigma floor and
  price weakness over the ``ETF_FLOW_WINDOW_DAYS`` window) coincident with
  single-name BTO call flow > 1-sigma on at least
  :data:`DIVERGENCE_BTO_MIN_CONSTITUENTS` of the ETF's top-10 constituents.
- **Recent corporate actions awareness** — pass-through block listing every
  ``corporate_actions`` row whose ``ex_date`` falls within
  :data:`RECENT_ACTIONS_WINDOW_TRADING_DAYS` trading days of ``as_of``.

The "two year" lookback for unusual event cadence and the 30-day forward
window for clustering are encoded as named constants per 08e § Notes —
they are definitional thresholds attached to the spec, not Class A
configuration knobs.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from statistics import fmean, pstdev
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.output import AnomalyFlag, OutputAudience, OutputBlock
from alphamind.persistence.models import (
    CorporateActions,
    EtfMembership,
    EventCalendar,
    OhlcvBars,
    OptionsContractSnapshots,
    SectorClassification,
)

# ---------------------------------------------------------------------------
# Definitional constants (per 08e § Notes — not Class A configuration)
# ---------------------------------------------------------------------------
#
# Several values below numerically coincide with unrelated keys in
# ``config/distillation.yaml`` (e.g. ``30`` with ``regime_vvix_low_percentile``,
# ``5`` with ``options_low_oi_volume_multiple``). The no-magic-numbers audit
# (``tests/distillation/test_no_magic_numbers.py``) treats those collisions as
# Class A bypass attempts; ``tests/distillation/no_magic_numbers_allowlist.txt``
# carries one entry per collision naming the unrelated YAML key.

UNUSUAL_EVENT_CADENCE_LOOKBACK_DAYS: int = 730
"""Two-year lookback for the unusual-event-cadence flag.

``quant 12d`` documents the example ("a company that hasn't held an
investor day in two years"). Encoded as a constant rather than a Class A
threshold per the story-08e Notes — the value is definitional to the
signal, not a tunable knob.
"""

CLUSTERING_WINDOW_DAYS: int = 30
"""Forward-window length for sector event clustering.

08e § Scope: ``≥ 3 events ... scheduled by universe names within the
same sector inside a 30-day forward window``.
"""

CLUSTERING_MIN_EVENTS: int = 3
"""Minimum events inside ``CLUSTERING_WINDOW_DAYS`` to trigger clustering."""

CLUSTERING_EVENT_TYPES: frozenset[str] = frozenset(
    {"investor_day", "conference", "product_launch", "regulatory_decision"}
)
"""Event-type subset that participates in the clustering signal.

Matches the four event types named in 08e § Scope. The unusual-cadence
signal looks at the first three — ``regulatory_decision`` does not have
the "two-year cadence" semantics (it is event-driven, not management-led).
"""

UNUSUAL_CADENCE_EVENT_TYPES: frozenset[str] = frozenset(
    {"investor_day", "conference", "product_launch"}
)
"""Event types whose unusual cadence is interpretable.

Per 08e § Scope: ``investor_day`` / ``conference`` / ``product_launch``.
"""

RECENT_ACTIONS_WINDOW_TRADING_DAYS: int = 5
"""Half-window for the recent-corporate-actions pass-through block (±5 trading days)."""

ETF_FLOW_WINDOW_DAYS: int = 5
"""Trailing window for ETF flow proxy.

08e § Scope: ``change in etf_membership.weight_pct over the trailing
5-day window for the ETF as a whole, plus volume comparison vs. its
20-day baseline``. The 20-day baseline window itself reaches the
function via a Class A configuration argument
(``persistence_windows.volume_baseline_days``); only the 5-day proxy
window lives as a constant.
"""

DIVERGENCE_FLOW_SIGMA_THRESHOLD: float = 1.0
"""Z-score boundary for both legs of the ETF/single-name divergence test.

08e § Scope writes "below 1-sigma" / "> 1-sigma" as the threshold.
"""

DIVERGENCE_BTO_MIN_CONSTITUENTS: int = 2
"""Minimum top-10 constituents that must show BTO flow > 1-sigma.

08e § Scope: ``BTO call flow > 1-sigma on ≥ 2 of the top-10 constituents``.
"""

# Variance estimates require at least two observations to be meaningful.
_MIN_VARIANCE_SAMPLES: int = 2

# Leading 10 characters of an ISO 8601 UTC timestamp form the ``YYYY-MM-DD``
# date prefix used to bucket events by day.
_ISO_DATE_PREFIX_LENGTH: int = 10

ETF_FLOW_ATTRIBUTION_METHOD: str = "weight_volume_proxy"
"""Documents the ETF flow proxy simplification.

Tagged on every ``q12.etf_vs_single_name_divergence`` block per 08e §
Notes so downstream readers know the proxy's limit.
"""


# ---------------------------------------------------------------------------
# Sector → audience mapping
# ---------------------------------------------------------------------------
#
# ``sector_classification.alphamind_sector`` carries one of
# ``tech`` / ``semis`` / ``financials`` / ``energy`` per storage.md;
# ``OutputAudience`` collapses tech and semis into the tech_semis
# researcher's audience.

_SECTOR_TO_AUDIENCE: Mapping[str, OutputAudience] = {
    "tech": OutputAudience.SECTOR_TECH_SEMIS,
    "semis": OutputAudience.SECTOR_TECH_SEMIS,
    "financials": OutputAudience.SECTOR_FINANCIALS,
    "energy": OutputAudience.SECTOR_ENERGY,
}

# Sector ETF tickers per storage.md § sector_classification — XLK/SMH map
# to the tech_semis audience; XLF financials; XLE energy.
_ETF_TO_AUDIENCE: Mapping[str, OutputAudience] = {
    "XLK": OutputAudience.SECTOR_TECH_SEMIS,
    "SMH": OutputAudience.SECTOR_TECH_SEMIS,
    "XLF": OutputAudience.SECTOR_FINANCIALS,
    "XLE": OutputAudience.SECTOR_ENERGY,
}


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _parse_iso_utc(ts: str) -> datetime:
    """Parse an ISO 8601 UTC timestamp/date into a tz-aware :class:`datetime`."""
    if "T" in ts:
        return datetime.fromisoformat(ts.removesuffix("Z")).replace(tzinfo=UTC)
    return datetime.fromisoformat(ts).replace(tzinfo=UTC)


def _format_iso_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Sector audience resolution
# ---------------------------------------------------------------------------


def _sector_audience(sector: str) -> OutputAudience | None:
    return _SECTOR_TO_AUDIENCE.get(sector)


def _etf_audience(etf_ticker: str) -> OutputAudience | None:
    return _ETF_TO_AUDIENCE.get(etf_ticker)


# ---------------------------------------------------------------------------
# Event-novelty detection
# ---------------------------------------------------------------------------


def _select_scheduled_events(
    session: Session,
    *,
    as_of_dt: datetime,
    horizon_dt: datetime,
    event_types: Iterable[str],
) -> Sequence[EventCalendar]:
    """Return scheduled events of ``event_types`` within ``[as_of, horizon]``."""
    stmt = (
        select(EventCalendar)
        .where(
            EventCalendar.event_type.in_(tuple(event_types)),
            EventCalendar.status == "scheduled",
            EventCalendar.scheduled_at >= _format_iso_utc(as_of_dt),
            EventCalendar.scheduled_at <= _format_iso_utc(horizon_dt),
        )
        .order_by(EventCalendar.ticker, EventCalendar.scheduled_at)
    )
    return list(session.execute(stmt).scalars().all())


def _select_prior_event(
    session: Session,
    *,
    ticker: str,
    event_type: str,
    as_of_dt: datetime,
) -> str | None:
    """Return the most recent ``scheduled_at`` strictly before ``as_of`` for the pair.

    Looks at events with ``status`` ∈ {``completed``, ``scheduled``} per
    08e § Scope; cancelled / postponed events do not represent a prior
    occurrence.
    """
    stmt = (
        select(EventCalendar.scheduled_at)
        .where(
            EventCalendar.ticker == ticker,
            EventCalendar.event_type == event_type,
            EventCalendar.status.in_(("completed", "scheduled")),
            EventCalendar.scheduled_at < _format_iso_utc(as_of_dt),
        )
        .order_by(EventCalendar.scheduled_at.desc())
        .limit(1)
    )
    return session.execute(stmt).scalar_one_or_none()


def _detect_unusual_event_cadence(
    session: Session,
    *,
    as_of_dt: datetime,
    forward_events: Sequence[EventCalendar],
) -> dict[str, list[dict[str, Any]]]:
    """Return ``{ticker: [flag_record, ...]}`` for unusual-cadence detections.

    ``forward_events`` is the pre-fetched scheduled-event list; we filter
    to the unusual-cadence event-type subset and check the prior-event
    lookback per ticker.
    """
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    lookback_floor = as_of_dt - timedelta(days=UNUSUAL_EVENT_CADENCE_LOOKBACK_DAYS)
    for event in forward_events:
        if event.event_type not in UNUSUAL_CADENCE_EVENT_TYPES:
            continue
        if event.ticker is None:
            continue
        prior_at = _select_prior_event(
            session,
            ticker=event.ticker,
            event_type=event.event_type,
            as_of_dt=as_of_dt,
        )
        if prior_at is not None and _parse_iso_utc(prior_at) >= lookback_floor:
            continue
        out[event.ticker].append(
            {
                "flag": "unusual_event_cadence",
                "event_type": event.event_type,
                "scheduled_at": event.scheduled_at,
                "prior_event_at": prior_at,
            }
        )
    return out


def _detect_event_clustering(
    *,
    forward_events: Sequence[EventCalendar],
    sector_lookup: Mapping[str, str],
) -> dict[str, list[dict[str, Any]]]:
    """Return ``{ticker: [flag_record]}`` for tickers in a clustered sector.

    A cluster fires when ≥ :data:`CLUSTERING_MIN_EVENTS` events of the
    documented event-type subset are scheduled inside the forward window
    for tickers in a single ``alphamind_sector``. Each participating
    ticker carries the cluster flag so the per-ticker output convention
    (one entry per ticker) holds for clustering as well as cadence.
    """
    by_sector: dict[str, list[EventCalendar]] = defaultdict(list)
    for event in forward_events:
        if event.event_type not in CLUSTERING_EVENT_TYPES:
            continue
        if event.ticker is None:
            continue
        sector = sector_lookup.get(event.ticker)
        if sector is None:
            continue
        by_sector[sector].append(event)
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sector, events in by_sector.items():
        if len(events) < CLUSTERING_MIN_EVENTS:
            continue
        cluster_summary = {
            "flag": "event_clustering",
            "sector": sector,
            "n_events": len(events),
            "window_days": CLUSTERING_WINDOW_DAYS,
        }
        # Attach the cluster summary to every ticker in the cluster so
        # the per-ticker output convention holds.
        for event in events:
            assert event.ticker is not None
            out[event.ticker].append(cluster_summary)
    return out


def _select_sector_lookup(session: Session) -> dict[str, str]:
    """Return ``{ticker: alphamind_sector}`` over every classified ticker."""
    rows = session.execute(
        select(SectorClassification.ticker, SectorClassification.alphamind_sector)
    ).all()
    return {ticker: sector for ticker, sector in rows}


# ---------------------------------------------------------------------------
# Recent corporate actions pass-through
# ---------------------------------------------------------------------------


def _select_recent_actions(
    session: Session,
    *,
    as_of_dt: datetime,
) -> Sequence[CorporateActions]:
    """Return ``corporate_actions`` rows whose ``ex_date`` is within ±N trading days.

    The "trading day" approximation here is calendar-day expansion: the
    ±5 trading days for a typical week corresponds to a calendar window
    of ±7 days (one weekend either side); we use ``5 + 2`` to accommodate
    one weekend on each side. The signal is awareness only — the ±5 is
    documented as approximate per 08e § Scope.
    """
    # 5 trading days ≈ 7 calendar days (one weekend either side).
    window_calendar_days = RECENT_ACTIONS_WINDOW_TRADING_DAYS + DIVERGENCE_BTO_MIN_CONSTITUENTS
    lower = (as_of_dt - timedelta(days=window_calendar_days)).date().isoformat()
    upper = (as_of_dt + timedelta(days=window_calendar_days)).date().isoformat()
    stmt = (
        select(CorporateActions)
        .where(
            CorporateActions.ex_date >= lower,
            CorporateActions.ex_date <= upper,
        )
        .order_by(CorporateActions.ticker, CorporateActions.ex_date)
    )
    return list(session.execute(stmt).scalars().all())


# ---------------------------------------------------------------------------
# ETF flow vs. single-name flow divergence
# ---------------------------------------------------------------------------


def _select_etf_volume_series(
    session: Session,
    *,
    etf_ticker: str,
    range_start_dt: datetime,
    range_end_dt: datetime,
) -> list[int]:
    """Daily adjusted volumes for ``etf_ticker`` ascending."""
    stmt = (
        select(OhlcvBars.adj_volume)
        .where(
            OhlcvBars.ticker == etf_ticker,
            OhlcvBars.timeframe == "1d",
            OhlcvBars.period_start >= _format_iso_utc(range_start_dt),
            OhlcvBars.period_start <= _format_iso_utc(range_end_dt),
        )
        .order_by(OhlcvBars.period_start)
    )
    return [int(v) for v in session.execute(stmt).scalars().all()]


def _select_etf_close_series(
    session: Session,
    *,
    etf_ticker: str,
    range_start_dt: datetime,
    range_end_dt: datetime,
) -> list[float]:
    stmt = (
        select(OhlcvBars.adj_close)
        .where(
            OhlcvBars.ticker == etf_ticker,
            OhlcvBars.timeframe == "1d",
            OhlcvBars.period_start >= _format_iso_utc(range_start_dt),
            OhlcvBars.period_start <= _format_iso_utc(range_end_dt),
        )
        .order_by(OhlcvBars.period_start)
    )
    return [float(c) for c in session.execute(stmt).scalars().all()]


def _z_score(value: float, mean: float, stdev: float) -> float:
    if stdev <= 0.0:
        return 0.0
    return (value - mean) / stdev


def _etf_volume_weakness(
    session: Session,
    *,
    etf_ticker: str,
    as_of_dt: datetime,
    volume_baseline_days: int,
) -> tuple[bool, float] | None:
    """Return ``(is_weak, recent_volume_z)`` for the ETF flow leg.

    Weakness fires when the trailing-``ETF_FLOW_WINDOW_DAYS`` mean volume
    sits below ``-DIVERGENCE_FLOW_SIGMA_THRESHOLD`` on the
    ``volume_baseline_days`` window AND the close price drifts down over
    the same ``ETF_FLOW_WINDOW_DAYS``.

    Returns ``None`` when the bar history is too thin to compute the
    z-score (i.e. fewer than ``ETF_FLOW_WINDOW_DAYS + 1`` bars or no
    baseline variance).
    """
    baseline_volumes = _select_etf_volume_series(
        session,
        etf_ticker=etf_ticker,
        range_start_dt=as_of_dt - timedelta(days=volume_baseline_days),
        range_end_dt=as_of_dt,
    )
    if len(baseline_volumes) < ETF_FLOW_WINDOW_DAYS + 1:
        return None
    recent_volumes = baseline_volumes[-ETF_FLOW_WINDOW_DAYS:]
    historical = baseline_volumes[:-ETF_FLOW_WINDOW_DAYS]
    if len(historical) < _MIN_VARIANCE_SAMPLES:
        return None
    recent_mean = float(fmean(recent_volumes))
    base_mean = float(fmean(historical))
    base_stdev = float(pstdev(historical)) if len(historical) > 1 else 0.0
    z = _z_score(recent_mean, base_mean, base_stdev)
    if z >= -DIVERGENCE_FLOW_SIGMA_THRESHOLD:
        return False, z
    closes = _select_etf_close_series(
        session,
        etf_ticker=etf_ticker,
        range_start_dt=as_of_dt - timedelta(days=ETF_FLOW_WINDOW_DAYS),
        range_end_dt=as_of_dt,
    )
    if len(closes) < _MIN_VARIANCE_SAMPLES:
        return None
    price_weakness = closes[-1] < closes[0]
    return (price_weakness, z)


def _select_top10_members(
    session: Session,
    *,
    etf_ticker: str,
) -> list[str]:
    """Return tickers flagged ``is_top_10`` for the ETF, latest weight_as_of."""
    latest_stmt = (
        select(EtfMembership.weight_as_of)
        .where(EtfMembership.etf_ticker == etf_ticker)
        .order_by(EtfMembership.weight_as_of.desc())
        .limit(1)
    )
    latest = session.execute(latest_stmt).scalar_one_or_none()
    if latest is None:
        return []
    member_stmt = (
        select(EtfMembership.ticker)
        .where(
            EtfMembership.etf_ticker == etf_ticker,
            EtfMembership.weight_as_of == latest,
            EtfMembership.is_top_10 == 1,
        )
        .order_by(EtfMembership.ticker)
    )
    return [t for t in session.execute(member_stmt).scalars().all()]


def _bto_call_volume_z(
    session: Session,
    *,
    ticker: str,
    as_of_dt: datetime,
    volume_baseline_days: int,
) -> float | None:
    """Z-score of trailing-``ETF_FLOW_WINDOW_DAYS`` BTO-proxy call volume.

    BTO proxy: total call ``volume_today`` summed across snapshots whose
    underlying is ``ticker`` over the recent and baseline windows. Story
    08e § Notes recommends recomputing the Q3 proxy on the fly rather
    than depending on 08b's ordering.

    Returns ``None`` when the baseline is too thin to compute the
    z-score.
    """
    baseline_start = as_of_dt - timedelta(days=volume_baseline_days)
    base_stmt = (
        select(
            OptionsContractSnapshots.snapshot_ts,
            OptionsContractSnapshots.volume_today,
        )
        .where(
            OptionsContractSnapshots.underlying_ticker == ticker,
            OptionsContractSnapshots.snapshot_ts >= _format_iso_utc(baseline_start),
            OptionsContractSnapshots.snapshot_ts <= _format_iso_utc(as_of_dt),
        )
        .order_by(OptionsContractSnapshots.snapshot_ts)
    )
    rows = session.execute(base_stmt).all()
    if not rows:
        return None
    by_day_total: dict[str, int] = defaultdict(int)
    for snapshot_ts, volume in rows:
        if volume is None:
            continue
        day = snapshot_ts[:_ISO_DATE_PREFIX_LENGTH]
        by_day_total[day] += int(volume)
    daily_volumes = [by_day_total[d] for d in sorted(by_day_total)]
    if len(daily_volumes) < ETF_FLOW_WINDOW_DAYS + 1:
        return None
    recent = daily_volumes[-ETF_FLOW_WINDOW_DAYS:]
    historical = daily_volumes[:-ETF_FLOW_WINDOW_DAYS]
    if len(historical) < _MIN_VARIANCE_SAMPLES:
        return None
    recent_mean = float(fmean(recent))
    base_mean = float(fmean(historical))
    base_stdev = float(pstdev(historical)) if len(historical) > 1 else 0.0
    return _z_score(recent_mean, base_mean, base_stdev)


def _detect_etf_vs_single_name_divergence(
    session: Session,
    *,
    as_of_dt: datetime,
    volume_baseline_days: int,
) -> list[tuple[str, dict[str, Any]]]:
    """Return ``[(etf_ticker, payload)]`` for every ETF that fires the divergence."""
    out: list[tuple[str, dict[str, Any]]] = []
    for etf_ticker in sorted(_ETF_TO_AUDIENCE):
        weakness = _etf_volume_weakness(
            session,
            etf_ticker=etf_ticker,
            as_of_dt=as_of_dt,
            volume_baseline_days=volume_baseline_days,
        )
        if weakness is None:
            continue
        is_weak, etf_z = weakness
        if not is_weak:
            continue
        members = _select_top10_members(session, etf_ticker=etf_ticker)
        constituent_z: dict[str, float] = {}
        for member in members:
            z = _bto_call_volume_z(
                session,
                ticker=member,
                as_of_dt=as_of_dt,
                volume_baseline_days=volume_baseline_days,
            )
            if z is None:
                continue
            constituent_z[member] = z
        firing_constituents = sorted(
            t for t, z in constituent_z.items() if z > DIVERGENCE_FLOW_SIGMA_THRESHOLD
        )
        if len(firing_constituents) < DIVERGENCE_BTO_MIN_CONSTITUENTS:
            continue
        out.append(
            (
                etf_ticker,
                {
                    "etf_ticker": etf_ticker,
                    "etf_volume_z": etf_z,
                    "firing_constituents": firing_constituents,
                    "constituent_bto_z": {t: constituent_z[t] for t in firing_constituents},
                    "attribution_method": ETF_FLOW_ATTRIBUTION_METHOD,
                },
            )
        )
    return out


# ---------------------------------------------------------------------------
# Output assembly
# ---------------------------------------------------------------------------


def _emit_event_novelty_blocks(
    *,
    cadence_per_ticker: Mapping[str, list[dict[str, Any]]],
    cluster_per_ticker: Mapping[str, list[dict[str, Any]]],
    sector_lookup: Mapping[str, str],
    freshness_ts: datetime,
) -> list[OutputBlock]:
    """Emit one ``q12.event_novelty`` block per audience.

    Both inputs are ``{ticker: [flag_record, ...]}`` per the pinned
    per-ticker payload convention; flags from the same ticker merge into
    a single payload entry.
    """
    by_audience: dict[OutputAudience, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )

    for source_map in (cadence_per_ticker, cluster_per_ticker):
        for ticker, flags in source_map.items():
            sector = sector_lookup.get(ticker)
            if sector is None:
                continue
            audience = _sector_audience(sector)
            if audience is None:
                continue
            by_audience[audience][ticker].extend(flags)

    blocks: list[OutputBlock] = []
    for audience in sorted(by_audience, key=lambda a: a.value):
        per_ticker = by_audience[audience]
        sorted_payload = {ticker: {"flags": per_ticker[ticker]} for ticker in sorted(per_ticker)}
        flag_count = sum(len(entry["flags"]) for entry in sorted_payload.values())
        anomaly_flags = (
            (
                AnomalyFlag(
                    # Subject = sector audience (not a symbol): keeps the entry_id
                    # unique when ≥ 2 audiences fire one run (ALP-934). The audience
                    # is not a ticker, so the flag is *not* registered ticker-bearing
                    # and its emitted ``ticker`` stays None.
                    name=f"q12_event_novelty:{audience.value}",
                    magnitude=float(flag_count),
                    severity="investigate_if_persists",
                ),
            )
            if flag_count
            else ()
        )
        blocks.append(
            OutputBlock(
                block_id="q12.event_novelty",
                audience=frozenset({audience}),
                freshness_ts=freshness_ts,
                calibration_state=CalibrationState.CALIBRATED,
                bootstrap_reason=None,
                payload={"per_ticker": sorted_payload},
                anomaly_flags=anomaly_flags,
                regime_context=None,
            )
        )
    return blocks


def _emit_recent_actions_block(
    *,
    actions: Sequence[CorporateActions],
    sector_lookup: Mapping[str, str],
    freshness_ts: datetime,
) -> list[OutputBlock]:
    by_audience: dict[OutputAudience, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for action in actions:
        sector = sector_lookup.get(action.ticker)
        if sector is None:
            continue
        audience = _sector_audience(sector)
        if audience is None:
            continue
        by_audience[audience][action.ticker].append(
            {
                "action_id": action.action_id,
                "action_type": action.action_type,
                "ex_date": action.ex_date,
                "cash_amount_per_share": action.cash_amount_per_share,
            }
        )
    blocks: list[OutputBlock] = []
    for audience in sorted(by_audience, key=lambda a: a.value):
        per_ticker = by_audience[audience]
        sorted_payload = {ticker: {"actions": per_ticker[ticker]} for ticker in sorted(per_ticker)}
        blocks.append(
            OutputBlock(
                block_id="q12.recent_corporate_actions",
                audience=frozenset({audience}),
                freshness_ts=freshness_ts,
                calibration_state=CalibrationState.CALIBRATED,
                bootstrap_reason=None,
                payload={"per_ticker": sorted_payload},
                anomaly_flags=(),
                regime_context=None,
            )
        )
    return blocks


def _emit_divergence_blocks(
    *,
    detections: Sequence[tuple[str, dict[str, Any]]],
    freshness_ts: datetime,
) -> list[OutputBlock]:
    blocks: list[OutputBlock] = []
    for etf_ticker, payload in detections:
        audience = _etf_audience(etf_ticker)
        if audience is None:
            continue
        per_ticker = {
            ticker: {"bto_z": payload["constituent_bto_z"][ticker]}
            for ticker in payload["firing_constituents"]
        }
        block_payload: dict[str, Any] = {
            "etf_ticker": etf_ticker,
            "etf_volume_z": payload["etf_volume_z"],
            "attribution_method": payload["attribution_method"],
            "per_ticker": per_ticker,
        }
        blocks.append(
            OutputBlock(
                block_id="q12.etf_vs_single_name_divergence",
                audience=frozenset({audience}),
                freshness_ts=freshness_ts,
                calibration_state=CalibrationState.CALIBRATED,
                bootstrap_reason=None,
                payload=block_payload,
                anomaly_flags=(
                    AnomalyFlag(
                        # Subject = the sector ETF (a symbol): keeps the entry_id
                        # unique when ≥ 2 ETFs fire one run (ALP-934). The ETF is a
                        # symbol, so the flag is registered ticker-bearing and the
                        # emitted ``ticker`` is the ETF (see aggregation.py).
                        name=f"etf_vs_single_name_divergence:{etf_ticker}",
                        magnitude=float(len(payload["firing_constituents"])),
                        severity="investigate_now",
                    ),
                ),
                regime_context=None,
            )
        )
    return blocks


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def detect_q12_signals(
    session: Session,
    *,
    as_of: str,
    volume_baseline_days: int,
) -> list[OutputBlock]:
    """Run every Q12 detection path and return the assembled :class:`OutputBlock` list.

    ``volume_baseline_days`` is the trailing window for the ETF flow proxy
    and the BTO proxy z-scores. Per the no-magic-numbers audit the
    orchestrator passes
    ``persistence_windows.volume_baseline_days`` from the loaded
    :class:`DistillationDomainConfig`; the function carries no default so a
    Class A bypass cannot occur.
    """
    as_of_dt = _parse_iso_utc(as_of)
    horizon_dt = as_of_dt + timedelta(days=CLUSTERING_WINDOW_DAYS)

    forward_events = _select_scheduled_events(
        session,
        as_of_dt=as_of_dt,
        horizon_dt=horizon_dt,
        event_types=CLUSTERING_EVENT_TYPES,
    )
    sector_lookup = _select_sector_lookup(session)
    cadence = _detect_unusual_event_cadence(
        session, as_of_dt=as_of_dt, forward_events=forward_events
    )
    clusters = _detect_event_clustering(forward_events=forward_events, sector_lookup=sector_lookup)

    freshness_ts = as_of_dt
    blocks: list[OutputBlock] = []
    blocks.extend(
        _emit_event_novelty_blocks(
            cadence_per_ticker=cadence,
            cluster_per_ticker=clusters,
            sector_lookup=sector_lookup,
            freshness_ts=freshness_ts,
        )
    )

    actions = _select_recent_actions(session, as_of_dt=as_of_dt)
    blocks.extend(
        _emit_recent_actions_block(
            actions=actions,
            sector_lookup=sector_lookup,
            freshness_ts=freshness_ts,
        )
    )

    divergence = _detect_etf_vs_single_name_divergence(
        session,
        as_of_dt=as_of_dt,
        volume_baseline_days=volume_baseline_days,
    )
    blocks.extend(_emit_divergence_blocks(detections=divergence, freshness_ts=freshness_ts))

    return blocks


__all__ = [
    "CLUSTERING_EVENT_TYPES",
    "CLUSTERING_MIN_EVENTS",
    "CLUSTERING_WINDOW_DAYS",
    "DIVERGENCE_BTO_MIN_CONSTITUENTS",
    "DIVERGENCE_FLOW_SIGMA_THRESHOLD",
    "ETF_FLOW_ATTRIBUTION_METHOD",
    "ETF_FLOW_WINDOW_DAYS",
    "RECENT_ACTIONS_WINDOW_TRADING_DAYS",
    "UNUSUAL_CADENCE_EVENT_TYPES",
    "UNUSUAL_EVENT_CADENCE_LOOKBACK_DAYS",
    "detect_q12_signals",
]
