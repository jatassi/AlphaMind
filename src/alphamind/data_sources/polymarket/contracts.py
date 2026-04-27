"""
Polymarket prediction-market contract and snapshot collector.

Pulls active markets from the Gamma API, filters to in-scope categories with
liquidity_usd >= 10_000, UPSERTs prediction_market_contracts, and writes one
prediction_market_snapshots row per active contract per invocation.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from alphamind.data_sources._common import track_run
from alphamind.persistence.models import (
    Base,
    PredictionMarketContracts,
    PredictionMarketSnapshots,
)
from alphamind.persistence.session import make_engine, make_session_factory

log = logging.getLogger(__name__)

_PLATFORM = "polymarket"
_LIQUIDITY_MIN = 10_000.0

# ---------------------------------------------------------------------------
# Category mapping
# ---------------------------------------------------------------------------
# Each entry is a set of lowercase substrings to match against the market's
# tags (case-insensitive).  First match wins; unknown → "other".

_CATEGORY_RULES: list[tuple[str, list[str]]] = [
    (
        "monetary_policy",
        ["fed", "fomc", "federal reserve", "rate cut", "rate hike", "interest rate"],
    ),
    ("antitrust", ["antitrust", "ftc", "doj", "monopoly", "competition"]),
    ("trade", ["tariff", "trade war", "trade deal", "import", "export control"]),
    (
        "financial_reg",
        ["sec ", "cfpb", "dodd-frank", "banking reg", "financial reg", "capital req"],
    ),
    ("tax", ["tax ", "irs ", "corporate tax", "tax reform", "tax cut"]),
    (
        "election",
        [
            "election",
            "president",
            "congress",
            "senate",
            "ballot",
            "vote",
            "democrat",
            "republican",
        ],
    ),
    ("opec", ["opec", "oil production", "petroleum"]),
    ("conflict", ["war", "conflict", "military", "invasion", "nato", "ukraine", "russia"]),
    ("sanctions", ["sanction", "embargo", "export ban"]),
]


def _derive_category(tags: list[str]) -> str:
    """Return the AlphaMind category for the given Polymarket tag list."""
    lowered = " ".join(t.lower() for t in tags)
    for category, keywords in _CATEGORY_RULES:
        if any(kw in lowered for kw in keywords):
            return category
    return "other"


# ---------------------------------------------------------------------------
# Testable I/O helpers (injectable in tests via patch)
# ---------------------------------------------------------------------------


def _fetch_markets(_since: datetime) -> list[dict[str, Any]]:
    """
    Fetch all active markets from the Gamma API.

    Production implementation — replaced by patch in tests.
    """
    from alphamind.data_sources.polymarket.client import PolymarketClient

    client = PolymarketClient()
    results: list[dict[str, Any]] = []
    offset = 0
    page_size = 100
    while True:
        page = client.get_markets(limit=page_size, offset=offset)
        if not page:
            break
        results.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    return results


def _fetch_prices(condition_id: str) -> dict[str, Any] | None:
    """
    Fetch YES/NO prices for a contract from the CLOB API.

    Production implementation — replaced by patch in tests.
    """
    from alphamind.data_sources.polymarket.client import PolymarketClient

    client = PolymarketClient()
    return client.get_prices(condition_id)


def _now() -> datetime:
    """Return current UTC time.  Replaced by patch in tests."""
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Main collection function
# ---------------------------------------------------------------------------


def collect_snapshots(
    since: datetime,
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
    _session_factory:
        SQLAlchemy session factory override (for tests).
    _repo:
        ``track_run`` repo override (for tests).
    """
    if _session_factory is None:
        engine = make_engine()
        Base.metadata.create_all(engine)
        _session_factory = make_session_factory(engine)

    with track_run("polymarket.contracts", _repo=_repo) as run:
        markets = _fetch_markets(since)
        snapshot_ts = _now().isoformat()
        rows_written = 0

        with _session_factory() as sess:
            for market in markets:
                condition_id: str = market["conditionId"]
                liquidity: float = float(market.get("liquidity") or 0.0)

                # --- Liquidity gate ---
                if liquidity < _LIQUIDITY_MIN:
                    continue

                # --- Category derivation ---
                tags: list[str] = market.get("tags") or []
                category = _derive_category(tags)
                if category == "other":
                    log.warning(
                        "polymarket: unknown category for contract %s tags=%r"
                        " — defaulting to 'other'",
                        condition_id,
                        tags,
                    )

                # --- Prices ---
                prices = _fetch_prices(condition_id)

                # Non-binary market detection
                if prices is None or "yes" not in prices:
                    log.warning(
                        "polymarket: skipping non-binary contract %s (no 'yes' price in response)",
                        condition_id,
                    )
                    continue

                yes_prob: float = float(prices["yes"])
                bid: float | None = float(prices["bid"]) if prices.get("bid") is not None else None
                ask: float | None = float(prices["ask"]) if prices.get("ask") is not None else None
                volume_24h: float | None = float(market.get("volume24hr") or 0.0) or None

                # --- Closed / resolution ---
                is_closed: bool = bool(market.get("closed", False))
                raw_outcome: str | None = market.get("outcome")
                if is_closed:
                    resolution: str | None = raw_outcome.lower() if raw_outcome else "undecided"
                else:
                    resolution = None

                # --- Upsert contract ---
                existing = sess.get(PredictionMarketContracts, condition_id)
                if existing is None:
                    contract = PredictionMarketContracts(
                        contract_id=condition_id,
                        platform=_PLATFORM,
                        description=market.get("question", ""),
                        category=category,
                        resolution_date=market.get("endDate"),
                        resolution_outcome=resolution,
                        created_at=market.get("createdAt", snapshot_ts),
                        last_seen_at=snapshot_ts,
                    )
                    sess.add(contract)
                else:
                    existing.last_seen_at = snapshot_ts
                    existing.category = category
                    if resolution is not None:
                        existing.resolution_outcome = resolution

                # --- Write snapshot (INSERT OR IGNORE via merge) ---
                snap_pk = (condition_id, snapshot_ts)
                existing_snap = sess.get(PredictionMarketSnapshots, snap_pk)
                if existing_snap is None:
                    snap = PredictionMarketSnapshots(
                        contract_id=condition_id,
                        snapshot_ts=snapshot_ts,
                        yes_probability=yes_prob,
                        volume_24h_usd=volume_24h,
                        liquidity_usd=liquidity,
                        bid=bid,
                        ask=ask,
                        ingested_at=snapshot_ts,
                    )
                    sess.add(snap)
                    rows_written += 1

            sess.commit()

        run.rows_written = rows_written
