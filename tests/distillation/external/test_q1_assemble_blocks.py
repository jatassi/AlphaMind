"""Tests for the Q1 top-level assemble_q1_blocks entry point — story 12 follow-up.

This is the orchestrator-shaped function that fans out per
``(sector_audience, indicator_group)`` and produces the flat
list of :class:`OutputBlock` instances the orchestrator's per-category-indicator-compute dispatcher
will swap in for ``_placeholder_blocks("q1")``.

Acceptance criteria (one test per criterion):

- Returns a list of OutputBlock instances.
- One block per (sector_audience, indicator_group); per-ticker payload sorted.
- Anomaly blocks (volume_anomaly / price_move_anomaly) included when the
  fixture inputs satisfy the firing conditions.
- Bootstrap path: when per-ticker baseline is below ``*_min_observations``,
  affected indicator block carries ``BOOTSTRAP`` calibration; anomaly
  severity downgrades to ``investigate_if_persists``.
- Empty ``ticker_scope`` returns ``[]`` (no fan-out, no blocks).
- Determinism: two calls produce byte-identical block lists.
- Configuration thresholds (``volume_anomaly_sigma``,
  ``price_move_atr_multiple``) reach the function via the loaded
  :class:`DistillationConfig` — never inlined, never read directly from YAML.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session

from alphamind.distillation._config_domain import DistillationDomainConfig
from alphamind.distillation._repository import TickerBaselineRow
from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.output import OutputAudience, OutputBlock, format_block
from alphamind.distillation.q1 import BLOCK_ID_VOLUME_ANOMALY, assemble_q1_blocks
from alphamind.distillation.q1.assemble import _atr_regime_label_and_tag
from alphamind.distillation.q1.output_blocks import (
    BLOCK_ID_DIVERGENCE_FLAGS,
    BLOCK_ID_GAP,
    BLOCK_ID_RELATIVE_PERFORMANCE,
    BLOCK_ID_TECHNICALS,
    BLOCK_ID_TREND_STATE,
    BLOCK_ID_VOLUME_PROFILE,
)
from alphamind.persistence.models import (
    AssetUniverse,
    DistillationTickerBaseline,
    OhlcvBars,
    SectorClassification,
)

from .conftest import _build_distillation_config as _build_shared_distillation_config

# ---------------------------------------------------------------------------
# Distillation config builder — Q1 calibration
# ---------------------------------------------------------------------------
#
# Q1's anomaly tests are calibrated against ``volume_anomaly_sigma=2.5`` /
# ``price_move_atr_multiple=1.5`` defaults (distinct from the shared
# conftest builder's ``2.0`` / ``2.5``). This thin wrapper restores those
# Q1-specific defaults while delegating the full config literal to the
# shared builder, so call sites that override ``volume_anomaly_sigma`` keep
# working unchanged.


def _build_distillation_config(
    *,
    volume_anomaly_sigma: float = 2.5,
    price_move_atr_multiple: float = 1.5,
    volume_baseline_days: int = 20,
    atr_baseline_days: int = 14,
) -> DistillationDomainConfig:
    """Return a Q1-calibrated :class:`DistillationDomainConfig` for tests."""
    return _build_shared_distillation_config(
        volume_anomaly_sigma=volume_anomaly_sigma,
        price_move_atr_multiple=price_move_atr_multiple,
        volume_baseline_days=volume_baseline_days,
        atr_baseline_days=atr_baseline_days,
    )


# ---------------------------------------------------------------------------
# Fixture seeding helpers
# ---------------------------------------------------------------------------


_SECTOR_TICKERS: dict[str, tuple[str, str, str]] = {
    "NVDA": ("semis", "tech_semis", "SMH"),
    "AAPL": ("tech", "tech_semis", "XLK"),
    "JPM": ("financials", "financials", "XLF"),
    "XOM": ("energy", "energy", "XLE"),
}
"""Fixture roster — one ticker per ``alphamind_sector``; tech_semis covers two."""


_BENCHMARK_TICKERS: tuple[str, ...] = ("SPY", "SMH", "XLK", "XLF", "XLE")
"""Sector ETF + SPY tickers needed for relative-performance computation."""


def _add_universe_ticker(
    session: Session,
    *,
    ticker: str,
    asset_class: str = "equity",
    asset_role: str = "universe",
) -> None:
    session.add(
        AssetUniverse(
            asset_id=f"asset-{ticker.lower()}",
            ticker=ticker,
            full_name=f"{ticker} Holdings",
            asset_class=asset_class,
            asset_role=asset_role,
            exchange="NASDAQ",
            avg_daily_volume_shares=1_000_000,
            is_active=1,
            added_date="2020-01-01",
            last_updated="2026-04-26T00:00:00Z",
        )
    )


def _add_sector_classification(
    session: Session,
    *,
    ticker: str,
    sector: str,
    domain_researcher: str,
    etf: str,
) -> None:
    session.add(
        SectorClassification(
            ticker=ticker,
            asset_id=f"asset-{ticker.lower()}",
            alphamind_sector=sector,
            domain_researcher=domain_researcher,
            sector_etf=etf,
            classification_source="test",
            last_updated="2026-04-26T00:00:00Z",
        )
    )


def _seed_universe_and_sectors(session: Session) -> None:
    """Seed the four sector tickers + SPY/sector-ETF benchmark rows."""
    for ticker, (sector, domain_researcher, etf) in _SECTOR_TICKERS.items():
        _add_universe_ticker(session=session, ticker=ticker)
        _add_sector_classification(
            session=session,
            ticker=ticker,
            sector=sector,
            domain_researcher=domain_researcher,
            etf=etf,
        )
    for benchmark in _BENCHMARK_TICKERS:
        _add_universe_ticker(
            session=session, ticker=benchmark, asset_class="etf", asset_role="proxy"
        )
    session.commit()


def _seed_ohlcv_for_ticker(
    session: Session,
    *,
    ticker: str,
    days: int,
    end: datetime,
    base_price: float = 100.0,
    base_volume: int = 1_000_000,
) -> None:
    """Seed daily bars ending at ``end`` with deterministic perturbations."""
    for offset in range(days, 0, -1):
        bar_date = end - timedelta(days=offset)
        perturbation = ((offset * 7) % 11) * 0.5
        price = base_price + perturbation
        volume = base_volume + ((offset * 13) % 17) * 10_000
        session.add(
            OhlcvBars(
                ticker=ticker,
                period_start=bar_date.strftime("%Y-%m-%dT00:00:00Z"),
                timeframe="1d",
                period_end=bar_date.strftime("%Y-%m-%dT23:59:59Z"),
                session="regular",
                adj_open=price,
                adj_high=price + 1.0,
                adj_low=price - 1.0,
                adj_close=price + 0.5,
                adj_volume=volume,
                adj_vwap=price,
                unadj_open=price,
                unadj_high=price + 1.0,
                unadj_low=price - 1.0,
                unadj_close=price + 0.5,
                unadj_volume=volume,
                unadj_vwap=price,
                trade_count=100,
                source="test",
                ingested_at=bar_date.strftime("%Y-%m-%dT00:00:00Z"),
            )
        )


def _seed_baseline(
    session: Session,
    *,
    ticker: str,
    kind: str,
    as_of: str,
    mean: float,
    stdev: float,
    n_observations: int,
    window_days: int,
    state: CalibrationState = CalibrationState.CALIBRATED,
) -> None:
    session.add(
        DistillationTickerBaseline(
            ticker=ticker,
            baseline_kind=kind,
            as_of=as_of,
            mean=mean,
            stdev=stdev,
            n_observations=n_observations,
            window_days=window_days,
            calibration_state=state.value,
            ingested_at=as_of,
        )
    )


def _format_iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Tests — one per acceptance criterion
# ---------------------------------------------------------------------------


class TestEmptyTickerScope:
    """Empty ``ticker_scope`` short-circuits to an empty block list."""

    def test_empty_scope_returns_empty_list(self, session: Session) -> None:
        config = _build_distillation_config()
        as_of = datetime(2026, 4, 25, tzinfo=UTC)
        blocks = assemble_q1_blocks(
            session,
            config=config,
            as_of=as_of,
            ticker_scope=[],
        )
        assert blocks == []


# ---------------------------------------------------------------------------
# Common populated session for non-empty fan-out tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def populated_session(session: Session) -> Session:
    """A session seeded with three sectors, ETF benchmarks, and 60 days of bars."""
    _seed_universe_and_sectors(session)
    end = datetime(2026, 4, 25, tzinfo=UTC)
    days = 220  # Enough for the 200-EMA and the 20-day windows
    for ticker in (*_SECTOR_TICKERS.keys(), *_BENCHMARK_TICKERS):
        # Use distinct base prices so the cross-sectional sort is stable.
        base = 100.0 + (sum(ord(c) for c in ticker) % 13)
        _seed_ohlcv_for_ticker(
            session,
            ticker=ticker,
            days=days,
            end=end,
            base_price=base,
        )
    # Seed CALIBRATED volume + ATR baselines for each sector ticker so the
    # default fan-out emits CALIBRATED blocks in the simplest case.
    as_of_iso = _format_iso_z(end)
    for ticker in _SECTOR_TICKERS:
        _seed_baseline(
            session,
            ticker=ticker,
            kind="volume",
            as_of=as_of_iso,
            mean=1_000_000.0,
            stdev=10_000.0,
            n_observations=60,
            window_days=20,
            state=CalibrationState.CALIBRATED,
        )
        _seed_baseline(
            session,
            ticker=ticker,
            kind="atr",
            as_of=as_of_iso,
            mean=2.0,
            stdev=0.2,
            n_observations=60,
            window_days=14,
            state=CalibrationState.CALIBRATED,
        )
    session.commit()
    return session


# ---------------------------------------------------------------------------
# Default fan-out shape
# ---------------------------------------------------------------------------


def test_default_fan_out_shape(populated_session: Session) -> None:
    """One combined assertion for all five structural facts of the default fan-out.

    Facts asserted:
    1. Returns a non-empty list of :class:`OutputBlock` instances.
    2. ``payload['per_ticker']`` keys are sorted by ticker on every per-ticker block.
    3. No duplicate ``(audience, block_id)`` pairs — one block per sector/indicator-group.
    4. Each block's audience is a singleton sector audience.
    5. Every populated sector audience emits exactly the six documented indicator-group blocks.
    """
    config = _build_distillation_config()
    as_of = datetime(2026, 4, 25, tzinfo=UTC)
    blocks = assemble_q1_blocks(populated_session, config=config, as_of=as_of)

    # Fact 1 — returns OutputBlock instances.
    assert isinstance(blocks, list)
    assert len(blocks) > 0
    for block in blocks:
        assert isinstance(block, OutputBlock)

    # Fact 2 — per_ticker payload sorted by ticker.
    per_ticker_blocks = [b for b in blocks if "per_ticker" in b.payload]
    assert per_ticker_blocks, "Expected at least one per-ticker block"
    for block in per_ticker_blocks:
        keys = list(block.payload["per_ticker"].keys())
        assert keys == sorted(keys), f"Block {block.block_id} per_ticker keys not sorted: {keys}"

    # Fact 3 — no duplicate (audience, block_id) pairs.
    seen: set[tuple[frozenset[OutputAudience], str]] = set()
    for block in blocks:
        key = (block.audience, block.block_id)
        assert key not in seen, (
            f"Duplicate (audience, block_id) pair: {block.audience} / {block.block_id}"
        )
        seen.add(key)

    # Fact 4 — singleton-sector audience on every block.
    sector_audiences = {
        OutputAudience.SECTOR_TECH_SEMIS,
        OutputAudience.SECTOR_FINANCIALS,
        OutputAudience.SECTOR_ENERGY,
    }
    for block in blocks:
        assert len(block.audience) == 1, (
            f"Block {block.block_id} audience not singleton: {block.audience}"
        )
        (only,) = tuple(block.audience)
        assert only in sector_audiences, (
            f"Block {block.block_id} audience {only} not a sector audience"
        )

    # Fact 5 — six indicator-group blocks per populated audience.
    per_audience_block_ids: dict[OutputAudience, set[str]] = {}
    for block in blocks:
        (audience,) = tuple(block.audience)
        per_audience_block_ids.setdefault(audience, set()).add(block.block_id)
    expected_ids = {
        BLOCK_ID_TECHNICALS,
        BLOCK_ID_VOLUME_PROFILE,
        BLOCK_ID_GAP,
        BLOCK_ID_RELATIVE_PERFORMANCE,
        BLOCK_ID_TREND_STATE,
        BLOCK_ID_DIVERGENCE_FLAGS,
    }
    for audience in (
        OutputAudience.SECTOR_TECH_SEMIS,
        OutputAudience.SECTOR_FINANCIALS,
        OutputAudience.SECTOR_ENERGY,
    ):
        assert audience in per_audience_block_ids, (
            f"Audience {audience} produced no blocks; saw {sorted(per_audience_block_ids)}"
        )
        actual = per_audience_block_ids[audience]
        missing = expected_ids - actual
        assert not missing, (
            f"Audience {audience} missing blocks {sorted(missing)}; saw {sorted(actual)}"
        )


class TestAnomalyBlocksFire:
    """When anomaly conditions are satisfied, the corresponding blocks appear."""

    def test_volume_anomaly_block_emitted_when_today_volume_spikes(self, session: Session) -> None:
        """A 5-sigma volume spike against the seeded baseline produces a volume_anomaly block."""
        _seed_universe_and_sectors(session)
        end = datetime(2026, 4, 25, tzinfo=UTC)
        days = 60
        # Seed normal bars for every ticker; then overwrite NVDA's most-recent bar
        # with a volume spike well above the seeded baseline mean + sigma * stdev.
        for ticker in (*_SECTOR_TICKERS.keys(), *_BENCHMARK_TICKERS):
            _seed_ohlcv_for_ticker(session, ticker=ticker, days=days, end=end, base_price=100.0)
        # Spike NVDA's last bar.
        nvda_today = end - timedelta(days=0)
        # Manually overwrite the most-recent bar's volume.

        session.execute(
            update(OhlcvBars)
            .where(
                OhlcvBars.ticker == "NVDA",
                OhlcvBars.timeframe == "1d",
                OhlcvBars.period_start
                == _format_iso_z(nvda_today - timedelta(days=1))[:10] + "T00:00:00Z",
            )
            .values(adj_volume=10_000_000)
        )
        # Seed a tight baseline so 10M >> mean + 2.5-sigma * stdev.
        as_of_iso = _format_iso_z(end)
        for ticker in _SECTOR_TICKERS:
            _seed_baseline(
                session,
                ticker=ticker,
                kind="volume",
                as_of=as_of_iso,
                mean=1_000_000.0,
                stdev=100_000.0,
                n_observations=60,
                window_days=20,
            )
            _seed_baseline(
                session,
                ticker=ticker,
                kind="atr",
                as_of=as_of_iso,
                mean=2.0,
                stdev=0.2,
                n_observations=60,
                window_days=14,
            )
        session.commit()

        config = _build_distillation_config(volume_anomaly_sigma=2.5)
        blocks = assemble_q1_blocks(session, config=config, as_of=end)

        anomaly_blocks = [b for b in blocks if b.block_id == BLOCK_ID_VOLUME_ANOMALY]
        assert anomaly_blocks, "Expected a volume_anomaly block when a ticker spikes"
        # The spiked ticker fires its own per_ticker entry.
        per_ticker_seen: set[str] = set()
        for block in anomaly_blocks:
            per_ticker_seen.update(block.payload["per_ticker"].keys())
        assert "NVDA" in per_ticker_seen


class TestBootstrapPath:
    """Below-min-observations baseline tags blocks BOOTSTRAP and downgrades severity."""

    def test_bootstrap_baseline_tags_indicator_block_bootstrap(self, session: Session) -> None:
        """A BOOTSTRAP-tagged baseline yields a BOOTSTRAP-tagged indicator block."""
        _seed_universe_and_sectors(session)
        end = datetime(2026, 4, 25, tzinfo=UTC)
        days = 60
        for ticker in (*_SECTOR_TICKERS.keys(), *_BENCHMARK_TICKERS):
            _seed_ohlcv_for_ticker(session, ticker=ticker, days=days, end=end, base_price=100.0)
        as_of_iso = _format_iso_z(end)
        # Seed baselines BELOW the minimum observation threshold so the
        # framework tags them BOOTSTRAP — this exercises the per-block tag.
        for ticker in _SECTOR_TICKERS:
            _seed_baseline(
                session,
                ticker=ticker,
                kind="volume",
                as_of=as_of_iso,
                mean=1_000_000.0,
                stdev=100_000.0,
                n_observations=3,  # below min_observations
                window_days=20,
                state=CalibrationState.ACCUMULATING,
            )
            _seed_baseline(
                session,
                ticker=ticker,
                kind="atr",
                as_of=as_of_iso,
                mean=2.0,
                stdev=0.2,
                n_observations=3,
                window_days=14,
                state=CalibrationState.ACCUMULATING,
            )
        session.commit()

        config = _build_distillation_config()
        blocks = assemble_q1_blocks(session, config=config, as_of=end)

        # At least one block carries ACCUMULATING — the volume-baseline tag
        # propagates to the volume_profile / relative_performance blocks.
        accumulating_blocks = [
            b for b in blocks if b.calibration_state is CalibrationState.ACCUMULATING
        ]
        assert accumulating_blocks, (
            "Expected at least one ACCUMULATING block when baselines are sub-threshold"
        )

    def test_q1_producer_emits_investigate_now_under_accumulating_baseline(
        self, session: Session
    ) -> None:
        """ALP-544 — the q1 producer emits investigate_now regardless of baseline state.

        Severity downgrade now lives at the publishing-layer cap
        (:mod:`alphamind.distillation._severity_cap`), not at the
        producer. This test pins the producer's contract: every fired
        volume spike carries ``investigate_now`` at the block boundary;
        the orchestrator's cap downgrades when the block's
        calibration_state is non-CALIBRATED.
        """
        _seed_universe_and_sectors(session)
        end = datetime(2026, 4, 25, tzinfo=UTC)
        days = 60
        for ticker in (*_SECTOR_TICKERS.keys(), *_BENCHMARK_TICKERS):
            _seed_ohlcv_for_ticker(session, ticker=ticker, days=days, end=end, base_price=100.0)
        # Spike NVDA's most-recent bar volume.

        nvda_today = end - timedelta(days=1)
        session.execute(
            update(OhlcvBars)
            .where(
                OhlcvBars.ticker == "NVDA",
                OhlcvBars.timeframe == "1d",
                OhlcvBars.period_start == _format_iso_z(nvda_today)[:10] + "T00:00:00Z",
            )
            .values(adj_volume=10_000_000)
        )
        as_of_iso = _format_iso_z(end)
        # Seed ACCUMULATING baselines.
        for ticker in _SECTOR_TICKERS:
            _seed_baseline(
                session,
                ticker=ticker,
                kind="volume",
                as_of=as_of_iso,
                mean=1_000_000.0,
                stdev=100_000.0,
                n_observations=3,
                window_days=20,
                state=CalibrationState.ACCUMULATING,
            )
            _seed_baseline(
                session,
                ticker=ticker,
                kind="atr",
                as_of=as_of_iso,
                mean=2.0,
                stdev=0.2,
                n_observations=3,
                window_days=14,
                state=CalibrationState.ACCUMULATING,
            )
        session.commit()

        config = _build_distillation_config(volume_anomaly_sigma=2.5)
        blocks = assemble_q1_blocks(session, config=config, as_of=end)

        anomaly_blocks = [b for b in blocks if b.block_id == BLOCK_ID_VOLUME_ANOMALY]
        assert anomaly_blocks
        firing_severities = [
            flag.severity for block in anomaly_blocks for flag in block.anomaly_flags
        ]
        assert firing_severities, "Expected at least one anomaly flag for the volume spike"
        assert all(s == "investigate_now" for s in firing_severities), (
            f"Producer must emit investigate_now uniformly; got {firing_severities}"
        )


class TestDeterminism:
    """Two calls on the same fixture produce byte-identical block lists."""

    def test_two_calls_produce_identical_block_lists(self, populated_session: Session) -> None:
        config = _build_distillation_config()
        as_of = datetime(2026, 4, 25, tzinfo=UTC)
        blocks_1 = assemble_q1_blocks(populated_session, config=config, as_of=as_of)
        blocks_2 = assemble_q1_blocks(populated_session, config=config, as_of=as_of)

        assert len(blocks_1) == len(blocks_2)

        rendered_1 = "\n".join(format_block(b) for b in blocks_1)
        rendered_2 = "\n".join(format_block(b) for b in blocks_2)
        assert rendered_1 == rendered_2, "Two calls produced different block renderings"


class TestConfigDrivenThresholds:
    """The function reads anomaly thresholds via the passed-in DistillationConfig."""

    def test_changing_volume_anomaly_sigma_changes_firing_set(self, session: Session) -> None:
        """Tightening ``volume_anomaly_sigma`` reduces the firing-ticker set."""
        _seed_universe_and_sectors(session)
        end = datetime(2026, 4, 25, tzinfo=UTC)
        days = 60
        for ticker in (*_SECTOR_TICKERS.keys(), *_BENCHMARK_TICKERS):
            _seed_ohlcv_for_ticker(session, ticker=ticker, days=days, end=end, base_price=100.0)
        as_of_iso = _format_iso_z(end)
        # NVDA volume on its last bar is the seeded value. We seed a
        # baseline whose mean/stdev produce a 3.0-sigma deviation against
        # NVDA's last-bar volume.

        session.execute(
            update(OhlcvBars)
            .where(
                OhlcvBars.ticker == "NVDA",
                OhlcvBars.timeframe == "1d",
                OhlcvBars.period_start
                == _format_iso_z(end - timedelta(days=1))[:10] + "T00:00:00Z",
            )
            .values(adj_volume=1_300_000)  # 3-sigma above mean=1M, stdev=100k
        )
        for ticker in _SECTOR_TICKERS:
            _seed_baseline(
                session,
                ticker=ticker,
                kind="volume",
                as_of=as_of_iso,
                mean=1_000_000.0,
                stdev=100_000.0,
                n_observations=60,
                window_days=20,
            )
            _seed_baseline(
                session,
                ticker=ticker,
                kind="atr",
                as_of=as_of_iso,
                mean=2.0,
                stdev=0.2,
                n_observations=60,
                window_days=14,
            )
        session.commit()

        # Loose threshold (sigma=2) — fires.
        loose_config = _build_distillation_config(volume_anomaly_sigma=2.0)
        loose_blocks = assemble_q1_blocks(session, config=loose_config, as_of=end)

        # Tight threshold (sigma=4) — does not fire on the same data.
        tight_config = _build_distillation_config(volume_anomaly_sigma=4.0)
        tight_blocks = assemble_q1_blocks(session, config=tight_config, as_of=end)

        loose_anomaly = [b for b in loose_blocks if b.block_id == BLOCK_ID_VOLUME_ANOMALY]
        tight_anomaly = [b for b in tight_blocks if b.block_id == BLOCK_ID_VOLUME_ANOMALY]

        # Loose threshold fires; tight threshold doesn't.
        assert loose_anomaly, "Loose threshold should fire"
        assert not tight_anomaly, "Tight threshold should not fire on this fixture"


class TestAtrRegimeLabelAndTag:
    """ALP-631: every non-CALIBRATED return carries a non-None reason.

    The previously-broken path: when the ATR baseline row itself carries
    ``calibration_state="unavailable"`` (collector-side gap, not a missing
    row), the helper returned ``(_, UNAVAILABLE, None)``, which propagated
    a ``None`` reason to the ``q1.trend_state`` block and rendered as an
    empty-string reason in the operator-facing summary and the literal
    ``None`` in the researcher-input header.
    """

    def _baseline(
        self,
        *,
        ticker: str = "AAPL",
        state: CalibrationState,
        n_observations: int = 60,
        window_days: int = 14,
    ) -> TickerBaselineRow:
        return TickerBaselineRow(
            ticker=ticker,
            baseline_kind="atr",
            as_of="2026-04-25T00:00:00Z",
            mean=2.0,
            stdev=0.2,
            n_observations=n_observations,
            window_days=window_days,
            calibration_state=state.value,
        )

    def test_missing_baseline_returns_unavailable_with_reason(self) -> None:
        _label, state, reason = _atr_regime_label_and_tag(ticker="AAPL", atr=1.8, atr_baseline=None)
        assert state is CalibrationState.UNAVAILABLE
        assert reason == "atr_baseline missing for AAPL"

    def test_unavailable_baseline_returns_unavailable_with_reason(self) -> None:
        baseline = self._baseline(state=CalibrationState.UNAVAILABLE)
        _label, state, reason = _atr_regime_label_and_tag(
            ticker="AAPL", atr=1.8, atr_baseline=baseline
        )
        assert state is CalibrationState.UNAVAILABLE
        assert reason == "atr_baseline calibration_state=unavailable for AAPL"

    def test_accumulating_baseline_returns_accumulating_with_observation_count(self) -> None:
        baseline = self._baseline(
            state=CalibrationState.ACCUMULATING, n_observations=4, window_days=14
        )
        _label, state, reason = _atr_regime_label_and_tag(
            ticker="AAPL", atr=1.8, atr_baseline=baseline
        )
        assert state is CalibrationState.ACCUMULATING
        assert reason == "atr_baseline_days: 4 < 14"

    def test_calibrated_baseline_returns_calibrated_with_no_reason(self) -> None:
        baseline = self._baseline(state=CalibrationState.CALIBRATED)
        _label, state, reason = _atr_regime_label_and_tag(
            ticker="AAPL", atr=1.8, atr_baseline=baseline
        )
        assert state is CalibrationState.CALIBRATED
        assert reason is None
