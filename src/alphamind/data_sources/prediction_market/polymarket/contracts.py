"""
Polymarket prediction-market contract and snapshot collector.

Pulls active markets from Gamma's ``/markets`` endpoint (snapshot fidelity —
``outcomePrices``, ``liquidity``, ``bestBid/Ask``, ``volume24hr`` are populated
there but stripped from the ``markets[]`` embedded inside ``/events``), then
batch-fetches ``/events?id=...`` for the unique event ids referenced by those
markets to recover ``tags[].label`` for the shared categorizer.  Filters by
``liquidity_usd >= 10_000``, UPSERTs ``prediction_market_contracts``, and
writes one ``prediction_market_snapshots`` row per active contract per
invocation.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from alphamind.data_sources._common import default_session_factory, resume_since, track_run
from alphamind.data_sources.prediction_market.categories import (
    OTHER,
    derive_canonical_category,
)
from alphamind.persistence.models import (
    PredictionMarketContracts,
    PredictionMarketSnapshots,
)

log = logging.getLogger(__name__)

_PLATFORM = "polymarket"
_LIQUIDITY_MIN = 10_000.0
_RESOLVED_PRICE_THRESHOLD = 0.99


# ---------------------------------------------------------------------------
# Testable I/O helpers (injectable in tests via patch)
# ---------------------------------------------------------------------------


def _fetch_markets(_since: datetime) -> list[dict[str, Any]]:
    """Fetch all markets from Gamma.  Replaced by patch in tests."""
    from alphamind.data_sources.prediction_market.polymarket.client import PolymarketClient

    client = PolymarketClient()
    results: list[dict[str, Any]] = []
    offset = 0
    page_size = 100
    while True:
        try:
            page = client.get_markets(limit=page_size, offset=offset)
        except httpx.HTTPStatusError as exc:
            # Gamma /markets caps pagination (observed at offset=10100 → HTTP 422).
            # Treat that signal as end-of-pagination so we keep the markets we
            # collected; a 422 at the first request is still a real error.
            if exc.response.status_code == 422 and offset > 0:
                log.warning(
                    "polymarket: pagination capped at offset=%d (Gamma returned 422); "
                    "%d markets collected.",
                    offset,
                    len(results),
                )
                break
            raise
        if not page:
            break
        results.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    return results


def _fetch_event_labels(event_ids: list[str]) -> dict[str, tuple[str, ...]]:
    """Return ``event_id → (tag.label, ...)`` for the given event ids.

    Production implementation — replaced by patch in tests.
    """
    from alphamind.data_sources.prediction_market.polymarket.client import PolymarketClient

    client = PolymarketClient()
    events = client.get_events_by_ids(event_ids)
    result: dict[str, tuple[str, ...]] = {}
    for event in events:
        eid = str(event.get("id", "")) or None
        if eid is None:
            continue
        tags = event.get("tags") or []
        labels = tuple(tag.get("label", "") for tag in tags if tag.get("label"))
        result[eid] = labels
    return result


def _now() -> datetime:
    """Return current UTC time.  Replaced by patch in tests."""
    return datetime.now(UTC)


def _parse_outcome_prices(market: dict[str, Any]) -> list[float] | None:
    """Parse Gamma's JSON-encoded ``outcomePrices`` into floats."""
    raw = market.get("outcomePrices")
    if not raw:
        return None
    try:
        prices = json.loads(raw) if isinstance(raw, str) else list(raw)
    except (TypeError, ValueError):
        return None
    if not prices:
        return None
    try:
        return [float(p) for p in prices]
    except (TypeError, ValueError):
        return None


def _yes_probability_from_prices(prices: list[float]) -> float:
    """Return YES probability — first element of ``outcomePrices``."""
    return prices[0]


def _resolution_from_prices(prices: list[float], *, is_closed: bool) -> str | None:
    """Derive ``resolution_outcome`` from ``outcomePrices``.

    Polymarket's ``outcome`` field is universally null even for resolved
    markets; the live signal lives in ``outcomePrices`` (parallel to the
    ``["Yes", "No"]`` outcomes array).  When ``closed`` and the dominant
    leg is ≥0.99 the market resolved to that side; when both prices are
    zero Polymarket treats the market as canceled; otherwise we leave
    resolution unset until a future ingestion catches it cleanly.
    """
    if not is_closed or len(prices) < 2:
        return None
    if all(abs(p) < 1e-9 for p in prices):
        return "canceled"
    top = max(prices)
    if top < _RESOLVED_PRICE_THRESHOLD:
        return None
    return "yes" if prices[0] >= prices[1] else "no"


