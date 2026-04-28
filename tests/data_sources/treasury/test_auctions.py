"""Tests for treasury auction collection."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind.persistence.models import Base, CollectionRuns, TreasuryAuctions
from alphamind.persistence.session import make_engine, make_session_factory

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine() -> Engine:
    """In-memory SQLite engine with all tables created."""
    e: Engine = make_engine(":memory:")
    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def session_factory(engine: Engine) -> type[Session]:
    """Session factory bound to in-memory engine."""
    sf: type[Session] = make_session_factory(engine)
    return sf


@pytest.fixture()
def fake_repo(engine: Engine, session_factory: type[Session]) -> Any:
    """A test double for _DefaultRepo that uses the in-memory database."""
    from alphamind.persistence.models import CollectionRuns

    class FakeRepo:
        def __init__(self) -> None:
            self._Session = session_factory
            self._model = CollectionRuns

        def insert_running(self, run_id: str, collector: str, started_at: str) -> None:
            with self._Session() as sess:
                sess.add(
                    CollectionRuns(
                        run_id=run_id,
                        collector=collector,
                        started_at=started_at,
                        status="running",
                    )
                )
                sess.commit()

        def update_success(self, run_id: str, completed_at: str, rows_written: int) -> None:
            with self._Session() as sess:
                row = sess.get(CollectionRuns, run_id)
                if row is not None:
                    row.status = "success"
                    row.completed_at = completed_at
                    row.rows_written = rows_written
                    sess.commit()

        def update_failed(self, run_id: str, error_summary: str) -> None:
            with self._Session() as sess:
                row = sess.get(CollectionRuns, run_id)
                if row is not None:
                    row.status = "failed"
                    row.error_summary = error_summary
                    sess.commit()

    return FakeRepo()


def _make_api_page(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a fake Treasury API response page."""
    return {
        "data": records,
        "meta": {
            "count": len(records),
            "total-count": len(records),
            "total-pages": 1,
        },
        "links": {
            "next": None,
            "prev": None,
        },
    }


def _auction_record(
    record_date: str = "2026-03-15",
    security_term: str = "10-Year",
    high_yield: str = "4.25",
    avg_med_yield: str = "4.245",
    bid_to_cover_ratio: str = "2.35",
    primary_dealer_accepted: str = "4_375_000_000",
    indirect_bidder_accepted: str = "22_855_000_000",
    direct_bidder_accepted: str = "7_770_000_000",
    total_accepted: str = "35_000_000_000",
) -> dict[str, Any]:
    """Build a single fake auction record with defaults matching the live v1 API shape."""
    return {
        "record_date": record_date,
        "security_term": security_term,
        "high_yield": high_yield,
        "avg_med_yield": avg_med_yield,
        "bid_to_cover_ratio": bid_to_cover_ratio,
        "primary_dealer_accepted": primary_dealer_accepted,
        "indirect_bidder_accepted": indirect_bidder_accepted,
        "direct_bidder_accepted": direct_bidder_accepted,
        "total_accepted": total_accepted,
    }


# ---------------------------------------------------------------------------
# collect_auctions: basic row writing
# ---------------------------------------------------------------------------


