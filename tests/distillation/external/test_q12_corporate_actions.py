"""Tests for ``q12_corporate_actions`` — story 02-distillation-layer/08e.

Covers the four detection paths defined in
``docs/implementation/02-distillation-layer/08e-q12-corporate-actions-signals.md``
§ Scope:

- ``unusual_event_cadence`` — flag when the prior matching event is older
  than the documented two-year lookback.
- ``event_clustering`` — flag when ``CLUSTERING_MIN_EVENTS`` events of the
  documented event-type subset land inside the forward 30-day window for a
  single sector.
- ``etf_vs_single_name_divergence`` — flag when both legs (ETF weakness and
  single-name BTO) co-occur on the same sector ETF and its top-10
  constituents.
- ``recent_corporate_actions`` — pass-through block listing every
  ``corporate_actions`` row whose ``ex_date`` falls within the documented
  ±5-trading-day window.

Each test composes the smallest possible fixture that exercises one
detection path so the boundary conditions in the acceptance criteria are
visibly exercised.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind._kernel.ids import Symbol
from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.output import OutputAudience, OutputBlock
from alphamind.distillation.q12_corporate_actions import (
    CLUSTERING_MIN_EVENTS,
    CLUSTERING_WINDOW_DAYS,
    DIVERGENCE_BTO_MIN_CONSTITUENTS,
    DIVERGENCE_FLOW_SIGMA_THRESHOLD,
    ETF_FLOW_WINDOW_DAYS,
    RECENT_ACTIONS_WINDOW_TRADING_DAYS,
    UNUSUAL_EVENT_CADENCE_LOOKBACK_DAYS,
    detect_q12_signals,
)
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    CorporateActions,
    EtfMembership,
    EventCalendar,
    OhlcvBars,
    SectorClassification,
)
from alphamind.persistence.session import make_engine, make_session_factory


@pytest.fixture()
def engine() -> Iterator[Engine]:
    eng = make_engine(":memory:")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    factory = make_session_factory(engine)
    with factory() as sess:
        yield sess


# ---------------------------------------------------------------------------
# Fixture helpers — kept minimal; each test composes only what it needs.
# ---------------------------------------------------------------------------


def _add_ticker(
    session: Session,
    ticker: str,
    *,
    sector: str = "tech",
    sector_etf: str = "XLK",
) -> None:
    session.add(
        AssetUniverse(
            asset_id=f"asset-{ticker.lower()}",
            ticker=ticker,
            full_name=ticker,
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
            domain_researcher="tech_semis" if sector in ("tech", "semis") else sector,
            sector_etf=sector_etf,
            classification_source="manual",
            last_updated="2026-04-26T00:00:00Z",
        )
    )


def _add_event(
    session: Session,
    *,
    event_id: str,
    ticker: str | None,
    event_type: str,
    scheduled_at: str,
    status: str = "scheduled",
) -> None:
    session.add(
        EventCalendar(
            event_id=event_id,
            event_type=event_type,
            ticker=ticker,
            scheduled_at=scheduled_at,
            description=None,
            status=status,
            source="test",
            ingested_at=scheduled_at,
            last_updated=scheduled_at,
        )
    )


def _add_corporate_action(
    session: Session,
    *,
    action_id: str,
    ticker: str,
    action_type: str,
    ex_date: str,
    cash_amount_per_share: float | None = None,
) -> None:
    session.add(
        CorporateActions(
            action_id=action_id,
            ticker=ticker,
            action_type=action_type,
            ex_date=ex_date,
            cash_amount_per_share=cash_amount_per_share,
            source="test",
            ingested_at=ex_date,
        )
    )


def _add_etf_membership(
    session: Session,
    *,
    ticker: str,
    etf_ticker: str,
    weight_pct: float,
    is_top_10: int,
    weight_as_of: str = "2026-04-20",
) -> None:
    session.add(
        EtfMembership(
            ticker=ticker,
            etf_ticker=etf_ticker,
            weight_as_of=weight_as_of,
            etf_name=etf_ticker,
            weight_pct=weight_pct,
            is_top_10=is_top_10,
        )
    )


def _add_bar(
    session: Session,
    *,
    ticker: str,
    period_start: str,
    close: float,
    volume: int,
) -> None:
    session.add(
        OhlcvBars(
            ticker=ticker,
            timeframe="1d",
            period_start=period_start,
            period_end=period_start,
            session="regular",
            adj_open=close,
            adj_high=close,
            adj_low=close,
            adj_close=close,
            adj_volume=volume,
            adj_vwap=close,
            unadj_open=close,
            unadj_high=close,
            unadj_low=close,
            unadj_close=close,
            unadj_volume=volume,
            unadj_vwap=close,
            trade_count=None,
            source="test",
            ingested_at=period_start,
        )
    )


def _block(blocks: list[OutputBlock], block_id: str) -> OutputBlock:
    matches = [b for b in blocks if b.block_id == block_id]
    assert matches, f"no block found with id {block_id!r}; got {[b.block_id for b in blocks]}"
    assert len(matches) == 1, f"expected exactly one {block_id} block, got {len(matches)}"
    return matches[0]


# Tests inject the baseline window directly — equivalent to the
# ``persistence_windows.volume_baseline_days`` Class A value the
# orchestrator hands the function in production.
TEST_VOLUME_BASELINE_DAYS = 20


# ---------------------------------------------------------------------------
# Constants — every magnitude in the spec lives in the source module so the
# tests can refer to it by name. Local re-exports here are also self-checks
# that the import surface is stable.
# ---------------------------------------------------------------------------


def test_named_constants_match_design_doc_thresholds() -> None:
    """The four definitional constants encode the values stated in 08e § Notes."""
    # 730 days ≈ 2 years, per 08e § Notes "UNUSUAL_EVENT_CADENCE_LOOKBACK_DAYS = 730".
    assert UNUSUAL_EVENT_CADENCE_LOOKBACK_DAYS == 730
    # 30-day forward window for sector clustering.
    assert CLUSTERING_WINDOW_DAYS == 30
    # ≥ 3 events triggers the cluster.
    assert CLUSTERING_MIN_EVENTS == 3
    # ±5 trading days for recent corporate actions pass-through.
    assert RECENT_ACTIONS_WINDOW_TRADING_DAYS == 5
    # ETF flow proxy reads the trailing-5-day weight/volume window.
    assert ETF_FLOW_WINDOW_DAYS == 5
    # Both flow legs use the 1-sigma z-score threshold from 08e § Scope.
    assert DIVERGENCE_FLOW_SIGMA_THRESHOLD == 1.0
    # Single-name divergence requires ≥ 2 of the top-10 constituents.
    assert DIVERGENCE_BTO_MIN_CONSTITUENTS == 2


# ---------------------------------------------------------------------------
# Unusual event cadence
# ---------------------------------------------------------------------------


class TestUnusualEventCadence:
    def test_fires_when_prior_event_more_than_two_years_ago(self, session: Session) -> None:
        """A scheduled investor day fires the flag when the prior one was > 2y ago."""
        _add_ticker(session, "AAPL", sector="tech")
        # Prior investor day completed slightly more than two years before as_of.
        _add_event(
            session,
            event_id="evt-old",
            ticker=Symbol("AAPL"),
            event_type="investor_day",
            scheduled_at="2024-04-01T00:00:00Z",
            status="completed",
        )
        # Newly scheduled investor day inside the next 30-day window.
        _add_event(
            session,
            event_id="evt-new",
            ticker=Symbol("AAPL"),
            event_type="investor_day",
            scheduled_at="2026-05-10T00:00:00Z",
            status="scheduled",
        )
        session.commit()

        blocks = detect_q12_signals(
            session,
            as_of="2026-04-27T00:00:00Z",
            volume_baseline_days=TEST_VOLUME_BASELINE_DAYS,
        )

        novelty = _block(blocks, "q12.event_novelty")
        assert novelty.calibration_state is CalibrationState.CALIBRATED
        per_ticker = novelty.payload["per_ticker"]
        assert "AAPL" in per_ticker
        flags = per_ticker["AAPL"]["flags"]
        assert any(f["flag"] == "unusual_event_cadence" for f in flags)
        # Audience is the affected ticker's sector audience.
        assert OutputAudience.SECTOR_TECH_SEMIS in novelty.audience

    def test_suppressed_when_prior_event_within_two_years(self, session: Session) -> None:
        """If a matching event was held inside the lookback, the flag stays off."""
        _add_ticker(session, "AAPL", sector="tech")
        _add_event(
            session,
            event_id="evt-recent",
            ticker=Symbol("AAPL"),
            event_type="investor_day",
            scheduled_at="2025-06-01T00:00:00Z",
            status="completed",
        )
        _add_event(
            session,
            event_id="evt-new",
            ticker=Symbol("AAPL"),
            event_type="investor_day",
            scheduled_at="2026-05-10T00:00:00Z",
            status="scheduled",
        )
        session.commit()

        blocks = detect_q12_signals(
            session,
            as_of="2026-04-27T00:00:00Z",
            volume_baseline_days=TEST_VOLUME_BASELINE_DAYS,
        )

        novelty_blocks = [b for b in blocks if b.block_id == "q12.event_novelty"]
        # Either no block at all, or no AAPL unusual-cadence flag if a block
        # was emitted for clustering. Inspect both possibilities.
        for block in novelty_blocks:
            for ticker, ticker_payload in block.payload["per_ticker"].items():
                for flag in ticker_payload["flags"]:
                    assert not (ticker == "AAPL" and flag["flag"] == "unusual_event_cadence"), (
                        "unusual_event_cadence must not fire when a "
                        "matching event was within 2 years"
                    )


# ---------------------------------------------------------------------------
# Event clustering (sector-level density of forward-window events)
# ---------------------------------------------------------------------------


class TestEventClustering:
    def test_fires_at_three_events_in_window(self, session: Session) -> None:
        """Three events of the documented type-set inside the forward window cluster."""
        for ticker in ("AAPL", "MSFT", "GOOG"):
            _add_ticker(session, ticker, sector="tech")
        _add_event(
            session,
            event_id="evt-1",
            ticker=Symbol("AAPL"),
            event_type="investor_day",
            scheduled_at="2026-05-01T00:00:00Z",
        )
        _add_event(
            session,
            event_id="evt-2",
            ticker=Symbol("MSFT"),
            event_type="conference",
            scheduled_at="2026-05-10T00:00:00Z",
        )
        _add_event(
            session,
            event_id="evt-3",
            ticker=Symbol("GOOG"),
            event_type="product_launch",
            scheduled_at="2026-05-20T00:00:00Z",
        )
        session.commit()

        blocks = detect_q12_signals(
            session,
            as_of="2026-04-27T00:00:00Z",
            volume_baseline_days=TEST_VOLUME_BASELINE_DAYS,
        )

        novelty = _block(blocks, "q12.event_novelty")
        per_ticker = novelty.payload["per_ticker"]
        # Every clustered ticker carries the same cluster summary so the
        # payload follows the per-ticker convention pinned across 08a-f.
        clustered_tickers = {
            ticker
            for ticker, entry in per_ticker.items()
            for flag in entry["flags"]
            if flag["flag"] == "event_clustering"
        }
        assert clustered_tickers == {"AAPL", "MSFT", "GOOG"}
        cluster_records = [
            flag
            for entry in per_ticker.values()
            for flag in entry["flags"]
            if flag["flag"] == "event_clustering"
        ]
        # Every per-ticker record carries the same n_events count.
        assert all(record["n_events"] == 3 for record in cluster_records)
        assert OutputAudience.SECTOR_TECH_SEMIS in novelty.audience

    def test_suppressed_at_two_events(self, session: Session) -> None:
        """Two events do not meet the ``CLUSTERING_MIN_EVENTS`` threshold."""
        for ticker in ("AAPL", "MSFT"):
            _add_ticker(session, ticker, sector="tech")
        _add_event(
            session,
            event_id="evt-1",
            ticker=Symbol("AAPL"),
            event_type="investor_day",
            scheduled_at="2026-05-01T00:00:00Z",
        )
        _add_event(
            session,
            event_id="evt-2",
            ticker=Symbol("MSFT"),
            event_type="conference",
            scheduled_at="2026-05-10T00:00:00Z",
        )
        session.commit()

        blocks = detect_q12_signals(
            session,
            as_of="2026-04-27T00:00:00Z",
            volume_baseline_days=TEST_VOLUME_BASELINE_DAYS,
        )

        cluster_records: list[dict[str, Any]] = []
        for block in blocks:
            if block.block_id != "q12.event_novelty":
                continue
            for entry in block.payload["per_ticker"].values():
                cluster_records.extend(f for f in entry["flags"] if f["flag"] == "event_clustering")
        assert cluster_records == []


# ---------------------------------------------------------------------------
# ETF flow vs. single-name flow divergence
# ---------------------------------------------------------------------------


def _add_etf_volume_history(
    session: Session,
    *,
    etf_ticker: str,
    as_of_dt: datetime,
    baseline_volume: int,
    recent_volume: int,
    volume_baseline_days: int,
) -> None:
    """Populate ``ohlcv_bars`` for an ETF with a clean weakness signal.

    Days ``[-volume_baseline_days, -ETF_FLOW_WINDOW_DAYS)`` carry
    ``baseline_volume`` (the historical mean); the most recent
    ``ETF_FLOW_WINDOW_DAYS`` carry ``recent_volume`` so the recent z is
    deterministic.
    """
    import alphamind.distillation.q12_corporate_actions as q12

    _add_ticker(session, etf_ticker, sector="tech")  # benign sector_classification row
    for offset in range(volume_baseline_days, q12.ETF_FLOW_WINDOW_DAYS, -1):
        day = as_of_dt - timedelta(days=offset)
        _add_bar(
            session,
            ticker=etf_ticker,
            period_start=day.strftime("%Y-%m-%dT%H:%M:%SZ"),
            close=100.0 + (offset * 0.01),  # tiny variance for non-zero stdev
            volume=baseline_volume + (offset % 3),
        )
    for offset in range(q12.ETF_FLOW_WINDOW_DAYS, 0, -1):
        day = as_of_dt - timedelta(days=offset - 1)
        # Recent window: closes drift down (price weakness leg).
        close_now = 100.0 - (q12.ETF_FLOW_WINDOW_DAYS - offset)
        _add_bar(
            session,
            ticker=etf_ticker,
            period_start=day.strftime("%Y-%m-%dT%H:%M:%SZ"),
            close=close_now,
            volume=recent_volume,
        )


def _add_options_history(
    session: Session,
    *,
    ticker: str,
    as_of_dt: datetime,
    baseline_volume: int,
    recent_volume: int,
    volume_baseline_days: int,
) -> None:
    """Populate ``options_contract_snapshots`` to drive a BTO z-score.

    Generates one snapshot per day inside the baseline window with
    ``baseline_volume`` in the historical portion and ``recent_volume``
    in the trailing-``ETF_FLOW_WINDOW_DAYS`` portion.
    """
    import alphamind.distillation.q12_corporate_actions as q12
    from alphamind.persistence.models import OptionsContracts, OptionsContractSnapshots

    contract_ticker = f"O:{ticker}TEST"
    if (
        session.execute(
            select(OptionsContracts).where(OptionsContracts.contract_ticker == contract_ticker)
        ).scalar_one_or_none()
        is None
    ):
        session.add(
            OptionsContracts(
                contract_ticker=contract_ticker,
                underlying_ticker=ticker,
                expiration_date="2026-12-31",
                strike_price=100.0,
                contract_type="call",
                first_seen_at="2026-01-01T00:00:00Z",
                last_seen_at="2026-04-26T00:00:00Z",
                source="test",
            )
        )
        session.flush()

    for offset in range(volume_baseline_days, q12.ETF_FLOW_WINDOW_DAYS, -1):
        day = as_of_dt - timedelta(days=offset)
        session.add(
            OptionsContractSnapshots(
                snapshot_ts=day.strftime("%Y-%m-%dT%H:%M:%SZ"),
                contract_ticker=contract_ticker,
                underlying_ticker=ticker,
                volume_today=baseline_volume + (offset % 3),
                source="test",
                ingested_at=day.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        )
    for offset in range(q12.ETF_FLOW_WINDOW_DAYS, 0, -1):
        day = as_of_dt - timedelta(days=offset - 1)
        session.add(
            OptionsContractSnapshots(
                snapshot_ts=day.strftime("%Y-%m-%dT%H:%M:%SZ"),
                contract_ticker=contract_ticker,
                underlying_ticker=ticker,
                volume_today=recent_volume,
                source="test",
                ingested_at=day.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        )


class TestEtfVsSingleNameDivergence:
    def test_fires_when_both_legs_present(self, session: Session) -> None:
        """ETF volume below 1-sigma baseline + price weakness + ≥ 2 BTO members above 1-sigma."""
        as_of_dt = datetime(2026, 4, 26, tzinfo=UTC)

        # ETF leg: weak volume + downward price drift over recent window.
        _add_etf_volume_history(
            session,
            etf_ticker="XLK",
            as_of_dt=as_of_dt,
            baseline_volume=10_000_000,
            recent_volume=1_000_000,  # severe drop → far below -1-sigma
            volume_baseline_days=TEST_VOLUME_BASELINE_DAYS,
        )

        # Two top-10 constituents with elevated BTO call flow.
        for member in ("AAPL", "MSFT"):
            _add_ticker(session, member, sector="tech")
            _add_etf_membership(
                session,
                ticker=member,
                etf_ticker="XLK",
                weight_pct=10.0,
                is_top_10=1,
            )
            _add_options_history(
                session,
                ticker=member,
                as_of_dt=as_of_dt,
                baseline_volume=1_000,
                recent_volume=100_000,  # >> 1-sigma above baseline
                volume_baseline_days=TEST_VOLUME_BASELINE_DAYS,
            )

        session.commit()

        blocks = detect_q12_signals(
            session,
            as_of=as_of_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            volume_baseline_days=TEST_VOLUME_BASELINE_DAYS,
        )

        divergence = _block(blocks, "q12.etf_vs_single_name_divergence")
        assert divergence.payload["etf_ticker"] == "XLK"
        assert divergence.payload["attribution_method"] == "weight_volume_proxy"
        assert OutputAudience.SECTOR_TECH_SEMIS in divergence.audience
        firing = divergence.payload["per_ticker"]
        assert set(firing) == {"AAPL", "MSFT"}

    def test_suppressed_when_only_etf_leg_fires(self, session: Session) -> None:
        """Without single-name BTO surge, the divergence does not fire."""
        as_of_dt = datetime(2026, 4, 26, tzinfo=UTC)
        _add_etf_volume_history(
            session,
            etf_ticker="XLK",
            as_of_dt=as_of_dt,
            baseline_volume=10_000_000,
            recent_volume=1_000_000,
            volume_baseline_days=TEST_VOLUME_BASELINE_DAYS,
        )

        # Members exist but their BTO flow is flat.
        for member in ("AAPL", "MSFT"):
            _add_ticker(session, member, sector="tech")
            _add_etf_membership(
                session,
                ticker=member,
                etf_ticker="XLK",
                weight_pct=10.0,
                is_top_10=1,
            )
            _add_options_history(
                session,
                ticker=member,
                as_of_dt=as_of_dt,
                baseline_volume=1_000,
                recent_volume=1_000,  # no surge → z ≈ zero
                volume_baseline_days=TEST_VOLUME_BASELINE_DAYS,
            )

        session.commit()

        blocks = detect_q12_signals(
            session,
            as_of=as_of_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            volume_baseline_days=TEST_VOLUME_BASELINE_DAYS,
        )
        assert not [b for b in blocks if b.block_id == "q12.etf_vs_single_name_divergence"]

    def test_suppressed_when_only_single_name_leg_fires(self, session: Session) -> None:
        """ETF volume normal — divergence does not fire even with BTO surge."""
        as_of_dt = datetime(2026, 4, 26, tzinfo=UTC)
        _add_etf_volume_history(
            session,
            etf_ticker="XLK",
            as_of_dt=as_of_dt,
            baseline_volume=10_000_000,
            recent_volume=10_500_000,  # roughly flat, not below -1-sigma
            volume_baseline_days=TEST_VOLUME_BASELINE_DAYS,
        )

        for member in ("AAPL", "MSFT"):
            _add_ticker(session, member, sector="tech")
            _add_etf_membership(
                session,
                ticker=member,
                etf_ticker="XLK",
                weight_pct=10.0,
                is_top_10=1,
            )
            _add_options_history(
                session,
                ticker=member,
                as_of_dt=as_of_dt,
                baseline_volume=1_000,
                recent_volume=100_000,
                volume_baseline_days=TEST_VOLUME_BASELINE_DAYS,
            )

        session.commit()

        blocks = detect_q12_signals(
            session,
            as_of=as_of_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            volume_baseline_days=TEST_VOLUME_BASELINE_DAYS,
        )
        assert not [b for b in blocks if b.block_id == "q12.etf_vs_single_name_divergence"]


# ---------------------------------------------------------------------------
# Recent corporate actions pass-through
# ---------------------------------------------------------------------------


class TestRecentCorporateActions:
    def test_populates_actions_inside_window(self, session: Session) -> None:
        """``ex_date`` within ±5 trading days lands in the per-ticker payload."""
        _add_ticker(session, "AAPL", sector="tech")
        _add_corporate_action(
            session,
            action_id="ca-1",
            ticker=Symbol("AAPL"),
            action_type="cash_dividend",
            ex_date="2026-04-25",
            cash_amount_per_share=0.24,
        )
        # Outside the window — must be excluded.
        _add_corporate_action(
            session,
            action_id="ca-old",
            ticker=Symbol("AAPL"),
            action_type="cash_dividend",
            ex_date="2025-04-01",
            cash_amount_per_share=0.24,
        )
        session.commit()

        blocks = detect_q12_signals(
            session,
            as_of="2026-04-27T00:00:00Z",
            volume_baseline_days=TEST_VOLUME_BASELINE_DAYS,
        )

        recent = _block(blocks, "q12.recent_corporate_actions")
        assert OutputAudience.SECTOR_TECH_SEMIS in recent.audience
        per_ticker = recent.payload["per_ticker"]
        assert "AAPL" in per_ticker
        action_ids = {a["action_id"] for a in per_ticker["AAPL"]["actions"]}
        assert action_ids == {"ca-1"}

    def test_emits_no_block_when_no_actions_in_window(self, session: Session) -> None:
        """An empty universe window suppresses the recent-actions block."""
        _add_ticker(session, "AAPL", sector="tech")
        _add_corporate_action(
            session,
            action_id="ca-old",
            ticker=Symbol("AAPL"),
            action_type="cash_dividend",
            ex_date="2024-01-01",
            cash_amount_per_share=0.24,
        )
        session.commit()

        blocks = detect_q12_signals(
            session,
            as_of="2026-04-27T00:00:00Z",
            volume_baseline_days=TEST_VOLUME_BASELINE_DAYS,
        )
        assert not [b for b in blocks if b.block_id == "q12.recent_corporate_actions"]