def _extract_event_ids(market: dict[str, Any]) -> tuple[str, ...]:
    """Return the event ids referenced by a Gamma market dict."""
    events = market.get("events") or []
    ids: list[str] = []
    for event in events:
        eid = event.get("id") if isinstance(event, dict) else None
        if eid is not None:
            ids.append(str(eid))
    return tuple(ids)


# ---------------------------------------------------------------------------
# Per-market processing
# ---------------------------------------------------------------------------


def _process_market(
    sess: Any,
    market: dict[str, Any],
    snapshot_ts: str,
    labels_by_event: dict[str, tuple[str, ...]],
) -> int:
    """Upsert a contract and write one snapshot row.  Returns 1 on insert, else 0."""
    condition_id: str = market["conditionId"]
    liquidity: float = float(market.get("liquidity") or 0.0)
    if liquidity < _LIQUIDITY_MIN:
        return 0

    prices = _parse_outcome_prices(market)
    if prices is None:
        log.warning(
            "polymarket: skipping contract %s — no parsable outcomePrices",
            condition_id,
        )
        return 0
    yes_prob = _yes_probability_from_prices(prices)

    description = market.get("question", "")
    event_ids = _extract_event_ids(market)
    vendor_labels: tuple[str, ...] = tuple(
        label for eid in event_ids for label in labels_by_event.get(eid, ())
    )
    category = derive_canonical_category(description=description, vendor_labels=vendor_labels)
    if category == OTHER:
        log.debug(
            "polymarket: contract %s categorized as 'other' (labels=%r)",
            condition_id,
            vendor_labels,
        )

    raw_bid = market.get("bestBid")
    raw_ask = market.get("bestAsk")
    bid: float | None = float(raw_bid) if raw_bid is not None else None
    ask: float | None = float(raw_ask) if raw_ask is not None else None
    volume_24h: float | None = float(market.get("volume24hr") or 0.0) or None

    is_closed: bool = bool(market.get("closed", False))
    resolution = _resolution_from_prices(prices, is_closed=is_closed)

    existing = sess.get(PredictionMarketContracts, condition_id)
    if existing is None:
        sess.add(
            PredictionMarketContracts(
                contract_id=condition_id,
                platform=_PLATFORM,
                description=description,
                category=category,
                resolution_date=market.get("endDate"),
                resolution_outcome=resolution,
                created_at=market.get("createdAt", snapshot_ts),
                last_seen_at=snapshot_ts,
            )
        )
    else:
        existing.last_seen_at = snapshot_ts
        existing.category = category
        existing.description = description
        if resolution is not None:
            existing.resolution_outcome = resolution

    if sess.get(PredictionMarketSnapshots, (condition_id, snapshot_ts)) is None:
        sess.add(
            PredictionMarketSnapshots(
                contract_id=condition_id,
                snapshot_ts=snapshot_ts,
                yes_probability=yes_prob,
                volume_24h_usd=volume_24h,
                liquidity_usd=liquidity,
                bid=bid,
                ask=ask,
                ingested_at=snapshot_ts,
            )
        )
        return 1
    return 0


# ---------------------------------------------------------------------------
# Main collection function
# ---------------------------------------------------------------------------


def collect_snapshots(
    since: datetime | None = None,
    *,
    _session_factory: Any = None,
    _repo: Any = None,
) -> None:
    """
    Pull active Polymarket markets, UPSERT contracts, write snapshots.

    Parameters
    ----------
    since:
        Lower-bound timestamp for the collection window (forward-only).
        Defaults to ``resume_since`` against ``prediction_market_snapshots``
        (1-hour lookback).  Accepted for runner-contract compatibility; the
        snapshot collector always captures current state.
    _session_factory:
        SQLAlchemy session factory override (for tests).
    _repo:
        ``track_run`` repo override (for tests).
    """
    if _session_factory is None:
        _session_factory = default_session_factory()

    if since is None:
        since = resume_since(
            column=PredictionMarketSnapshots.snapshot_ts,
            default_lookback=timedelta(hours=1),
            overlap=timedelta(0),
            session_factory=_session_factory,
        )

    with track_run("polymarket.contracts", _repo=_repo) as run:
        markets = _fetch_markets(since)
        snapshot_ts = _now().isoformat()

        unique_event_ids: list[str] = sorted(
            {eid for m in markets for eid in _extract_event_ids(m)}
        )
        labels_by_event = _fetch_event_labels(unique_event_ids)

        with _session_factory() as sess:
            rows_written = sum(
                _process_market(sess, m, snapshot_ts, labels_by_event) for m in markets
            )
            sess.commit()

        run.rows_written = rows_written