class TestCollectAuctions:
    """collect_auctions writes correct rows to treasury_auctions."""

    def test_writes_rows_for_all_four_tenors(
        self, session_factory: type[Session], fake_repo: Any
    ) -> None:
        """collect_auctions writes one row per auction across 2Y/5Y/10Y/30Y."""
        from alphamind.data_sources.treasury.auctions import collect_auctions

        records = [
            _auction_record(security_term="2-Year", record_date="2026-03-10"),
            _auction_record(security_term="5-Year", record_date="2026-03-12"),
            _auction_record(security_term="10-Year", record_date="2026-03-15"),
            _auction_record(security_term="30-Year", record_date="2026-03-20"),
        ]
        page = _make_api_page(records)

        with patch(
            "alphamind.data_sources.treasury.auctions._client.get",
            return_value=page,
        ):
            collect_auctions(
                since=date(2026, 3, 1),
                _session_factory=session_factory,
                _repo=fake_repo,
            )

        with session_factory() as sess:
            rows = sess.execute(select(TreasuryAuctions)).scalars().all()

        assert len(rows) == 4
        tenors = {r.tenor for r in rows}
        assert tenors == {"2Y", "5Y", "10Y", "30Y"}

    def test_auction_id_format(self, session_factory: type[Session], fake_repo: Any) -> None:
        """auction_id is {auction_date}_{tenor}."""
        from alphamind.data_sources.treasury.auctions import collect_auctions

        page = _make_api_page([_auction_record(record_date="2026-03-15", security_term="10-Year")])

        with patch(
            "alphamind.data_sources.treasury.auctions._client.get",
            return_value=page,
        ):
            collect_auctions(
                since=date(2026, 3, 1),
                _session_factory=session_factory,
                _repo=fake_repo,
            )

        with session_factory() as sess:
            row = sess.get(TreasuryAuctions, "2026-03-15_10Y")

        assert row is not None
        assert row.auction_id == "2026-03-15_10Y"

    def test_field_mapping(self, session_factory: type[Session], fake_repo: Any) -> None:
        """Fields are correctly mapped from API names to schema column names."""
        from alphamind.data_sources.treasury.auctions import collect_auctions

        page = _make_api_page(
            [
                _auction_record(
                    record_date="2026-03-15",
                    security_term="10-Year",
                    high_yield="4.25",
                    avg_med_yield="4.245",
                    bid_to_cover_ratio="2.35",
                    primary_dealer_accepted="4_375_000_000",
                    indirect_bidder_accepted="22_855_000_000",
                    direct_bidder_accepted="7_770_000_000",
                    total_accepted="35_000_000_000",
                )
            ]
        )

        with patch(
            "alphamind.data_sources.treasury.auctions._client.get",
            return_value=page,
        ):
            collect_auctions(
                since=date(2026, 3, 1),
                _session_factory=session_factory,
                _repo=fake_repo,
            )

        with session_factory() as sess:
            row = sess.get(TreasuryAuctions, "2026-03-15_10Y")

        assert row is not None
        assert row.auction_date == "2026-03-15"
        assert row.tenor == "10Y"
        assert row.auction_yield_bp == pytest.approx(425.0)  # 4.25 * 100
        assert row.bid_to_cover == pytest.approx(2.35)
        assert row.tail_bp == pytest.approx(0.5)  # (4.25 - 4.245) * 100
        assert row.primary_dealer_pct == pytest.approx(12.5)  # 4.375B / 35B * 100
        assert row.indirect_pct == pytest.approx(65.3)  # 22.855B / 35B * 100
        assert row.direct_pct == pytest.approx(22.2)  # 7.77B / 35B * 100
        assert row.auction_size_usd == pytest.approx(35.0)  # 35B / 1e9
        assert row.source == "treasury"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    """Re-running on the same window produces no duplicate rows."""

    def test_rerun_does_not_duplicate(self, session_factory: type[Session], fake_repo: Any) -> None:
        """Second collect_auctions on same window leaves row count unchanged."""
        from alphamind.data_sources.treasury.auctions import collect_auctions

        page = _make_api_page([_auction_record(record_date="2026-03-15", security_term="10-Year")])

        with patch(
            "alphamind.data_sources.treasury.auctions._client.get",
            return_value=page,
        ):
            collect_auctions(
                since=date(2026, 3, 1),
                _session_factory=session_factory,
                _repo=fake_repo,
            )
            collect_auctions(
                since=date(2026, 3, 1),
                _session_factory=session_factory,
                _repo=fake_repo,
            )

        with session_factory() as sess:
            rows = sess.execute(select(TreasuryAuctions)).scalars().all()

        assert len(rows) == 1


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


