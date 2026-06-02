"""Tests for ``q7_cross_asset.compute_cross_sector_rotation`` — story 08d.

DB-backed smoke: confirm the loader/shim wires the DB entry point to the
pure compute core.  The velocity and narrative classification logic is
fully pinned by ``test_compute.py::TestCrossSectorRotationCompute``.

Reduced in ALP-797 (q7 pure/DB double-altitude reduction).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from alphamind._kernel.ids import Symbol
from alphamind.distillation.output import OutputAudience, OutputBlock
from alphamind.distillation.q7 import compute_cross_sector_rotation
from alphamind.persistence.models import (
    AssetUniverse,
    OhlcvBars,
)


def _add_ticker(session: Session, ticker: str) -> None:
    session.add(
        AssetUniverse(
            asset_id=f"asset-{ticker.lower()}",
            ticker=ticker,
            full_name=f"{ticker}",
            asset_class="equity",
            asset_role="universe",
            exchange="NYSE",
            is_active=1,
            added_date="2020-01-01",
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


def _seed_etf_path(
    session: Session,
    *,
    ticker: str,
    closes: list[float],
    start_day: datetime,
) -> None:
    _add_ticker(session, ticker)
    for i, close in enumerate(closes):
        ts = (start_day + timedelta(days=i)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        _add_close(session, ticker=ticker, period_start=ts, close=close)


# ---------------------------------------------------------------------------
# Loader/persistence smoke
# ---------------------------------------------------------------------------


class TestCrossSectorRotationSmoke:
    def test_db_entry_point_returns_block_with_audience_and_velocity(
        self, session: Session
    ) -> None:
        """DB shim loads ETF price rows and wires into the pure compute core."""
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        start_day = as_of - timedelta(days=20)
        flat = [100.0] * 21
        for ticker in ("XLK", "SMH", "XLF", "XLE", "IWM", "SPY"):
            _seed_etf_path(session, ticker=Symbol(ticker), closes=flat, start_day=start_day)
        session.commit()

        blocks = compute_cross_sector_rotation(
            session,
            as_of=as_of,
            short_window_days=5,
            long_window_days=20,
        )

        assert len(blocks) == 1
        block = blocks[0]
        assert isinstance(block, OutputBlock)
        assert block.block_id == "q7.cross_sector_rotation"
        assert OutputAudience.CORRELATION_REGIME_BRIEF in block.audience
        assert "velocity_label" in block.payload
        assert "narrative_label" in block.payload
