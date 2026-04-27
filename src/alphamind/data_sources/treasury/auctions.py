"""Treasury auction data collection.

Pulls auction results from the Treasury Fiscal Data API for tenors of interest:
2Y, 5Y, 10Y, 30Y.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from alphamind.data_sources._common import track_run
from alphamind.data_sources.treasury.client import TreasuryClient
from alphamind.persistence.models import Base, TreasuryAuctions
from alphamind.persistence.session import make_engine, make_session_factory

_AUCTIONS_PATH = "/services/api/fiscal_service/v2/accounting/od/auctions_query"
_PAGE_SIZE = 100

# Maps API security_term values to storage short form
_TENOR_MAP: dict[str, str] = {
    "2-Year": "2Y",
    "5-Year": "5Y",
    "10-Year": "10Y",
    "30-Year": "30Y",
}

_client = TreasuryClient()


def _float_or_none(value: Any) -> float | None:
    """Parse a float from a string-or-None API field."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _fetch_all_pages(since: date) -> list[dict[str, Any]]:
    """Fetch all auction records from the API for the given date window."""
    security_terms = ",".join(_TENOR_MAP.keys())
    params: dict[str, Any] = {
        "fields": ",".join(
            [
                "record_date",
                "security_term",
                "high_yield",
                "bid_to_cover_ratio",
                "tail_basis_point",
                "primary_dealer_amt_pct",
                "indirect_bidder_amt_pct",
                "direct_bidder_amt_pct",
                "total_accepted_amt",
            ]
        ),
        "filter": f"record_date:gte:{since.isoformat()},security_term:in:({security_terms})",
        "page[size]": _PAGE_SIZE,
        "page[number]": 1,
    }

    all_records: list[dict[str, Any]] = []
    page_num = 1

    while True:
        params["page[number]"] = page_num
        response = _client.get(_AUCTIONS_PATH, params=params)
        data = response.get("data", [])
        all_records.extend(data)

        links = response.get("links", {})
        if not links.get("next"):
            break
        page_num += 1

    return all_records


def _record_to_row(record: dict[str, Any]) -> TreasuryAuctions | None:
    """Convert an API record dict to a TreasuryAuctions ORM row, or None if tenor unknown."""
    security_term = record.get("security_term", "")
    tenor = _TENOR_MAP.get(security_term)
    if tenor is None:
        return None

    auction_date = record.get("record_date", "")
    auction_id = f"{auction_date}_{tenor}"

    raw_yield = _float_or_none(record.get("high_yield"))
    auction_yield_bp = raw_yield * 100.0 if raw_yield is not None else None

    raw_size = _float_or_none(record.get("total_accepted_amt"))
    auction_size_usd = raw_size / 1000.0 if raw_size is not None else None

    return TreasuryAuctions(
        auction_id=auction_id,
        tenor=tenor,
        auction_date=auction_date,
        auction_yield_bp=auction_yield_bp,
        bid_to_cover=_float_or_none(record.get("bid_to_cover_ratio")),
        tail_bp=_float_or_none(record.get("tail_basis_point")),
        primary_dealer_pct=_float_or_none(record.get("primary_dealer_amt_pct")),
        indirect_pct=_float_or_none(record.get("indirect_bidder_amt_pct")),
        direct_pct=_float_or_none(record.get("direct_bidder_amt_pct")),
        auction_size_usd=auction_size_usd,
        source="treasury",
        ingested_at=datetime.now(UTC).isoformat(),
    )


def collect_auctions(
    since: date,
    *,
    _session_factory: Any = None,
    _repo: Any = None,
) -> None:
    """Collect Treasury auction results since *since* and write to treasury_auctions.

    Parameters
    ----------
    since:
        Start date for the collection window (inclusive).
    _session_factory:
        Optional session factory override for testing.
    _repo:
        Optional repository override for testing (passed to track_run).
    """
    if _session_factory is None:
        engine = make_engine()
        Base.metadata.create_all(engine)
        _session_factory = make_session_factory(engine)

    with track_run("treasury.auctions", _repo=_repo) as run:
        records = _fetch_all_pages(since)
        valid_rows = [r for r in (_record_to_row(rec) for rec in records) if r is not None]

        with _session_factory() as sess:
            for row in valid_rows:
                existing = sess.get(TreasuryAuctions, row.auction_id)
                if existing is None:
                    sess.add(row)
            sess.commit()

        run.rows_written = len(valid_rows)


def bootstrap_auctions(
    *,
    _session_factory: Any = None,
    _repo: Any = None,
) -> None:
    """Bootstrap 12 months of Treasury auction history."""
    today = datetime.now(UTC).date()
    twelve_months_ago = date(today.year - 1, today.month, today.day)
    collect_auctions(since=twelve_months_ago, _session_factory=_session_factory, _repo=_repo)
