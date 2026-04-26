"""
Kalshi prediction-market contract collector.

Pulls events from ``/trade-api/v2/events``, then fetches markets per event
via ``/trade-api/v2/markets?series_ticker=...``.

UPSERTs into ``prediction_market_contracts``; writes
``prediction_market_snapshots`` per active market.

``liquidity_usd`` is approximated as ``volume × last_price / 100`` (cents →
dollars), since Kalshi does not expose open-interest directly in the markets
endpoint.  This is noted in the module docstring so callers understand the
proxy.

``contract_id`` is Kalshi's ``market_ticker`` — the unique identifier for a
single binary market.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from alphamind.data_sources._common import track_run
from alphamind.data_sources.kalshi.client import KalshiClient
from alphamind.persistence.models import PredictionMarketContracts, PredictionMarketSnapshots

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Series ticker → storage category mapping
# ---------------------------------------------------------------------------

#: Maps Kalshi ``series_ticker`` prefixes to ``prediction_market_contracts.category``.
SERIES_CATEGORY_MAP: dict[str, str] = {
    "FED": "monetary_policy",
    "FOMC": "monetary_policy",
    "CPI": "inflation",
    "PCE": "inflation",
    "OPEC": "geopolitical",
    "OIL": "geopolitical",
    "ELECTION": "political",
    "PRES": "political",
    "SENATE": "political",
    "HOUSE": "political",
    "ANTITRUST": "regulatory",
    "SEC": "regulatory",
    "TRADE": "regulatory",
    "TARIFF": "regulatory",
    "GDP": "macro",
    "JOBS": "macro",
    "UNEMPLOYMENT": "macro",
}


def _derive_category(series_ticker: str) -> str:
    """Map a Kalshi series_ticker prefix to a storage category."""
    category = SERIES_CATEGORY_MAP.get(series_ticker)
    if category is None:
        logger.warning(
            "Unknown Kalshi series_ticker %r — defaulting category to 'other'.",
            series_ticker,
        )
        return "other"
    return category


def _map_result(result: str | None) -> str | None:
    """Map Kalshi result field to storage resolution_outcome."""
    if result is None:
        return None
    result_lower = result.lower()
    if result_lower in ("yes", "no"):
        return result_lower
    return "undecided"


def collect_snapshots(
    since: str,
    *,
    client: KalshiClient,
    session: Session,
    _repo: Any = None,
    _snapshot_ts: str | None = None,
) -> None:
    """
    Collect Kalshi prediction-market contracts and snapshots.

    Fetches all events, then for each event fetches its markets.  UPSERTs
    ``prediction_market_contracts`` rows and appends new
    ``prediction_market_snapshots`` rows.  Uses ``track_run`` to record the
    run in ``collection_runs``.

    Parameters
    ----------
    since:
        ISO 8601 timestamp used as a lower-bound filter hint (passed to the
        events endpoint; Kalshi ignores unknown query params gracefully).
    client:
        Authenticated :class:`~alphamind.data_sources.kalshi.client.KalshiClient`.
    session:
        SQLAlchemy :class:`~sqlalchemy.orm.Session` bound to the target DB.
    _repo:
        Optional repository override for ``track_run`` (for testing).
    _snapshot_ts:
        Optional fixed snapshot timestamp (for deterministic testing).
    """
    ingested_at = datetime.now(UTC).isoformat()
    snapshot_ts = _snapshot_ts or ingested_at

    with track_run("kalshi.contracts", _repo=_repo) as run:
        events_payload = client.get("/events")
        events: list[dict] = events_payload.get("events", [])

        rows_written = 0

        for event in events:
            series_ticker: str = event.get("series_ticker", "")
            category = _derive_category(series_ticker)

            markets_payload = client.get("/markets", series_ticker=series_ticker)
            markets: list[dict] = markets_payload.get("markets", [])

            for market in markets:
                contract_id: str = market["ticker"]
                description: str = market.get("title", "")
                close_time: str | None = market.get("close_time")
                open_time: str | None = market.get("open_time", ingested_at)
                market_status: str = market.get("status", "active")
                result: str | None = market.get("result")

                resolution_outcome = (
                    _map_result(result)
                    if market_status in ("closed", "finalized")
                    else None
                )

                # UPSERT contract (slow-changing reference row)
                stmt = sqlite_insert(PredictionMarketContracts).values(
                    contract_id=contract_id,
                    platform="kalshi",
                    description=description,
                    category=category,
                    resolution_date=close_time,
                    resolution_outcome=resolution_outcome,
                    created_at=open_time,
                    last_seen_at=ingested_at,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["contract_id"],
                    set_={
                        "resolution_outcome": resolution_outcome,
                        "last_seen_at": ingested_at,
                        "description": description,
                        "category": category,
                        "resolution_date": close_time,
                    },
                )
                session.execute(stmt)

                # Snapshot row — primary key (contract_id, snapshot_ts) prevents duplicates
                yes_bid: int = market.get("yes_bid", 0)
                yes_ask: int = market.get("yes_ask", 0)
                yes_probability = (yes_bid + yes_ask) / 200.0
                bid = yes_bid / 100.0
                ask = yes_ask / 100.0
                volume: int = market.get("volume", 0)
                last_price: int = market.get("last_price", 0)
                liquidity_usd = volume * last_price / 100.0

                snap_stmt = sqlite_insert(PredictionMarketSnapshots).values(
                    contract_id=contract_id,
                    snapshot_ts=snapshot_ts,
                    yes_probability=yes_probability,
                    volume_24h_usd=None,
                    liquidity_usd=liquidity_usd,
                    bid=bid,
                    ask=ask,
                    ingested_at=ingested_at,
                )
                snap_stmt = snap_stmt.on_conflict_do_nothing(
                    index_elements=["contract_id", "snapshot_ts"]
                )
                result_proxy = session.execute(snap_stmt)
                if result_proxy.rowcount > 0:
                    rows_written += 1

        session.commit()
        run.rows_written = rows_written
