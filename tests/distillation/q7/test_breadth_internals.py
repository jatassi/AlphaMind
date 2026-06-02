"""Tests for ``q7_cross_asset.compute_breadth_internals`` — story 08d.

DB-backed smoke: confirm the loader/shim wires the DB entry point to the
pure compute core.  The compute logic itself is fully pinned by
``test_compute.py::TestBreadthInternalsCompute``.

Reduced in ALP-797 (q7 pure/DB double-altitude reduction).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from alphamind._kernel.ids import Symbol
from alphamind.distillation.output import OutputAudience, OutputBlock
from alphamind.distillation.q7 import compute_breadth_internals
from alphamind.persistence.models import (
    AssetUniverse,
    OhlcvBars,
    SectorClassification,
)


def _add_ticker(session: Session, ticker: str, *, sector: str = "tech") -> None:
    asset_id = f"asset-{ticker.lower()}"
    session.add(
        AssetUniverse(
            asset_id=asset_id,
            ticker=ticker,
            full_name=f"{ticker}",
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
            asset_id=asset_id,
            alphamind_sector=sector,
            domain_researcher="tech_semis",
            sector_etf="XLK",
            classification_source="test",
            last_updated="2026-04-26T00:00:00Z",
        )
    )


def _add_close(session: Session, *, ticker: str, period_start: str, close: float) -> None:
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
            adj_volume=1_000_000,
            adj_vwap=close,
            unadj_open=close,
            unadj_high=close,
            unadj_low=close,
            unadj_close=close,
            unadj_volume=1_000_000,
            unadj_vwap=close,
            trade_count=None,
            source="test",
            ingested_at="2026-04-26T00:00:00Z",
        )
    )


def _seed_path(
    session: Session,
    *,
    ticker: str,
    closes: list[float],
    start_day: datetime,
) -> None:
    for i, close in enumerate(closes):
        ts = (start_day + timedelta(days=i)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        _add_close(session, ticker=ticker, period_start=ts, close=close)


# ---------------------------------------------------------------------------
# Loader/persistence smoke
# ---------------------------------------------------------------------------


class TestBreadthInternalsSmoke:
    def test_db_entry_point_returns_block_with_expected_payload_keys(
        self, session: Session
    ) -> None:
        """DB shim loads OHLCV rows and wires into the pure compute core."""
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        start_day = as_of - timedelta(days=20)
        rising = [100.0 * (1.0 + 0.01 * i) for i in range(21)]

        _add_ticker(session, "AAPL", sector="tech")
        _add_ticker(session, "MSFT", sector="tech")
        _add_ticker(session, "SPY", sector="tech")
        _seed_path(session, ticker=Symbol("AAPL"), closes=rising, start_day=start_day)
        _seed_path(session, ticker=Symbol("MSFT"), closes=rising, start_day=start_day)
        _seed_path(session, ticker=Symbol("SPY"), closes=rising, start_day=start_day)
        session.commit()

        blocks = compute_breadth_internals(
            session,
            universe_tickers=("AAPL", "MSFT"),
            sector_members={"tech": ("AAPL", "MSFT")},
            as_of=as_of,
        )

        assert len(blocks) == 1
        block = blocks[0]
        assert isinstance(block, OutputBlock)
        assert block.block_id == "q7.breadth_internals"
        assert OutputAudience.CORRELATION_REGIME_BRIEF in block.audience
        assert OutputAudience.UNIVERSAL_BROADCAST in block.audience
        for key in ("pct_above_20d_ema", "advance_decline_per_sector", "equal_vs_cap_weight"):
            assert key in block.payload
