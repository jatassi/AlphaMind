"""Tests for qualitative-research in-context input loaders — ALP-246.

Each loader is tested against a populated in-memory SQLite fixture so that
tests exercise real query paths rather than mocked internals.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind._kernel.ids import Symbol, ThesisId
from alphamind.analysis._shared import Sector
from alphamind.analysis.qualitative_research.loaders import (
    ActiveThesis,
    CalendarEvent,
    PredictionMarketSnapshot,
    QualitativeInputs,
    SentimentAggregate,
    load_active_thesis_summaries,
    load_calendar_events_72h,
    load_prediction_market_snapshot,
    load_qualitative_inputs,
    load_sentiment_aggregates,
)
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    DistillationContractHistory,
    DistillationTickerBaseline,
    EarningsEventDetails,
    EventCalendar,
    NewsArticles,
    NewsArticleTickers,
    OhlcvBars,
    PredictionMarketContracts,
    PredictionMarketSnapshots,
    SectorClassification,
)
from alphamind.persistence.session import make_engine, make_session_factory

# Importing the state.tables package registers ThesisRow / ThesisComponentRow /
# PositionRow on ``Base.metadata`` so ``create_all`` sees them.
from alphamind.state.tables import PositionRow, ThesisRow

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

AS_OF = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
_ISO = AS_OF.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture()
def engine() -> Iterator[Engine]:
    eng = make_engine(":memory:")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    sf = make_session_factory(engine)
    with sf() as sess:
        yield sess


def _add_ticker(session: Session, ticker: str, sector: str = "tech") -> None:
    asset_id = f"asset-{ticker.lower()}"
    session.add(
        AssetUniverse(
            asset_id=asset_id,
            ticker=ticker,
            full_name=f"{ticker} Corp",
            asset_class="equity",
            asset_role="universe",
            exchange="NASDAQ",
            is_active=1,
            added_date="2026-01-01",
            last_updated="2026-01-01T00:00:00Z",
        )
    )
    session.add(
        SectorClassification(
            ticker=ticker,
            asset_id=asset_id,
            alphamind_sector=sector,
            domain_researcher="tech_semis",
            sector_etf="XLK",
            classification_source="test",
            last_updated="2026-01-01T00:00:00Z",
        )
    )


def _add_sentiment_baseline(
    session: Session,
    ticker: str,
    *,
    mean: float,
    stdev: float,
    n_observations: int,
    as_of_str: str,
    calibration_state: str = "calibrated",
) -> None:
    session.add(
        DistillationTickerBaseline(
            ticker=ticker,
            baseline_kind="sentiment",
            as_of=as_of_str,
            mean=mean,
            stdev=stdev,
            n_observations=n_observations,
            window_days=90,
            calibration_state=calibration_state,
            ingested_at=as_of_str,
        )
    )


def _add_contract(
    session: Session,
    contract_id: str,
    *,
    platform: str = "Polymarket",
    description: str = "FOMC rate hold",
    category: str = "macro",
    resolution_date: str | None = "2026-06-01",
) -> None:
    session.add(
        PredictionMarketContracts(
            contract_id=contract_id,
            platform=platform,
            description=description,
            category=category,
            resolution_date=resolution_date,
            resolution_outcome=None,
            created_at="2026-01-01T00:00:00Z",
            last_seen_at=_ISO,
        )
    )
    session.flush()


def _add_snapshot(
    session: Session,
    contract_id: str,
    snapshot_ts: str,
    *,
    yes_probability: float,
    volume_24h_usd: float | None = 50_000.0,
    liquidity_usd: float | None = 100_000.0,
) -> None:
    session.add(
        PredictionMarketSnapshots(
            contract_id=contract_id,
            snapshot_ts=snapshot_ts,
            yes_probability=yes_probability,
            volume_24h_usd=volume_24h_usd,
            liquidity_usd=liquidity_usd,
            bid=None,
            ask=None,
            ingested_at=snapshot_ts,
        )
    )


def _add_contract_history(
    session: Session,
    contract_id: str,
    snapshot_ts: str,
    *,
    yes_probability: float,
    delta_pp_since_prior: float,
    liquidity_usd: float = 100_000.0,
    calibration_state: str = "calibrated",
) -> None:
    session.add(
        DistillationContractHistory(
            contract_id=contract_id,
            snapshot_ts=snapshot_ts,
            yes_probability=yes_probability,
            delta_pp_since_prior=delta_pp_since_prior,
            liquidity_usd=liquidity_usd,
            calibration_state=calibration_state,
            ingested_at=snapshot_ts,
        )
    )


def _add_daily_bar(
    session: Session,
    ticker: str,
    period_start_dt: datetime,
    *,
    adj_close: float,
) -> None:
    """Seed a single daily ``OhlcvBars`` row with ``timeframe='1d'``."""
    period_start = period_start_dt.astimezone(UTC).strftime("%Y-%m-%d")
    period_end = period_start_dt.astimezone(UTC).strftime("%Y-%m-%d")
    session.add(
        OhlcvBars(
            ticker=ticker,
            timeframe="1d",
            period_start=period_start,
            period_end=period_end,
            session="regular",
            adj_open=adj_close,
            adj_high=adj_close,
            adj_low=adj_close,
            adj_close=adj_close,
            adj_volume=1_000_000,
            adj_vwap=None,
            unadj_open=adj_close,
            unadj_high=adj_close,
            unadj_low=adj_close,
            unadj_close=adj_close,
            unadj_volume=1_000_000,
            unadj_vwap=None,
            trade_count=None,
            source="test",
            ingested_at=_ISO,
        )
    )


def _add_news_article(
    session: Session,
    *,
    article_id: str,
    ticker: str,
    published_at: str,
) -> None:
    """Add a NewsArticles + matching NewsArticleTickers row, flushing in
    FK-safe order."""
    session.add(
        NewsArticles(
            article_id=article_id,
            source="test",
            source_outlet=None,
            source_credibility_tier=None,
            url=None,
            language="en",
            headline_text="headline",
            body_path=None,
            published_at=published_at,
            ingested_at=published_at,
            vendor_sentiment_score=None,
            vendor_sentiment_label=None,
            topic_tags=None,
            cross_ticker_cluster_id=None,
        )
    )
    session.flush()
    session.add(
        NewsArticleTickers(
            article_id=article_id,
            ticker=ticker,
            is_primary=1,
            vendor_sentiment_score=None,
            vendor_sentiment_label=None,
        )
    )


def _add_thesis_and_position(
    session: Session,
    *,
    thesis_id: str = "TH-001",
    position_id: str = "POS-001",
    ticker: str = "NVDA",
    summary: str = "Bull thesis on AI capex",
    key_catalyst: str = "Q1 earnings beat",
    time_expectation_hours: float = 48.0,
    thesis_status: str = "ACTIVE",
    position_status: str = "OPEN",
    generation_timestamp: str = _ISO,
) -> None:
    """Seed a position + thesis pair atomically.

    The position carries an equity ``details_json`` blob so the loader's
    ``resolve_ticker`` path returns ``ticker``. The thesis stores ``summary``
    on the row and ``key_catalyst`` inside ``narrative_json`` per
    ``theses_codec.py``.

    Both rows go in a single transaction; the ``positions↔theses`` FK pair is
    declared DEFERRABLE INITIALLY DEFERRED so the cycle resolves at COMMIT.
    """
    _add_ticker(session, ticker)
    details_json = json.dumps(
        {
            "instrument_type": "EQUITY",
            "ticker": ticker,
            "share_count": 100.0,
            "average_cost_basis_per_share": 100.0,
        }
    )
    session.add(
        PositionRow(
            position_id=position_id,
            thesis_id=thesis_id,
            bracket_id=None,
            status=position_status,
            direction="LONG",
            entry_timestamp=None,
            instrument_type="EQUITY",
            details_json=details_json,
            execution_history_json="[]",
            realized_pnl_to_date_usd=None,
            corporate_action_adjustment_needed=0,
            parent_position_id=None,
            origin=None,
        )
    )
    session.add(
        ThesisRow(
            thesis_id=thesis_id,
            position_id=position_id,
            status=thesis_status,
            resolution_timestamp=None,
            resolution_category=None,
            summary=summary,
            time_expectation_hours=time_expectation_hours,
            position_size_rationale=None,
            generation_timestamp=generation_timestamp,
            narrative_json=json.dumps({"key_catalyst": key_catalyst}),
        )
    )


def _add_event(
    session: Session,
    event_id: str,
    *,
    event_type: str = "fomc",
    description: str = "FOMC Meeting",
    scheduled_at: datetime,
    sectors: str | None = None,
    ticker: str | None = None,
) -> None:
    session.add(
        EventCalendar(
            event_id=event_id,
            event_type=event_type,
            ticker=ticker,
            scheduled_at=scheduled_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            description=description,
            status="scheduled",
            source="test",
            ingested_at=_ISO,
            last_updated=_ISO,
            sectors=sectors,
        )
    )
    session.flush()


# ---------------------------------------------------------------------------
# 1. Import smoke test (tracer bullet)
# ---------------------------------------------------------------------------


class TestImports:
    def test_all_public_names_importable(self) -> None:
        """All names listed in the AC are importable from loaders."""
        # If the import at module top didn't fail, this trivially passes.
        assert callable(load_sentiment_aggregates)
        assert callable(load_prediction_market_snapshot)
        assert callable(load_calendar_events_72h)
        assert callable(load_active_thesis_summaries)
        assert callable(load_qualitative_inputs)
        assert issubclass(QualitativeInputs, object)
        assert issubclass(SentimentAggregate, object)
        assert issubclass(PredictionMarketSnapshot, object)
        assert issubclass(CalendarEvent, object)
        assert issubclass(ActiveThesis, object)


# ---------------------------------------------------------------------------
# 2. load_active_thesis_summaries
# ---------------------------------------------------------------------------


class TestLoadActiveThesisSummaries:
    def test_empty_db_returns_empty(self, session: Session) -> None:
        result = load_active_thesis_summaries(session, as_of=AS_OF)
        assert result == ()

    def test_active_thesis_returned_with_ticker_from_position(self, session: Session) -> None:
        _add_thesis_and_position(
            session,
            thesis_id="TH-001",
            position_id="POS-001",
            ticker="NVDA",
            summary="Bull thesis on AI capex",
            key_catalyst="Q1 earnings beat",
            time_expectation_hours=48.0,
        )
        session.commit()

        result = load_active_thesis_summaries(session, as_of=AS_OF)
        assert len(result) == 1
        thesis = result[0]
        assert thesis.thesis_id == "TH-001"
        assert thesis.ticker == "NVDA"
        assert thesis.summary == "Bull thesis on AI capex"
        assert thesis.key_catalyst == "Q1 earnings beat"
        assert thesis.time_expectation_hours == 48

    def test_resolved_and_cancelled_theses_excluded(self, session: Session) -> None:
        _add_thesis_and_position(
            session,
            thesis_id="TH-active",
            position_id="POS-1",
            ticker="NVDA",
            thesis_status="ACTIVE",
        )
        _add_thesis_and_position(
            session,
            thesis_id="TH-resolved",
            position_id="POS-2",
            ticker="AAPL",
            thesis_status="RESOLVED",
        )
        _add_thesis_and_position(
            session,
            thesis_id="TH-cancelled",
            position_id="POS-3",
            ticker="TSLA",
            thesis_status="CANCELLED",
        )
        session.commit()

        result = load_active_thesis_summaries(session, as_of=AS_OF)
        thesis_ids = [t.thesis_id for t in result]
        assert thesis_ids == ["TH-active"]

    def test_closed_positions_excluded(self, session: Session) -> None:
        # An ACTIVE thesis whose backing position has been CLOSED should not
        # surface; the qualitative researcher only takes context from the
        # live book.
        _add_thesis_and_position(
            session,
            thesis_id="TH-open",
            position_id="POS-open",
            ticker="NVDA",
            position_status="OPEN",
        )
        _add_thesis_and_position(
            session,
            thesis_id="TH-pending",
            position_id="POS-pending",
            ticker="AAPL",
            position_status="PENDING",
        )
        _add_thesis_and_position(
            session,
            thesis_id="TH-closed",
            position_id="POS-closed",
            ticker="TSLA",
            position_status="CLOSED",
        )
        session.commit()

        result = load_active_thesis_summaries(session, as_of=AS_OF)
        thesis_ids = {t.thesis_id for t in result}
        assert thesis_ids == {"TH-open", "TH-pending"}

    def test_results_sorted_by_thesis_id(self, session: Session) -> None:
        _add_thesis_and_position(session, thesis_id="TH-ccc", position_id="POS-c", ticker="NVDA")
        _add_thesis_and_position(session, thesis_id="TH-aaa", position_id="POS-a", ticker="AAPL")
        _add_thesis_and_position(session, thesis_id="TH-bbb", position_id="POS-b", ticker="TSLA")
        session.commit()

        result = load_active_thesis_summaries(session, as_of=AS_OF)
        thesis_ids = [t.thesis_id for t in result]
        assert thesis_ids == ["TH-aaa", "TH-bbb", "TH-ccc"]

    def test_theses_generated_after_as_of_excluded(self, session: Session) -> None:
        # A thesis born in the future relative to as_of must not leak into
        # the renderer.
        future_iso = (AS_OF + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _add_thesis_and_position(
            session,
            thesis_id="TH-future",
            position_id="POS-future",
            ticker="NVDA",
            generation_timestamp=future_iso,
        )
        session.commit()

        result = load_active_thesis_summaries(session, as_of=AS_OF)
        assert result == ()

    def test_options_position_returns_underlying_ticker(self, session: Session) -> None:
        _add_ticker(session, "NVDA")
        options_details = json.dumps(
            {
                "instrument_type": "OPTIONS",
                "underlying_ticker": "NVDA",
                "strike_price": 500.0,
                "expiration_date": "2026-06-19",
                "contract_type": "CALL",
                "contract_count": 1.0,
                "contract_multiplier": 100.0,
                "premium_paid_per_contract": 12.50,
                "greeks": {
                    "delta": 0.55,
                    "gamma": 0.01,
                    "theta": -0.05,
                    "vega": 0.10,
                    "as_of_timestamp": None,
                    "iv_used": None,
                    "refresh_failed": False,
                },
            }
        )
        session.add(
            PositionRow(
                position_id="POS-opt",
                thesis_id="TH-opt",
                bracket_id=None,
                status="OPEN",
                direction="LONG",
                entry_timestamp=None,
                instrument_type="OPTIONS",
                details_json=options_details,
                execution_history_json="[]",
                realized_pnl_to_date_usd=None,
                corporate_action_adjustment_needed=0,
                parent_position_id=None,
                origin=None,
            )
        )
        session.add(
            ThesisRow(
                thesis_id="TH-opt",
                position_id="POS-opt",
                status="ACTIVE",
                resolution_timestamp=None,
                resolution_category=None,
                summary="Upside call",
                time_expectation_hours=24.0,
                position_size_rationale=None,
                generation_timestamp=_ISO,
                narrative_json=json.dumps({"key_catalyst": "Earnings"}),
            )
        )
        session.commit()

        result = load_active_thesis_summaries(session, as_of=AS_OF)
        assert len(result) == 1
        assert result[0].ticker == "NVDA"


# ---------------------------------------------------------------------------
# 3. load_calendar_events_72h
# ---------------------------------------------------------------------------


class TestLoadCalendarEvents72h:
    def test_empty_db_returns_empty(self, session: Session) -> None:
        result = load_calendar_events_72h(session, as_of=AS_OF)
        assert result == ()

    def test_event_within_72h_included(self, session: Session) -> None:
        event_time = AS_OF + timedelta(hours=24)
        _add_event(session, "evt-1", scheduled_at=event_time, description="FOMC Meeting")
        session.commit()

        result = load_calendar_events_72h(session, as_of=AS_OF)
        assert len(result) == 1
        assert result[0].event_id == "evt-1"

    def test_event_exactly_at_boundary_excluded(self, session: Session) -> None:
        # The spec says [as_of, as_of + 72h) — event at exactly +73h is out.
        event_time = AS_OF + timedelta(hours=73)
        _add_event(session, "evt-73h", scheduled_at=event_time)
        session.commit()

        result = load_calendar_events_72h(session, as_of=AS_OF)
        assert result == ()

    def test_events_sorted_ascending_by_event_time(self, session: Session) -> None:
        later = AS_OF + timedelta(hours=48)
        earlier = AS_OF + timedelta(hours=12)
        _add_event(session, "evt-later", scheduled_at=later, description="Later event")
        _add_event(session, "evt-earlier", scheduled_at=earlier, description="Earlier event")
        session.commit()

        result = load_calendar_events_72h(session, as_of=AS_OF)
        assert len(result) == 2
        assert result[0].event_time <= result[1].event_time

    def test_earnings_event_includes_consensus(self, session: Session) -> None:
        _add_ticker(session, "NVDA")
        event_time = AS_OF + timedelta(hours=24)
        _add_event(
            session,
            "evt-earnings",
            event_type="earnings",
            description="NVDA earnings",
            scheduled_at=event_time,
            ticker=Symbol("NVDA"),
        )
        session.add(
            EarningsEventDetails(
                event_id="evt-earnings",
                ticker=Symbol("NVDA"),
                fiscal_period="Q1",
                fiscal_year=2026,
                expected_call_time=None,
                eps_consensus=1.50,
                eps_actual=None,
                revenue_consensus_usd=40_000_000_000.0,
                revenue_actual_usd=None,
                reported_at=None,
                source="test",
            )
        )
        session.commit()

        result = load_calendar_events_72h(session, as_of=AS_OF)
        assert len(result) == 1
        entry = result[0]
        assert entry.consensus is not None
        assert "1.50" in entry.consensus

    def test_calendar_event_fields(self, session: Session) -> None:
        event_time = AS_OF + timedelta(hours=12)
        _add_event(
            session,
            "evt-fomc",
            event_type="fomc",
            description="FOMC Decision",
            scheduled_at=event_time,
        )
        session.commit()

        result = load_calendar_events_72h(session, as_of=AS_OF)
        assert len(result) == 1
        e = result[0]
        assert e.event_id == "evt-fomc"
        assert e.event_name == "FOMC Decision"
        assert e.event_type == "fomc"
        assert isinstance(e.tickers, tuple)
        assert isinstance(e.sectors, frozenset)
        assert e.consensus is None

    def test_sectors_parsed_correctly(self, session: Session) -> None:
        event_time = AS_OF + timedelta(hours=12)
        _add_event(
            session,
            "evt-sector",
            event_type="earnings",
            description="Earnings call",
            scheduled_at=event_time,
            sectors="tech_semis,financials",
        )
        session.commit()

        result = load_calendar_events_72h(session, as_of=AS_OF)
        assert len(result) == 1
        e = result[0]
        assert Sector.TECH_SEMIS in e.sectors
        assert Sector.FINANCIALS in e.sectors


# ---------------------------------------------------------------------------
# 4. load_sentiment_aggregates
# ---------------------------------------------------------------------------


class TestLoadSentimentAggregates:
    def test_empty_db_returns_empty(self, session: Session) -> None:
        result = load_sentiment_aggregates(session, as_of=AS_OF)
        assert result == ()

    def test_calibrated_ticker_returns_record(self, session: Session) -> None:
        _add_ticker(session, "NVDA")
        _add_sentiment_baseline(
            session,
            "NVDA",
            mean=0.1,
            stdev=0.3,
            n_observations=100,
            as_of_str=_ISO,
        )
        session.commit()

        result = load_sentiment_aggregates(session, as_of=AS_OF)
        assert len(result) == 1
        agg = result[0]
        assert agg.ticker == "NVDA"
        assert 0.0 <= agg.percentile_vs_self <= 1.0

    def test_percentile_vs_self_in_unit_range(self, session: Session) -> None:
        _add_ticker(session, "AAPL")
        _add_sentiment_baseline(
            session,
            "AAPL",
            mean=0.2,
            stdev=0.4,
            n_observations=50,
            as_of_str=_ISO,
        )
        session.commit()

        result = load_sentiment_aggregates(session, as_of=AS_OF)
        assert len(result) == 1
        assert 0.0 <= result[0].percentile_vs_self <= 1.0

    def test_ticker_scope_filters_results(self, session: Session) -> None:
        _add_ticker(session, "NVDA")
        _add_ticker(session, "AAPL")
        _add_sentiment_baseline(
            session, "NVDA", mean=0.1, stdev=0.3, n_observations=100, as_of_str=_ISO
        )
        _add_sentiment_baseline(
            session, "AAPL", mean=0.2, stdev=0.4, n_observations=80, as_of_str=_ISO
        )
        session.commit()

        result = load_sentiment_aggregates(session, as_of=AS_OF, ticker_scope=["NVDA"])
        tickers = [r.ticker for r in result]
        assert "NVDA" in tickers
        assert "AAPL" not in tickers

    def test_none_scope_returns_all_tickers(self, session: Session) -> None:
        _add_ticker(session, "NVDA")
        _add_ticker(session, "AAPL")
        _add_sentiment_baseline(
            session, "NVDA", mean=0.1, stdev=0.3, n_observations=100, as_of_str=_ISO
        )
        _add_sentiment_baseline(
            session, "AAPL", mean=0.2, stdev=0.4, n_observations=80, as_of_str=_ISO
        )
        session.commit()

        result = load_sentiment_aggregates(session, as_of=AS_OF, ticker_scope=None)
        tickers = {r.ticker for r in result}
        assert "NVDA" in tickers
        assert "AAPL" in tickers

    def test_bootstrap_ticker_returns_record_from_pool(self, session: Session) -> None:
        """Below-threshold ticker still returns a record using universe-pooled fallback."""
        # Add a calibrated ticker so the pool is non-empty.
        _add_ticker(session, "NVDA")
        _add_sentiment_baseline(
            session,
            "NVDA",
            mean=0.0,
            stdev=0.5,
            n_observations=200,
            as_of_str=_ISO,
            calibration_state="calibrated",
        )
        # Add a bootstrap ticker.
        _add_ticker(session, "THIN", sector="tech")
        _add_sentiment_baseline(
            session,
            "THIN",
            mean=0.3,
            stdev=0.2,
            n_observations=5,  # Very few observations
            as_of_str=_ISO,
            calibration_state="bootstrap",
        )
        session.commit()

        result = load_sentiment_aggregates(session, as_of=AS_OF, ticker_scope=["THIN"])
        assert len(result) == 1
        assert 0.0 <= result[0].percentile_vs_self <= 1.0

    def test_record_fields_present(self, session: Session) -> None:
        _add_ticker(session, "JPM", sector="financials")
        _add_sentiment_baseline(
            session, "JPM", mean=0.05, stdev=0.2, n_observations=120, as_of_str=_ISO
        )
        session.commit()

        result = load_sentiment_aggregates(session, as_of=AS_OF)
        assert len(result) == 1
        agg = result[0]
        assert agg.ticker == "JPM"
        assert isinstance(agg.directional_score, float)
        assert isinstance(agg.magnitude, float)
        assert agg.rate_of_change is None or isinstance(agg.rate_of_change, float)
        assert agg.volume is None or isinstance(agg.volume, int)
        assert agg.divergence_flag is None or isinstance(agg.divergence_flag, bool)
        assert 0.0 <= agg.percentile_vs_self <= 1.0
        assert isinstance(agg.data_freshness, datetime)

    def test_rate_of_change_is_latest_minus_prior_mean(self, session: Session) -> None:
        """``rate_of_change`` = latest sentiment-baseline mean minus the prior row's mean."""
        _add_ticker(session, "NVDA")
        prior_iso = (AS_OF - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _add_sentiment_baseline(
            session, "NVDA", mean=0.1, stdev=0.3, n_observations=100, as_of_str=prior_iso
        )
        _add_sentiment_baseline(
            session, "NVDA", mean=0.5, stdev=0.3, n_observations=100, as_of_str=_ISO
        )
        session.commit()

        result = load_sentiment_aggregates(session, as_of=AS_OF)
        assert len(result) == 1
        assert result[0].rate_of_change == pytest.approx(0.4)

    def test_rate_of_change_none_when_single_baseline_row(self, session: Session) -> None:
        """No prior baseline → rate_of_change=None (degrades gracefully)."""
        _add_ticker(session, "NVDA")
        _add_sentiment_baseline(
            session, "NVDA", mean=0.1, stdev=0.3, n_observations=100, as_of_str=_ISO
        )
        session.commit()

        result = load_sentiment_aggregates(session, as_of=AS_OF)
        assert len(result) == 1
        assert result[0].rate_of_change is None

    def test_volume_counts_article_tickers_in_window(self, session: Session) -> None:
        """``volume`` = count of ``news_article_tickers`` rows whose article
        ``published_at`` falls in the rate-of-change window
        ``(prior_baseline.as_of, latest_baseline.as_of]``.
        """
        _add_ticker(session, "NVDA")
        prior_iso = (AS_OF - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _add_sentiment_baseline(
            session, "NVDA", mean=0.1, stdev=0.3, n_observations=100, as_of_str=prior_iso
        )
        _add_sentiment_baseline(
            session, "NVDA", mean=0.5, stdev=0.3, n_observations=100, as_of_str=_ISO
        )

        # Three in-window articles, one too old, one too new.
        in_window_a = (AS_OF - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        in_window_b = (AS_OF - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        in_window_c = (AS_OF - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        too_old = (AS_OF - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
        too_new = (AS_OF + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for article_id, ts in (
            ("art-in-1", in_window_a),
            ("art-in-2", in_window_b),
            ("art-in-3", in_window_c),
            ("art-old", too_old),
            ("art-new", too_new),
        ):
            _add_news_article(session, article_id=article_id, ticker="NVDA", published_at=ts)
        session.commit()

        result = load_sentiment_aggregates(session, as_of=AS_OF)
        assert len(result) == 1
        assert result[0].volume == 3

    def test_volume_none_when_no_prior_baseline(self, session: Session) -> None:
        """Without a prior baseline there's no window — volume falls back to None."""
        _add_ticker(session, "NVDA")
        _add_sentiment_baseline(
            session, "NVDA", mean=0.1, stdev=0.3, n_observations=100, as_of_str=_ISO
        )
        session.commit()

        result = load_sentiment_aggregates(session, as_of=AS_OF)
        assert len(result) == 1
        assert result[0].volume is None

    def test_divergence_flag_true_when_sentiment_positive_price_negative(
        self, session: Session
    ) -> None:
        """Positive sentiment + negative price return → ``divergence_flag=True``."""
        _add_ticker(session, "NVDA")
        _add_sentiment_baseline(
            session, "NVDA", mean=0.4, stdev=0.3, n_observations=100, as_of_str=_ISO
        )
        # Latest price 95, prior price 100 → -5% return.
        _add_daily_bar(session, "NVDA", AS_OF, adj_close=95.0)
        _add_daily_bar(session, "NVDA", AS_OF - timedelta(days=5), adj_close=100.0)
        session.commit()

        result = load_sentiment_aggregates(session, as_of=AS_OF)
        assert len(result) == 1
        assert result[0].divergence_flag is True

    def test_divergence_flag_false_when_directions_agree(self, session: Session) -> None:
        """Positive sentiment + positive price return → ``divergence_flag=False``."""
        _add_ticker(session, "NVDA")
        _add_sentiment_baseline(
            session, "NVDA", mean=0.4, stdev=0.3, n_observations=100, as_of_str=_ISO
        )
        _add_daily_bar(session, "NVDA", AS_OF, adj_close=105.0)
        _add_daily_bar(session, "NVDA", AS_OF - timedelta(days=5), adj_close=100.0)
        session.commit()

        result = load_sentiment_aggregates(session, as_of=AS_OF)
        assert len(result) == 1
        assert result[0].divergence_flag is False

    def test_divergence_flag_false_when_signal_below_threshold(self, session: Session) -> None:
        """Sentiment magnitude under the threshold suppresses the flag."""
        _add_ticker(session, "NVDA")
        # Tiny positive sentiment that should not trigger the flag.
        _add_sentiment_baseline(
            session, "NVDA", mean=0.001, stdev=0.3, n_observations=100, as_of_str=_ISO
        )
        _add_daily_bar(session, "NVDA", AS_OF, adj_close=95.0)
        _add_daily_bar(session, "NVDA", AS_OF - timedelta(days=5), adj_close=100.0)
        session.commit()

        result = load_sentiment_aggregates(session, as_of=AS_OF)
        assert len(result) == 1
        assert result[0].divergence_flag is False

    def test_divergence_flag_none_when_no_price_history(self, session: Session) -> None:
        """No price bars → divergence_flag falls back to None."""
        _add_ticker(session, "NVDA")
        _add_sentiment_baseline(
            session, "NVDA", mean=0.4, stdev=0.3, n_observations=100, as_of_str=_ISO
        )
        session.commit()

        result = load_sentiment_aggregates(session, as_of=AS_OF)
        assert len(result) == 1
        assert result[0].divergence_flag is None


# ---------------------------------------------------------------------------
# 5. load_prediction_market_snapshot
# ---------------------------------------------------------------------------


class TestLoadPredictionMarketSnapshot:
    def test_empty_db_returns_empty(self, session: Session) -> None:
        result = load_prediction_market_snapshot(session, as_of=AS_OF)
        assert result == ()

    def test_single_snapshot_delta_is_zero(self, session: Session) -> None:
        _add_contract(session, "c1")
        ts = (AS_OF - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _add_snapshot(session, "c1", ts, yes_probability=0.65)
        _add_contract_history(session, "c1", ts, yes_probability=0.65, delta_pp_since_prior=0.0)
        session.commit()

        result = load_prediction_market_snapshot(session, as_of=AS_OF)
        assert len(result) == 1
        snap = result[0]
        assert snap.contract_id == "c1"
        assert snap.delta_since_last_invocation_pp == 0.0

    def test_two_snapshots_delta_correct(self, session: Session) -> None:
        _add_contract(session, "c2")
        ts1 = (AS_OF - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ts2 = (AS_OF - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _add_snapshot(session, "c2", ts1, yes_probability=0.60)
        _add_snapshot(session, "c2", ts2, yes_probability=0.70)
        _add_contract_history(session, "c2", ts1, yes_probability=0.60, delta_pp_since_prior=0.0)
        _add_contract_history(session, "c2", ts2, yes_probability=0.70, delta_pp_since_prior=10.0)
        session.commit()

        result = load_prediction_market_snapshot(session, as_of=AS_OF)
        assert len(result) == 1
        snap = result[0]
        assert snap.delta_since_last_invocation_pp == pytest.approx(10.0)

    def test_snapshot_fields_present(self, session: Session) -> None:
        _add_contract(
            session, "c3", platform="Kalshi", description="Fed rate hold", category="macro"
        )
        ts = (AS_OF - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _add_snapshot(session, "c3", ts, yes_probability=0.72, volume_24h_usd=80_000.0)
        _add_contract_history(session, "c3", ts, yes_probability=0.72, delta_pp_since_prior=2.0)
        session.commit()

        result = load_prediction_market_snapshot(session, as_of=AS_OF)
        assert len(result) == 1
        snap = result[0]
        assert snap.contract_id == "c3"
        assert snap.description == "Fed rate hold"
        assert snap.platform == "Kalshi"
        assert snap.category == "macro"
        assert snap.current_probability == pytest.approx(0.72)
        assert isinstance(snap.delta_since_prior_pp, float)
        assert isinstance(snap.volume_24h_usd, (float, type(None)))
        assert isinstance(snap.is_low_liquidity, bool)
        assert isinstance(snap.meets_threshold_flag, bool)
        assert isinstance(snap.data_freshness, datetime)

    def test_meets_threshold_flag_set_for_large_delta(self, session: Session) -> None:
        _add_contract(session, "c4")
        ts = (AS_OF - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _add_snapshot(session, "c4", ts, yes_probability=0.80, volume_24h_usd=200_000.0)
        # Large delta → should trigger threshold flag.
        _add_contract_history(session, "c4", ts, yes_probability=0.80, delta_pp_since_prior=25.0)
        session.commit()

        result = load_prediction_market_snapshot(session, as_of=AS_OF)
        assert len(result) == 1
        # A 25pp delta should be above any sane threshold.
        assert result[0].meets_threshold_flag is True

    def test_delta_since_prior_pp_uses_second_latest_history_row(self, session: Session) -> None:
        """delta_since_prior_pp = current_probability - probability of the second-latest
        history row at or before as_of, regardless of the gap between rows.

        The "since prior" naming reflects the irregular spacing of the underlying
        snapshot history — the second-latest row could be hours, days, or longer
        in the past.
        """
        _add_contract(session, "c-prior")
        # Three rows spaced irregularly: 5 days ago, 1 hour ago, and AS_OF.
        ts1 = (AS_OF - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ts2 = (AS_OF - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _add_snapshot(session, "c-prior", ts1, yes_probability=0.40)
        _add_snapshot(session, "c-prior", ts2, yes_probability=0.55)
        _add_contract_history(
            session, "c-prior", ts1, yes_probability=0.40, delta_pp_since_prior=0.0
        )
        _add_contract_history(
            session, "c-prior", ts2, yes_probability=0.55, delta_pp_since_prior=15.0
        )
        session.commit()

        result = load_prediction_market_snapshot(session, as_of=AS_OF)
        assert len(result) == 1
        snap = result[0]
        # current_probability - probability of second-latest history row (0.40)
        # = 0.55 - 0.40 = 0.15 (delta is in probability units, not pp).
        assert snap.delta_since_prior_pp == pytest.approx(0.15)

    def test_history_lookup_is_batched_not_n_plus_one(
        self, engine: Engine, session: Session
    ) -> None:
        """Latest-two history rows are loaded in a single batched query, not per-contract.

        Regression guard against the N+1 pattern: with 5 contracts at 3
        history rows each, the loader should issue fewer than 5 history-table
        queries total — empirically a single window-function query.
        """
        from sqlalchemy import event as sa_event

        ts_old = (AS_OF - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ts_mid = (AS_OF - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ts_new = (AS_OF - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for i in range(5):
            cid = f"c-batch-{i}"
            _add_contract(session, cid)
            _add_snapshot(session, cid, ts_new, yes_probability=0.70)
            _add_contract_history(
                session, cid, ts_old, yes_probability=0.50, delta_pp_since_prior=0.0
            )
            _add_contract_history(
                session, cid, ts_mid, yes_probability=0.60, delta_pp_since_prior=10.0
            )
            _add_contract_history(
                session, cid, ts_new, yes_probability=0.70, delta_pp_since_prior=10.0
            )
        session.commit()

        history_query_count = 0

        @sa_event.listens_for(engine, "after_cursor_execute")
        def _count_history_queries(
            conn: object,
            cursor: object,
            statement: str,
            parameters: object,
            context: object,
            executemany: bool,
        ) -> None:
            nonlocal history_query_count
            if "distillation_contract_history" in statement.lower():
                history_query_count += 1

        try:
            result = load_prediction_market_snapshot(session, as_of=AS_OF)
        finally:
            sa_event.remove(engine, "after_cursor_execute", _count_history_queries)

        assert len(result) == 5
        # Per-row deltas should match the seeded latest-vs-prior gap (0.70 - 0.60).
        for snap in result:
            assert snap.delta_since_prior_pp == pytest.approx(0.10)
        # N+1 elimination: the documented design is a single window-function
        # query batching the latest-two history rows for all contracts.
        assert history_query_count <= 1, (
            f"expected single batched history query, got {history_query_count}"
        )


# ---------------------------------------------------------------------------
# 6. load_qualitative_inputs — aggregator and freshness
# ---------------------------------------------------------------------------


class TestLoadQualitativeInputs:
    def test_empty_db_returns_container(self, session: Session) -> None:
        result = load_qualitative_inputs(session, as_of=AS_OF)
        assert isinstance(result, QualitativeInputs)
        assert result.sentiment_aggregates == ()
        assert result.prediction_markets == ()
        assert result.events == ()
        assert result.theses == ()

    def test_data_freshness_is_minimum_of_sub_loaders(self, session: Session) -> None:
        """data_freshness = min(sub-loader freshness timestamps)."""
        # Add a sentiment baseline with an older freshness.
        older_ts = (AS_OF - timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _add_ticker(session, "NVDA")
        _add_sentiment_baseline(
            session, "NVDA", mean=0.0, stdev=0.3, n_observations=100, as_of_str=older_ts
        )
        # Add an event with a newer freshness.
        event_time = AS_OF + timedelta(hours=24)
        _add_event(session, "evt-1", scheduled_at=event_time, description="FOMC")
        session.commit()

        result = load_qualitative_inputs(session, as_of=AS_OF)
        # Freshness should be the minimum (most stale) of the sub-loaders.
        assert isinstance(result.data_freshness, datetime)

    def test_theses_always_empty(self, session: Session) -> None:
        result = load_qualitative_inputs(session, as_of=AS_OF)
        assert result.theses == ()


# ---------------------------------------------------------------------------
# 7. Immutability
# ---------------------------------------------------------------------------


class TestImmutability:
    def test_sentiment_aggregate_is_frozen(self, session: Session) -> None:
        _add_ticker(session, "NVDA")
        _add_sentiment_baseline(
            session, "NVDA", mean=0.1, stdev=0.3, n_observations=100, as_of_str=_ISO
        )
        session.commit()

        result = load_sentiment_aggregates(session, as_of=AS_OF)
        assert len(result) == 1
        with pytest.raises((TypeError, AttributeError, ValidationError)):
            result[0].ticker = "MUTATED"  # type: ignore[misc]

    def test_calendar_event_is_frozen(self, session: Session) -> None:
        event_time = AS_OF + timedelta(hours=12)
        _add_event(session, "evt-freeze", scheduled_at=event_time)
        session.commit()

        result = load_calendar_events_72h(session, as_of=AS_OF)
        assert len(result) == 1
        with pytest.raises((TypeError, AttributeError, ValidationError)):
            result[0].event_name = "MUTATED"  # type: ignore[misc]

    def test_prediction_market_snapshot_is_frozen(self, session: Session) -> None:
        _add_contract(session, "c-freeze")
        ts = (AS_OF - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _add_snapshot(session, "c-freeze", ts, yes_probability=0.5)
        _add_contract_history(
            session, "c-freeze", ts, yes_probability=0.5, delta_pp_since_prior=0.0
        )
        session.commit()

        result = load_prediction_market_snapshot(session, as_of=AS_OF)
        assert len(result) == 1
        with pytest.raises((TypeError, AttributeError, ValidationError)):
            result[0].contract_id = "MUTATED"  # type: ignore[misc]

    def test_active_thesis_is_frozen(self) -> None:
        thesis = ActiveThesis(
            thesis_id=ThesisId("t1"),
            ticker=Symbol("NVDA"),
            summary="Bull thesis",
            key_catalyst="Earnings beat",
            time_expectation_hours=72,
        )
        with pytest.raises((TypeError, AttributeError, ValidationError)):
            thesis.ticker = "MUTATED"  # type: ignore[misc]

    def test_qualitative_inputs_is_frozen(self, session: Session) -> None:
        result = load_qualitative_inputs(session, as_of=AS_OF)
        with pytest.raises((TypeError, AttributeError, ValidationError)):
            result.theses = ()  # type: ignore[misc]
