"""
Kalshi prediction-market contract collector.

Pulls events from ``/trade-api/v2/events``, then fetches markets per event
via ``/trade-api/v2/markets?series_ticker=...``.

UPSERTs into ``prediction_market_contracts``; writes
``prediction_market_snapshots`` per active market.

``liquidity_usd`` is approximated as ``volume x last_price / 100`` (cents →
dollars), since Kalshi does not expose open-interest directly in the markets
endpoint.

``contract_id`` is Kalshi's ``market_ticker`` — the unique identifier for a
single binary market.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from alphamind.data_sources._common import default_session_factory, track_run
from alphamind.data_sources.prediction_market.categories import derive_canonical_category
from alphamind.data_sources.prediction_market.kalshi._protocol import KalshiAPI
from alphamind.data_sources.prediction_market.kalshi.client import KalshiClient
from alphamind.persistence.models import PredictionMarketContracts, PredictionMarketSnapshots

logger = logging.getLogger(__name__)


def _map_result(result: str | None) -> str | None:
    """Map Kalshi result field to storage resolution_outcome."""
    if result is None:
        return None
    result_lower = result.lower()
    if result_lower in ("yes", "no"):
        return result_lower
    return "undecided"


def collect_snapshots(
    since: str | None = None,
    *,
    client: KalshiAPI | None = None,
    session_factory: Any = None,
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
        Accepted for runner-contract compatibility; Kalshi snapshots always
        reflect current state so its value does not affect collection.
    client:
        :class:`~alphamind.data_sources.prediction_market.kalshi.client.KalshiClient`.
        Defaults to ``KalshiClient()`` which reads ``KALSHI_EMAIL`` and
        ``KALSHI_PASSWORD`` from the environment.
    session_factory:
        SQLAlchemy session factory bound to the target DB.
        Defaults to ``default_session_factory()``.
    _repo:
        Optional repository override for ``track_run`` (for testing).
    _snapshot_ts:
        Optional fixed snapshot timestamp (for deterministic testing).
    """
    if client is None:
        client = KalshiClient()
    if session_factory is None:
        session_factory = default_session_factory()
    del since  # accepted for runner-contract compatibility; ignored by snapshot semantics

    ingested_at = datetime.now(UTC).isoformat()
    snapshot_ts = _snapshot_ts or ingested_at

    with track_run("kalshi.contracts", _repo=_repo) as run:
        events_payload = client.get("/events")
        events: list[dict[str, Any]] = events_payload.get("events", [])

        rows_written = 0

        # Commit per event so the SQLite writer lock is held only during the
        # brief INSERTs, not across the slow rate-limited HTTP cycle (Kalshi is
        # capped at 30 req/min, so a full sweep can take ~5 min). Without this,
        # other vendor collectors firing concurrently exhausted busy_timeout.
        for event in events:
            series_ticker: str = event.get("series_ticker", "")
            event_category: str = event.get("category", "") or ""
            vendor_labels: tuple[str, ...] = (event_category,) if event_category else ()

            markets_payload = client.get("/markets", series_ticker=series_ticker)
            markets: list[dict[str, Any]] = markets_payload.get("markets", [])

            with session_factory() as session:
                for market in markets:
                    contract_id: str = market["ticker"]
                    description: str = market.get("title", "")
                    close_time: str | None = market.get("close_time")
                    open_time: str | None = market.get("open_time", ingested_at)
                    market_status: str = market.get("status", "active")
                    result: str | None = market.get("result")

                    category = derive_canonical_category(
                        description=description, vendor_labels=vendor_labels
                    )

                    resolution_outcome = (
                        _map_result(result) if market_status in ("closed", "finalized") else None
                    )

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
