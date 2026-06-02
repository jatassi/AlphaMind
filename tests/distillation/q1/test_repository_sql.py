"""Tests for SqlDistillationRepository — the concrete production reader.

The Sql impl closes over a SQLAlchemy ``Session`` and returns the frozen
dataclass projections declared in
:mod:`alphamind.distillation._repository`. The IO contract is exercised
end-to-end against an in-memory SQLite database so the production reader
is verified independently of the compute consumers.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from alphamind._kernel.ids import Symbol
from alphamind.distillation._repository_sql import SqlDistillationRepository
from alphamind.persistence.models import (
    AssetUniverse,
    DistillationEventHistory,
    OhlcvBars,
    SectorClassification,
)


def _add_ticker(session: Session, ticker: str, *, sector: str = "tech") -> None:
    session.add(
        AssetUniverse(
            asset_id=f"asset-{ticker.lower()}",
            ticker=ticker,
            full_name=f"{ticker} Holdings",
            asset_class="equity",
            asset_role="universe",
            exchange="NASDAQ",
            is_active=1,
            added_date="2020-01-01",
            last_updated="2026-04-26T00:00:00Z",
        )
    )
    session.add(
        SectorClassification(
            ticker=ticker,
            asset_id=f"asset-{ticker.lower()}",
            alphamind_sector=sector,
            domain_researcher="tech_semis",
            sector_etf="XLK",
            classification_source="manual",
            last_updated="2026-04-26T00:00:00Z",
        )
    )


def _add_gap_event(
    session: Session,
    *,
    ticker: str,
    event_ts: str,
    outcome: str,
) -> None:
    session.add(
        DistillationEventHistory(
            ticker=ticker,
            event_kind="gap",
            event_ts=event_ts,
            direction="up",
            magnitude_atr_multiple=2.0,
            outcome=outcome,
            outcome_observed_at=event_ts if outcome != "pending" else None,
            ingested_at=event_ts,
        )
    )


def _add_bar(
    session: Session,
    *,
    ticker: str,
    period_start: str,
    close: float = 100.0,
) -> None:
    session.add(
        OhlcvBars(
            ticker=ticker,
            timeframe="1d",
            period_start=period_start,
            period_end=period_start,
            session="regular",
            adj_open=close,
            adj_high=close + 1.0,
            adj_low=close - 1.0,
            adj_close=close,
            adj_volume=1_000_000,
            unadj_open=close,
            unadj_high=close + 1.0,
            unadj_low=close - 1.0,
            unadj_close=close,
            unadj_volume=1_000_000,
            source="polygon",
            ingested_at=period_start,
        )
    )


class TestSqlRepositoryGapHistory:
    def test_gap_fill_counts_aggregate_resolved_and_filled(self, session: Session) -> None:
        _add_ticker(session, "AAPL")
        # 3 filled + 2 unfilled = 5 resolved, 3 filled, 1 pending.
        for i in range(3):
            _add_gap_event(
                session, ticker=Symbol("AAPL"), event_ts=f"2026-03-01T00:0{i}:00Z", outcome="filled"
            )
        for i in range(2):
            _add_gap_event(
                session,
                ticker=Symbol("AAPL"),
                event_ts=f"2026-03-02T00:0{i}:00Z",
                outcome="unfilled",
            )
        # Pending event must not count toward resolved.
        _add_gap_event(
            session, ticker=Symbol("AAPL"), event_ts="2026-04-01T00:00:00Z", outcome="pending"
        )
        session.commit()

        repo = SqlDistillationRepository(session)
        counts = repo.load_gap_fill_event_counts(
            ticker=Symbol("AAPL"), as_of="2026-04-25T00:00:00Z"
        )
        assert counts.resolved == 5
        assert counts.filled == 3
        assert counts.pending == 1

    def test_gap_fill_counts_pending_only(self, session: Session) -> None:
        """All-pending history: 0 resolved, 0 filled, N pending."""
        _add_ticker(session, "AAPL")
        for i in range(4):
            _add_gap_event(
                session,
                ticker=Symbol("AAPL"),
                event_ts=f"2026-04-0{i + 1}T00:00:00Z",
                outcome="pending",
            )
        session.commit()

        repo = SqlDistillationRepository(session)
        counts = repo.load_gap_fill_event_counts(
            ticker=Symbol("AAPL"), as_of="2026-04-25T00:00:00Z"
        )
        assert counts.resolved == 0
        assert counts.filled == 0
        assert counts.pending == 4

    def test_sector_pooled_counts_track_pending(self, session: Session) -> None:
        """Sector pool sums pending across all tickers in the sector."""
        _add_ticker(session, "AAPL", sector="tech")
        _add_ticker(session, "MSFT", sector="tech")
        _add_gap_event(
            session, ticker=Symbol("AAPL"), event_ts="2026-04-01T00:00:00Z", outcome="pending"
        )
        _add_gap_event(
            session, ticker=Symbol("MSFT"), event_ts="2026-04-02T00:00:00Z", outcome="pending"
        )
        _add_gap_event(
            session, ticker=Symbol("MSFT"), event_ts="2026-03-01T00:00:00Z", outcome="filled"
        )
        session.commit()

        repo = SqlDistillationRepository(session)
        counts = repo.load_sector_pooled_gap_fill_counts(
            sector="tech", as_of="2026-04-25T00:00:00Z"
        )
        assert counts.resolved == 1
        assert counts.filled == 1
        assert counts.pending == 2


class TestSqlRepositoryBars:
    def test_daily_bars_returned_in_chronological_order(self, session: Session) -> None:
        _add_ticker(session, "AAPL")
        for day in range(1, 6):
            _add_bar(
                session,
                ticker=Symbol("AAPL"),
                period_start=f"2026-04-{day:02d}T00:00:00Z",
                close=100.0 + day,
            )
        session.commit()

        repo = SqlDistillationRepository(session)
        bars = repo.load_daily_bars(ticker=Symbol("AAPL"), as_of="2026-04-25T00:00:00Z", days=3)
        assert len(bars) == 3
        # Returned in chronological order, latest last.
        assert bars[0].period_start == "2026-04-03T00:00:00Z"
        assert bars[-1].period_start == "2026-04-05T00:00:00Z"


class TestSqlRepositorySectorClassifications:
    def test_default_scope_filters_to_audience_sectors(self, session: Session) -> None:
        _add_ticker(session, "AAPL", sector="tech")
        _add_ticker(session, "JPM", sector="financials")
        _add_ticker(session, "FOO", sector="real_estate")  # not in audience
        session.commit()

        repo = SqlDistillationRepository(session)
        scope = repo.load_default_ticker_scope()
        assert "AAPL" in scope
        assert "JPM" in scope
        assert "FOO" not in scope