class TestFailureHandling:
    """On API failure, collection_runs records failed; no data rows written."""

    def test_api_error_records_failed_run(
        self, session_factory: type[Session], fake_repo: Any
    ) -> None:
        """HTTP error marks collection_runs as failed and writes no auction rows."""
        from alphamind.data_sources.treasury.auctions import collect_auctions

        with (
            patch(
                "alphamind.data_sources.treasury.auctions._client.get",
                side_effect=Exception("API down"),
            ),
            pytest.raises(Exception, match="API down"),
        ):
            collect_auctions(
                since=date(2026, 3, 1),
                _session_factory=session_factory,
                _repo=fake_repo,
            )

        with session_factory() as sess:
            runs = sess.execute(select(CollectionRuns)).scalars().all()
            auctions = sess.execute(select(TreasuryAuctions)).scalars().all()

        assert len(auctions) == 0
        assert len(runs) == 1
        assert runs[0].status == "failed"
        assert "API down" in (runs[0].error_summary or "")


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


class TestPagination:
    """collect_auctions follows pagination to retrieve all records."""

    def test_fetches_multiple_pages(self, session_factory: type[Session], fake_repo: Any) -> None:
        """When API returns next-page link, all pages are fetched."""
        from alphamind.data_sources.treasury.auctions import collect_auctions

        page1 = {
            "data": [_auction_record(record_date="2026-03-10", security_term="10-Year")],
            "meta": {"count": 1, "total-count": 2, "total-pages": 2},
            "links": {"next": "page2", "prev": None},
        }
        page2 = {
            "data": [_auction_record(record_date="2026-03-15", security_term="10-Year")],
            "meta": {"count": 1, "total-count": 2, "total-pages": 2},
            "links": {"next": None, "prev": "page1"},
        }

        call_count = 0

        def fake_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return page1
            return page2

        with patch(
            "alphamind.data_sources.treasury.auctions._client.get",
            side_effect=fake_get,
        ):
            collect_auctions(
                since=date(2026, 3, 1),
                _session_factory=session_factory,
                _repo=fake_repo,
            )

        with session_factory() as sess:
            rows = sess.execute(select(TreasuryAuctions)).scalars().all()

        assert len(rows) == 2
        assert call_count == 2


# ---------------------------------------------------------------------------
# bootstrap_auctions: 12-month depth
# ---------------------------------------------------------------------------


class TestCollectAuctionsNoArgs:
    """collect_auctions is callable with no positional args (cron registry contract)."""

    def test_callable_with_no_args(self, session_factory: type[Session], fake_repo: Any) -> None:
        page = _make_api_page([])

        with patch(
            "alphamind.data_sources.treasury.auctions._client.get",
            return_value=page,
        ):
            from alphamind.data_sources.treasury.auctions import collect_auctions

            # No since — must not raise
            collect_auctions(
                _session_factory=session_factory,
                _repo=fake_repo,
            )


class TestBootstrapAuctions:
    """bootstrap_auctions covers 12 months of history."""

    def test_bootstrap_covers_12_months(
        self, session_factory: type[Session], fake_repo: Any
    ) -> None:
        """bootstrap_auctions calls collect with a since date 12 months back."""
        from alphamind.data_sources.treasury import auctions

        captured_since: list[date] = []

        def fake_collect(since: date, _session_factory: Any = None, _repo: Any = None) -> None:
            captured_since.append(since)

        with patch.object(auctions, "collect_auctions", side_effect=fake_collect):
            auctions.bootstrap_auctions(_session_factory=session_factory, _repo=fake_repo)

        assert len(captured_since) == 1
        today = datetime.now(UTC).date()
        twelve_months_ago = date(today.year - 1, today.month, today.day)
        delta = abs((captured_since[0] - twelve_months_ago).days)
        assert delta <= 1  # within one day of 12 months back
