"""Tests for ``q7_cross_asset.compute_intermarket_regime`` — story 08d.

Cover the four intermarket relationships from quant 7d:

- Stocks vs. bonds (SPY/TLT) correlation regime: loader/smoke.
- Gold vs. real yields (GLD vs. DFII10): divergence flag firing.
- Oil vs. energy stock beta (WTI vs. XLE): beta-drift flag.
- VIX vs. SPY: divergence flag.
- Date-alignment tests (ALP-629): unique DB-path coverage — fully preserved.

The regime-label computation itself is pinned by
``test_compute.py::TestIntermarketRegimeCompute``.

Reduced in ALP-797 (q7 pure/DB double-altitude reduction).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest
from sqlalchemy.orm import Session

from alphamind._kernel.ids import Symbol
from alphamind.distillation.output import OutputAudience
from alphamind.distillation.q7 import compute_intermarket_regime
from alphamind.distillation.q7._helpers import (
    _log_returns_from_closes,
    _pearson_correlation,
)
from alphamind.persistence.models import (
    AssetUniverse,
    MacroObservations,
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


def _seed_path(
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


def _seed_macro(
    session: Session,
    *,
    series_id: str,
    values: list[float],
    start_day: datetime,
    source: str = "fred",
) -> None:
    for i, v in enumerate(values):
        observation_date = (start_day + timedelta(days=i)).strftime("%Y-%m-%d")
        session.add(
            MacroObservations(
                source=source,
                series_id=series_id,
                observation_date=observation_date,
                revision_number=0,
                release_date=observation_date,
                value=v,
                units="pct",
                frequency="d",
                ingested_at="2026-04-26T00:00:00Z",
            )
        )


def _seed_macro_at_dates(
    session: Session,
    *,
    series_id: str,
    values_by_offset: dict[int, float],
    start_day: datetime,
    source: str = "fred",
) -> None:
    """Seed macro values only on the given day offsets from ``start_day``."""
    for offset, v in values_by_offset.items():
        observation_date = (start_day + timedelta(days=offset)).strftime("%Y-%m-%d")
        session.add(
            MacroObservations(
                source=source,
                series_id=series_id,
                observation_date=observation_date,
                revision_number=0,
                release_date=observation_date,
                value=v,
                units="pct",
                frequency="d",
                ingested_at="2026-04-26T00:00:00Z",
            )
        )


# ---------------------------------------------------------------------------
# Stocks vs. bonds regime (SPY/TLT) — loader/persistence smoke
# ---------------------------------------------------------------------------


class TestIntermarketStocksVsBonds:
    def test_negative_correlation_labeled_growth_environment(self, session: Session) -> None:
        # 60-day window. SPY and TLT move opposite each other on every day →
        # correlation negative → growth environment.
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        start_day = as_of - timedelta(days=60)
        returns = [(0.01 if i % 2 else -0.01) for i in range(60)]
        spy_closes = [400.0]
        tlt_closes = [100.0]
        for r in returns:
            spy_closes.append(spy_closes[-1] * (1.0 + r))
            tlt_closes.append(tlt_closes[-1] * (1.0 - r))  # opposite direction
        _seed_path(session, ticker=Symbol("SPY"), closes=spy_closes, start_day=start_day)
        _seed_path(session, ticker=Symbol("TLT"), closes=tlt_closes, start_day=start_day)
        # Add stub series for the other intermarket relationships so the
        # block builds.
        _seed_path(session, ticker=Symbol("GLD"), closes=[180.0] * 61, start_day=start_day)
        _seed_path(session, ticker=Symbol("XLE"), closes=[80.0] * 61, start_day=start_day)
        _seed_macro(
            session,
            series_id="DFII10",
            values=[1.5] * 61,
            start_day=start_day,
        )
        _seed_macro(
            session,
            series_id="DCOILWTICO",
            values=[70.0] * 61,
            start_day=start_day,
        )
        _seed_macro(
            session,
            series_id="VIXCLS",
            values=[15.0] * 61,
            start_day=start_day,
        )
        session.commit()

        blocks = compute_intermarket_regime(
            session,
            as_of=as_of,
            window_days=60,
            short_window_days=20,
        )

        spy_tlt_blocks = [b for b in blocks if b.block_id.endswith("spy_tlt")]
        assert len(spy_tlt_blocks) == 1
        block = spy_tlt_blocks[0]
        assert block.payload["regime_label"] == "growth_environment"
        assert OutputAudience.CORRELATION_REGIME_BRIEF in block.audience
        assert OutputAudience.UNIVERSAL_BROADCAST in block.audience


# ---------------------------------------------------------------------------
# Gold vs. real yields divergence (GLD vs. DFII10)
# ---------------------------------------------------------------------------


class TestIntermarketGoldVsRealYields:
    def test_gold_real_yields_divergence_flag_fires_when_correlation_breaks(
        self, session: Session
    ) -> None:
        # Gold and real-yield divergence: in a textbook regime, GLD and real
        # yields move opposite (negative correlation). When they diverge from
        # that pattern (e.g., both rising), the flag fires.
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        start_day = as_of - timedelta(days=60)
        # Build noisy paths where GLD and DFII10 move together day-by-day.
        # Both rising together → positive correlation → divergence from
        # the textbook negative-correlation regime.
        deltas = [(0.01, 0.02), (-0.005, -0.01), (0.008, 0.015), (-0.003, -0.006)] * 16
        gld_closes = [180.0]
        dfii10_values = [1.5]
        for gld_delta, dfii_delta in deltas[:60]:
            gld_closes.append(gld_closes[-1] * (1.0 + gld_delta))
            dfii10_values.append(dfii10_values[-1] + dfii_delta)
        _seed_path(session, ticker=Symbol("GLD"), closes=gld_closes, start_day=start_day)
        _seed_path(session, ticker=Symbol("SPY"), closes=[400.0] * 61, start_day=start_day)
        _seed_path(session, ticker=Symbol("TLT"), closes=[100.0] * 61, start_day=start_day)
        _seed_path(session, ticker=Symbol("XLE"), closes=[80.0] * 61, start_day=start_day)
        _seed_macro(
            session,
            series_id="DFII10",
            values=dfii10_values,
            start_day=start_day,
        )
        _seed_macro(
            session,
            series_id="DCOILWTICO",
            values=[70.0] * 61,
            start_day=start_day,
        )
        _seed_macro(
            session,
            series_id="VIXCLS",
            values=[15.0] * 61,
            start_day=start_day,
        )
        session.commit()

        blocks = compute_intermarket_regime(
            session,
            as_of=as_of,
            window_days=60,
            short_window_days=20,
        )

        gld_blocks = [b for b in blocks if b.block_id.endswith("gld_real_yields")]
        block = gld_blocks[0]
        # Both rising → positive correlation → divergence from the
        # textbook negative regime → flag.
        names = [flag.name for flag in block.anomaly_flags]
        assert any("gold_real_yields_divergence" in name for name in names), (
            f"expected gold/real-yields divergence flag, got {names}"
        )


# ---------------------------------------------------------------------------
# Oil vs. energy stock beta stability monitoring
# ---------------------------------------------------------------------------


class TestIntermarketOilVsXLEBeta:
    def test_oil_xle_beta_stability_flag_fires_when_beta_drifts(self, session: Session) -> None:
        # Construct a 60-day window where the oil/XLE beta is roughly 1 across
        # the long window, then over the most recent 20 sessions XLE doubles
        # its sensitivity → beta moves >0.5.
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        start_day = as_of - timedelta(days=60)
        # 40 days of beta=1: equal returns.
        early_oil = [(0.01 if i % 2 else -0.01) for i in range(40)]
        early_xle = list(early_oil)
        # 20 days of beta=2: XLE returns twice oil returns.
        late_oil = [(0.01 if i % 2 else -0.01) for i in range(20)]
        late_xle = [r * 2.0 for r in late_oil]
        oil_returns = early_oil + late_oil
        xle_returns = early_xle + late_xle
        oil_closes = [70.0]
        xle_closes = [80.0]
        for r in oil_returns:
            oil_closes.append(oil_closes[-1] * (1.0 + r))
        for r in xle_returns:
            xle_closes.append(xle_closes[-1] * (1.0 + r))
        _seed_macro(
            session,
            series_id="DCOILWTICO",
            values=oil_closes,
            start_day=start_day,
        )
        _seed_path(session, ticker=Symbol("XLE"), closes=xle_closes, start_day=start_day)
        _seed_path(session, ticker=Symbol("SPY"), closes=[400.0] * 61, start_day=start_day)
        _seed_path(session, ticker=Symbol("TLT"), closes=[100.0] * 61, start_day=start_day)
        _seed_path(session, ticker=Symbol("GLD"), closes=[180.0] * 61, start_day=start_day)
        _seed_macro(session, series_id="DFII10", values=[1.5] * 61, start_day=start_day)
        _seed_macro(session, series_id="VIXCLS", values=[15.0] * 61, start_day=start_day)
        session.commit()

        blocks = compute_intermarket_regime(
            session,
            as_of=as_of,
            window_days=60,
            short_window_days=20,
        )

        oil_blocks = [b for b in blocks if b.block_id.endswith("oil_xle_beta")]
        block = oil_blocks[0]
        names = [flag.name for flag in block.anomaly_flags]
        assert any("oil_xle_beta_drift" in name for name in names), (
            f"expected oil/XLE beta-drift flag, got {names}"
        )


# ---------------------------------------------------------------------------
# VIX vs. SPY divergence
# ---------------------------------------------------------------------------


class TestIntermarketVixVsSpy:
    def test_vix_rising_on_flat_market_fires_divergence_flag(self, session: Session) -> None:
        # VIX up day-over-day, SPY up or flat day-over-day → divergence.
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        start_day = as_of - timedelta(days=60)
        # Construct noisy paths where SPY and VIX move together — the
        # textbook regime is negative correlation, so both rising on the
        # same days produces a positive realized correlation → divergence.
        deltas = [(0.005, 0.5), (-0.003, -0.2), (0.004, 0.3), (-0.002, -0.1)] * 16
        spy_closes = [400.0]
        vix_values = [12.0]
        for spy_delta, vix_delta in deltas[:60]:
            spy_closes.append(spy_closes[-1] * (1.0 + spy_delta))
            vix_values.append(vix_values[-1] + vix_delta)
        _seed_path(session, ticker=Symbol("SPY"), closes=spy_closes, start_day=start_day)
        _seed_macro(
            session,
            series_id="VIXCLS",
            values=vix_values,
            start_day=start_day,
        )
        _seed_path(session, ticker=Symbol("TLT"), closes=[100.0] * 61, start_day=start_day)
        _seed_path(session, ticker=Symbol("GLD"), closes=[180.0] * 61, start_day=start_day)
        _seed_path(session, ticker=Symbol("XLE"), closes=[80.0] * 61, start_day=start_day)
        _seed_macro(session, series_id="DFII10", values=[1.5] * 61, start_day=start_day)
        _seed_macro(
            session,
            series_id="DCOILWTICO",
            values=[70.0] * 61,
            start_day=start_day,
        )
        session.commit()

        blocks = compute_intermarket_regime(
            session,
            as_of=as_of,
            window_days=60,
            short_window_days=20,
        )

        vix_blocks = [b for b in blocks if b.block_id.endswith("vix_spy")]
        block = vix_blocks[0]
        names = [flag.name for flag in block.anomaly_flags]
        assert any("vix_spy_divergence" in name for name in names), (
            f"expected VIX/SPY divergence flag, got {names}"
        )


# ---------------------------------------------------------------------------
# Date-aligned correlation (ALP-629) — mismatched OHLCV vs FRED calendars
# ---------------------------------------------------------------------------


class TestIntermarketDateAlignment:
    """Date-join intersection drives correlation when calendars mismatch."""

    def test_vix_spy_correlation_uses_date_joined_intersection(self, session: Session) -> None:
        # SPY publishes daily bars across the full 60-day window (NYSE
        # calendar). VIX is published with a 1-day lag (last NYSE day
        # missing) plus three interior gaps inside the window. The fix
        # date-joins the two series on common calendar dates before
        # computing returns/deltas; the previous index-aligned slice paired
        # SPY[-n:] with VIX[-n:] across a date-shuffled sample.
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        window_days = 60
        start_day = as_of - timedelta(days=window_days)

        # Deterministic anti-correlated construction: SPY's per-day return
        # and VIX's per-day delta carry opposite signs when both legs
        # observe consecutive days.
        spy_step = [(0.01 if i % 2 == 0 else -0.01) for i in range(window_days)]
        vix_step = [(-0.5 if i % 2 == 0 else 0.5) for i in range(window_days)]
        spy_closes = [400.0]
        vix_values_full = [15.0]
        for s_step, v_step in zip(spy_step, vix_step, strict=True):
            spy_closes.append(spy_closes[-1] * (1.0 + s_step))
            vix_values_full.append(vix_values_full[-1] + v_step)

        _seed_path(session, ticker=Symbol("SPY"), closes=spy_closes, start_day=start_day)
        missing_offsets = {5, 10, 15, window_days}
        vix_values_by_offset = {
            offset: value
            for offset, value in enumerate(vix_values_full)
            if offset not in missing_offsets
        }
        _seed_macro_at_dates(
            session,
            series_id="VIXCLS",
            values_by_offset=vix_values_by_offset,
            start_day=start_day,
        )
        # Stub the unrelated intermarket legs so all four blocks build.
        _seed_path(session, ticker=Symbol("TLT"), closes=[100.0] * 61, start_day=start_day)
        _seed_path(session, ticker=Symbol("GLD"), closes=[180.0] * 61, start_day=start_day)
        _seed_path(session, ticker=Symbol("XLE"), closes=[80.0] * 61, start_day=start_day)
        _seed_macro(session, series_id="DFII10", values=[1.5] * 61, start_day=start_day)
        _seed_macro(session, series_id="DCOILWTICO", values=[70.0] * 61, start_day=start_day)
        session.commit()

        blocks = compute_intermarket_regime(
            session,
            as_of=as_of,
            window_days=window_days,
            short_window_days=20,
        )

        vix_block = next(b for b in blocks if b.block_id.endswith("vix_spy"))
        actual_correlation = vix_block.payload["correlation"]
        assert actual_correlation is not None

        # Expected: intersect dates, take SPY closes and VIX values on the
        # intersected offsets in order, compute returns/deltas on the
        # joined series, pearson.
        common_offsets = sorted(set(range(window_days + 1)) - missing_offsets)
        spy_joined = [spy_closes[o] for o in common_offsets]
        vix_joined = [vix_values_full[o] for o in common_offsets]
        spy_returns_joined = _log_returns_from_closes(spy_joined)
        vix_deltas_joined = [b - a for a, b in pairwise(vix_joined)]
        expected_correlation = _pearson_correlation(spy_returns_joined, vix_deltas_joined)
        assert actual_correlation == pytest.approx(expected_correlation, abs=1e-9)

        # Sanity: with anti-correlated steps the date-joined correlation
        # comes out strongly negative even after a handful of gap-spanning
        # near-zero contributions.
        assert actual_correlation < -0.5

        # Negative assertion: the previous index-aligned slice produces a
        # materially different (near-zero) correlation on the same seeded
        # data. The fix has to move the answer.
        spy_returns_all = _log_returns_from_closes(spy_closes)
        vix_values_ordered = [v for _, v in sorted(vix_values_by_offset.items())]
        vix_deltas_all = [b - a for a, b in pairwise(vix_values_ordered)]
        positional_n = min(len(spy_returns_all), len(vix_deltas_all))
        positional_correlation = _pearson_correlation(
            spy_returns_all[-positional_n:], vix_deltas_all[-positional_n:]
        )
        assert abs(actual_correlation - positional_correlation) > 0.1, (
            f"actual {actual_correlation} matches positional {positional_correlation}; "
            "date-join had no effect"
        )

    def test_block_observation_count_reflects_joined_intersection(self, session: Session) -> None:
        # The bootstrap_reason carries ``<input_name>: <n_observations> < <required>``
        # when below threshold. After the fix the reported n must be the
        # joined-intersection size (returns/deltas from the intersected
        # value series), not the per-leg row count.
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        window_days = 60
        start_day = as_of - timedelta(days=window_days)
        # SPY: 16 daily bars (offsets 0..15) — sparse enough to leave the
        # block in an accumulating state so the reason carries the count.
        spy_closes = [400.0 + i for i in range(16)]
        _seed_path(session, ticker=Symbol("SPY"), closes=spy_closes, start_day=start_day)
        # VIX: 20 values on offsets 0..19, but offsets 5 and 7 missing.
        vix_values_by_offset = {
            offset: 15.0 + 0.1 * offset for offset in range(20) if offset not in {5, 7}
        }
        _seed_macro_at_dates(
            session,
            series_id="VIXCLS",
            values_by_offset=vix_values_by_offset,
            start_day=start_day,
        )
        # Stub the unrelated legs.
        _seed_path(session, ticker=Symbol("TLT"), closes=[100.0] * 61, start_day=start_day)
        _seed_path(session, ticker=Symbol("GLD"), closes=[180.0] * 61, start_day=start_day)
        _seed_path(session, ticker=Symbol("XLE"), closes=[80.0] * 61, start_day=start_day)
        _seed_macro(session, series_id="DFII10", values=[1.5] * 61, start_day=start_day)
        _seed_macro(session, series_id="DCOILWTICO", values=[70.0] * 61, start_day=start_day)
        session.commit()

        blocks = compute_intermarket_regime(
            session,
            as_of=as_of,
            window_days=window_days,
            short_window_days=20,
        )

        vix_block = next(b for b in blocks if b.block_id.endswith("vix_spy"))
        spy_offsets = set(range(16))
        vix_offsets = set(vix_values_by_offset)
        intersected = sorted(spy_offsets & vix_offsets)
        expected_n = len(intersected) - 1  # returns/deltas on the joined values
        assert vix_block.bootstrap_reason is not None
        assert f": {expected_n} < " in vix_block.bootstrap_reason, vix_block.bootstrap_reason

    def test_oil_xle_beta_uses_date_joined_intersection(self, session: Session) -> None:
        # Parallel coverage to the vix_spy case for the other motivating
        # symptom (oil/XLE long β collapsing to ≈ 0.04 in production). XLE
        # publishes a full NYSE-calendar window; DCOILWTICO is missing 3
        # interior FRED-holiday days plus the latest day (FRED lag).
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        window_days = 60
        start_day = as_of - timedelta(days=window_days)

        # Construct XLE moves and oil moves that share sign on the same day:
        # date-joined regression should recover a positive β; the previous
        # positional slice scrambles it.
        xle_step = [(0.01 if i % 2 == 0 else -0.01) for i in range(window_days)]
        oil_step = [(0.5 if i % 2 == 0 else -0.5) for i in range(window_days)]
        xle_closes = [80.0]
        oil_values_full = [70.0]
        for x_step, o_step in zip(xle_step, oil_step, strict=True):
            xle_closes.append(xle_closes[-1] * (1.0 + x_step))
            oil_values_full.append(oil_values_full[-1] * (1.0 + o_step / 100.0))

        _seed_path(session, ticker=Symbol("XLE"), closes=xle_closes, start_day=start_day)
        missing_offsets = {5, 10, 15, window_days}
        oil_values_by_offset = {
            offset: value
            for offset, value in enumerate(oil_values_full)
            if offset not in missing_offsets
        }
        _seed_macro_at_dates(
            session,
            series_id="DCOILWTICO",
            values_by_offset=oil_values_by_offset,
            start_day=start_day,
        )
        _seed_path(session, ticker=Symbol("SPY"), closes=[400.0] * 61, start_day=start_day)
        _seed_path(session, ticker=Symbol("TLT"), closes=[100.0] * 61, start_day=start_day)
        _seed_path(session, ticker=Symbol("GLD"), closes=[180.0] * 61, start_day=start_day)
        _seed_macro(session, series_id="DFII10", values=[1.5] * 61, start_day=start_day)
        _seed_macro(session, series_id="VIXCLS", values=[15.0] * 61, start_day=start_day)
        session.commit()

        blocks = compute_intermarket_regime(
            session,
            as_of=as_of,
            window_days=window_days,
            short_window_days=20,
        )

        oil_block = next(b for b in blocks if b.block_id.endswith("oil_xle_beta"))
        actual_beta = oil_block.payload["long_beta"]
        assert actual_beta is not None
        assert actual_beta > 0.3, (
            f"date-joined oil-vs-XLE β should be materially positive, got {actual_beta}"
        )

    def test_gld_real_yields_uses_date_joined_intersection(self, session: Session) -> None:
        # GLD publishes a full NYSE-calendar window; DFII10 is missing 3
        # interior days + the latest day. With anti-correlated daily moves
        # the date-joined correlation must be materially negative.
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        window_days = 60
        start_day = as_of - timedelta(days=window_days)

        gld_step = [(0.01 if i % 2 == 0 else -0.01) for i in range(window_days)]
        yield_step = [(-0.02 if i % 2 == 0 else 0.02) for i in range(window_days)]
        gld_closes = [180.0]
        yield_values_full = [1.5]
        for g_step, y_step in zip(gld_step, yield_step, strict=True):
            gld_closes.append(gld_closes[-1] * (1.0 + g_step))
            yield_values_full.append(yield_values_full[-1] + y_step)

        _seed_path(session, ticker=Symbol("GLD"), closes=gld_closes, start_day=start_day)
        missing_offsets = {5, 10, 15, window_days}
        yield_values_by_offset = {
            offset: value
            for offset, value in enumerate(yield_values_full)
            if offset not in missing_offsets
        }
        _seed_macro_at_dates(
            session,
            series_id="DFII10",
            values_by_offset=yield_values_by_offset,
            start_day=start_day,
        )
        _seed_path(session, ticker=Symbol("SPY"), closes=[400.0] * 61, start_day=start_day)
        _seed_path(session, ticker=Symbol("TLT"), closes=[100.0] * 61, start_day=start_day)
        _seed_path(session, ticker=Symbol("XLE"), closes=[80.0] * 61, start_day=start_day)
        _seed_macro(session, series_id="VIXCLS", values=[1.5] * 61, start_day=start_day)
        _seed_macro(session, series_id="DCOILWTICO", values=[70.0] * 61, start_day=start_day)
        session.commit()

        blocks = compute_intermarket_regime(
            session,
            as_of=as_of,
            window_days=window_days,
            short_window_days=20,
        )

        gld_block = next(b for b in blocks if b.block_id.endswith("gld_real_yields"))
        actual_correlation = gld_block.payload["correlation"]
        assert actual_correlation is not None
        assert actual_correlation < -0.5, (
            f"date-joined gld vs real-yields correlation should be materially "
            f"negative, got {actual_correlation}"
        )
