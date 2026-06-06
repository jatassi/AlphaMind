"""Tests for ``alphamind.distillation.q3.etf_iv_divergence_loaders`` (ALP-812).

The IO shell pre-loads every per-sector input the ETF/single-name IV-divergence
compute consumes. These tests drive the four loader functions against the
in-memory ``session`` fixture (the sanctioned DB boundary), seeding
``OptionsContracts`` / ``OptionsContractSnapshots`` / ``SectorClassification``
(and the ``AssetUniverse`` rows their FKs require) with local helpers mirrored
from ``tests/distillation/external/test_q3_options.py``.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from alphamind.distillation.q3 import etf_iv_divergence_loaders as loaders
from alphamind.persistence.models import (
    AssetUniverse,
    OptionsContracts,
    OptionsContractSnapshots,
    SectorClassification,
)

_AS_OF = "2026-05-07T20:00:00Z"
_BASELINE_DAYS = 30


def _add_ticker(session: Session, ticker: str) -> None:
    session.add(
        AssetUniverse(
            asset_id=f"asset-{ticker.lower()}",
            ticker=ticker,
            full_name=f"{ticker} Inc",
            asset_class="equity",
            asset_role="universe",
            exchange="NASDAQ",
            avg_daily_volume_shares=1_000_000,
            is_active=1,
            added_date="2020-01-01",
            last_updated="2026-04-26T00:00:00Z",
        )
    )
    session.flush()


def _add_sector(session: Session, *, ticker: str, sector_etf: str) -> None:
    session.add(
        SectorClassification(
            ticker=ticker,
            asset_id=f"asset-{ticker.lower()}",
            alphamind_sector="technology",
            domain_researcher="tech-semis",
            sector_etf=sector_etf,
            classification_source="test",
            last_updated="2026-04-26T00:00:00Z",
        )
    )
    session.flush()


def _add_call(
    session: Session,
    *,
    underlying: str,
    snapshot_ts: str,
    iv: float | None,
    underlying_price: float,
    strike: float | None = None,
    volume: int | None = None,
) -> None:
    """Add one call contract + its snapshot. Strike defaults to ATM (== price)."""
    strike = underlying_price if strike is None else strike
    contract_ticker = f"O:{underlying}:{snapshot_ts}:{strike}"
    session.add(
        OptionsContracts(
            contract_ticker=contract_ticker,
            underlying_ticker=underlying,
            expiration_date="2026-06-19",
            strike_price=strike,
            contract_type="call",
            first_seen_at="2026-04-01T00:00:00Z",
            last_seen_at=snapshot_ts,
            source="test",
        )
    )
    session.flush()
    # Only the three fields the loaders read (implied_volatility, underlying_price,
    # volume_today) plus the NOT-NULL columns are set; the nullable greeks / OI /
    # bid-ask are left unset because no unit under test consults them.
    session.add(
        OptionsContractSnapshots(
            snapshot_ts=snapshot_ts,
            contract_ticker=contract_ticker,
            underlying_ticker=underlying,
            volume_today=volume,
            implied_volatility=iv,
            underlying_price=underlying_price,
            source="test",
            ingested_at=snapshot_ts,
        )
    )
    session.flush()


# ---------------------------------------------------------------------------
# _select_atm_iv_at
# ---------------------------------------------------------------------------


class TestSelectAtmIvAt:
    def test_picks_strike_closest_to_underlying_price(self, session: Session) -> None:
        _add_ticker(session, "AAPL")
        # underlying_price 100: strike 101 (gap 1) is the ATM pick over 90 / 110 (gap 10).
        _add_call(
            session, underlying="AAPL", snapshot_ts=_AS_OF, iv=0.25, underlying_price=100, strike=90
        )
        _add_call(
            session,
            underlying="AAPL",
            snapshot_ts=_AS_OF,
            iv=0.30,
            underlying_price=100,
            strike=101,
        )
        _add_call(
            session,
            underlying="AAPL",
            snapshot_ts=_AS_OF,
            iv=0.40,
            underlying_price=100,
            strike=110,
        )

        assert loaders._select_atm_iv_at(session, underlying="AAPL", as_of=_AS_OF) == pytest.approx(
            0.30
        )

    def test_none_when_no_snapshot_at_as_of(self, session: Session) -> None:
        _add_ticker(session, "AAPL")
        _add_call(
            session,
            underlying="AAPL",
            snapshot_ts="2026-05-06T20:00:00Z",  # a different day
            iv=0.30,
            underlying_price=100,
        )
        assert loaders._select_atm_iv_at(session, underlying="AAPL", as_of=_AS_OF) is None

    def test_none_when_candidate_iv_is_null(self, session: Session) -> None:
        _add_ticker(session, "AAPL")
        _add_call(session, underlying="AAPL", snapshot_ts=_AS_OF, iv=None, underlying_price=100)
        assert loaders._select_atm_iv_at(session, underlying="AAPL", as_of=_AS_OF) is None


# ---------------------------------------------------------------------------
# _select_aggregate_single_name_iv
# ---------------------------------------------------------------------------


class TestSelectAggregateSingleNameIv:
    def test_volume_weighted_average(self, session: Session) -> None:
        _add_ticker(session, "AAPL")
        _add_ticker(session, "MSFT")
        _add_call(
            session,
            underlying="AAPL",
            snapshot_ts=_AS_OF,
            iv=0.20,
            underlying_price=100,
            volume=200,
        )
        _add_call(
            session,
            underlying="MSFT",
            snapshot_ts=_AS_OF,
            iv=0.40,
            underlying_price=200,
            volume=600,
        )

        # (0.20*200 + 0.40*600) / (200 + 600) = 0.35
        result = loaders._select_aggregate_single_name_iv(
            session, tickers=["AAPL", "MSFT"], as_of=_AS_OF
        )
        assert result == pytest.approx(0.35)

    def test_weight_one_when_volume_missing_or_nonpositive(self, session: Session) -> None:
        _add_ticker(session, "AAPL")
        _add_ticker(session, "MSFT")
        _add_call(
            session,
            underlying="AAPL",
            snapshot_ts=_AS_OF,
            iv=0.20,
            underlying_price=100,
            volume=None,
        )
        _add_call(
            session, underlying="MSFT", snapshot_ts=_AS_OF, iv=0.40, underlying_price=200, volume=0
        )

        # Both fall back to weight 1.0 → simple mean (0.20 + 0.40) / 2 = 0.30
        result = loaders._select_aggregate_single_name_iv(
            session, tickers=["AAPL", "MSFT"], as_of=_AS_OF
        )
        assert result == pytest.approx(0.30)

    def test_skips_constituent_with_no_same_day_snapshot(self, session: Session) -> None:
        _add_ticker(session, "AAPL")
        _add_ticker(session, "MSFT")
        _add_call(
            session,
            underlying="AAPL",
            snapshot_ts=_AS_OF,
            iv=0.20,
            underlying_price=100,
            volume=100,
        )
        _add_call(
            session,
            underlying="MSFT",
            snapshot_ts=_AS_OF,
            iv=0.40,
            underlying_price=200,
            volume=100,
        )

        # GOOG has no snapshot at all → skipped; result is the AAPL/MSFT mean.
        result = loaders._select_aggregate_single_name_iv(
            session, tickers=["AAPL", "MSFT", "GOOG"], as_of=_AS_OF
        )
        assert result == pytest.approx(0.30)

    def test_none_when_no_constituent_contributes(self, session: Session) -> None:
        _add_ticker(session, "AAPL")
        _add_call(
            session,
            underlying="AAPL",
            snapshot_ts="2026-05-06T20:00:00Z",  # not as_of
            iv=0.20,
            underlying_price=100,
        )
        result = loaders._select_aggregate_single_name_iv(session, tickers=["AAPL"], as_of=_AS_OF)
        assert result is None


# ---------------------------------------------------------------------------
# _select_etf_iv_spread_baseline
# ---------------------------------------------------------------------------


class TestSelectEtfIvSpreadBaseline:
    def test_returns_fmean_and_pstdev_of_paired_spread(self, session: Session) -> None:
        _add_ticker(session, "XLK")
        _add_ticker(session, "AAPL")
        # ETF history [0.50, 0.50]; constituent history [0.40, 0.20] (sorted by ts).
        _add_call(
            session,
            underlying="XLK",
            snapshot_ts="2026-04-20T20:00:00Z",
            iv=0.50,
            underlying_price=300,
        )
        _add_call(
            session,
            underlying="XLK",
            snapshot_ts="2026-04-27T20:00:00Z",
            iv=0.50,
            underlying_price=300,
        )
        _add_call(
            session,
            underlying="AAPL",
            snapshot_ts="2026-04-20T20:00:00Z",
            iv=0.40,
            underlying_price=100,
        )
        _add_call(
            session,
            underlying="AAPL",
            snapshot_ts="2026-04-27T20:00:00Z",
            iv=0.20,
            underlying_price=100,
        )

        # spreads = [0.50-0.40, 0.50-0.20] = [0.10, 0.30] → fmean 0.20, pstdev 0.10
        mean, stdev = loaders._select_etf_iv_spread_baseline(
            session,
            etf_ticker="XLK",
            constituents=["AAPL"],
            range_end=_AS_OF,
            baseline_days=_BASELINE_DAYS,
        )
        assert mean == pytest.approx(0.20)
        assert stdev == pytest.approx(0.10)

    def test_multi_constituent_aggregate_is_cross_sectional_and_date_aligned(
        self, session: Session
    ) -> None:
        """Every constituent present at a ``snapshot_ts`` contributes to that
        date's single-name aggregate, paired against the ETF IV at the same
        ``snapshot_ts``. With no volume the per-date aggregate is the simple
        mean, so adding MSFT changes the baseline — the ALP-812 "MSFT dropped"
        flat-concat behavior no longer holds.
        """
        _add_ticker(session, "XLK")
        _add_ticker(session, "AAPL")
        _add_ticker(session, "MSFT")
        # ETF [0.50, 0.50]; AAPL [0.40, 0.20]; MSFT [0.10, 0.15] over two shared dates.
        for ts, iv in (("2026-04-20T20:00:00Z", 0.50), ("2026-04-27T20:00:00Z", 0.50)):
            _add_call(session, underlying="XLK", snapshot_ts=ts, iv=iv, underlying_price=300)
        for ts, iv in (("2026-04-20T20:00:00Z", 0.40), ("2026-04-27T20:00:00Z", 0.20)):
            _add_call(session, underlying="AAPL", snapshot_ts=ts, iv=iv, underlying_price=100)
        for ts, iv in (("2026-04-20T20:00:00Z", 0.10), ("2026-04-27T20:00:00Z", 0.15)):
            _add_call(session, underlying="MSFT", snapshot_ts=ts, iv=iv, underlying_price=200)

        # per-date single-name (simple mean): [mean(0.40,0.10), mean(0.20,0.15)] = [0.25, 0.175]
        # spreads = [0.50-0.25, 0.50-0.175] = [0.25, 0.325] → fmean 0.2875, pstdev 0.0375
        mean, stdev = loaders._select_etf_iv_spread_baseline(
            session,
            etf_ticker="XLK",
            constituents=["AAPL", "MSFT"],
            range_end=_AS_OF,
            baseline_days=_BASELINE_DAYS,
        )
        assert mean == pytest.approx(0.2875)
        assert stdev == pytest.approx(0.0375)

    def test_per_date_aggregate_is_volume_weighted(self, session: Session) -> None:
        """Within each ``snapshot_ts`` the constituents reduce by the same
        ``volume_today``-weighting the point estimate uses, not a simple mean.
        """
        _add_ticker(session, "XLK")
        _add_ticker(session, "AAPL")
        _add_ticker(session, "MSFT")
        for ts in ("2026-04-20T20:00:00Z", "2026-04-27T20:00:00Z"):
            _add_call(session, underlying="XLK", snapshot_ts=ts, iv=0.60, underlying_price=300)
        # AAPL vol 200, MSFT vol 600 on both dates → the weighted mean leans toward MSFT.
        for ts, iv in (("2026-04-20T20:00:00Z", 0.20), ("2026-04-27T20:00:00Z", 0.30)):
            _add_call(
                session, underlying="AAPL", snapshot_ts=ts, iv=iv, underlying_price=100, volume=200
            )
        for ts, iv in (("2026-04-20T20:00:00Z", 0.40), ("2026-04-27T20:00:00Z", 0.50)):
            _add_call(
                session, underlying="MSFT", snapshot_ts=ts, iv=iv, underlying_price=200, volume=600
            )

        # d1 weighted = (0.20*200 + 0.40*600)/800 = 0.35 → spread 0.25
        # d2 weighted = (0.30*200 + 0.50*600)/800 = 0.45 → spread 0.15
        # spreads = [0.25, 0.15] → fmean 0.20, pstdev 0.05 (simple mean would give 0.225/0.05).
        mean, stdev = loaders._select_etf_iv_spread_baseline(
            session,
            etf_ticker="XLK",
            constituents=["AAPL", "MSFT"],
            range_end=_AS_OF,
            baseline_days=_BASELINE_DAYS,
        )
        assert mean == pytest.approx(0.20)
        assert stdev == pytest.approx(0.05)

    def test_date_misaligned_observations_are_excluded(self, session: Session) -> None:
        """Pairing is by matching ``snapshot_ts``: an ETF date with no
        constituent snapshot contributes no spread, and a constituent
        observation on a date absent from the ETF series is excluded entirely.
        """
        _add_ticker(session, "XLK")
        _add_ticker(session, "AAPL")
        _add_ticker(session, "MSFT")
        # ETF on three dates; the third (04-29) has no constituent → no spread.
        for ts in ("2026-04-20T20:00:00Z", "2026-04-27T20:00:00Z", "2026-04-29T20:00:00Z"):
            _add_call(session, underlying="XLK", snapshot_ts=ts, iv=0.50, underlying_price=300)
        for ts, iv in (("2026-04-20T20:00:00Z", 0.40), ("2026-04-27T20:00:00Z", 0.20)):
            _add_call(session, underlying="AAPL", snapshot_ts=ts, iv=iv, underlying_price=100)
        # MSFT: one shared date (04-27) and one ETF-absent date (05-01, iv 0.99 must not leak).
        _add_call(
            session,
            underlying="MSFT",
            snapshot_ts="2026-04-27T20:00:00Z",
            iv=0.10,
            underlying_price=200,
        )
        _add_call(
            session,
            underlying="MSFT",
            snapshot_ts="2026-05-01T20:00:00Z",
            iv=0.99,
            underlying_price=200,
        )

        # d1 (04-20): AAPL only → 0.40 → spread 0.10
        # d2 (04-27): mean(AAPL 0.20, MSFT 0.10) = 0.15 → spread 0.35
        # d3 (04-29): no constituent → skipped; MSFT 05-01 (0.99) absent from ETF → excluded
        # spreads = [0.10, 0.35] → fmean 0.225, pstdev 0.125
        mean, stdev = loaders._select_etf_iv_spread_baseline(
            session,
            etf_ticker="XLK",
            constituents=["AAPL", "MSFT"],
            range_end=_AS_OF,
            baseline_days=_BASELINE_DAYS,
        )
        assert mean == pytest.approx(0.225)
        assert stdev == pytest.approx(0.125)

    def test_zero_zero_when_etf_history_empty(self, session: Session) -> None:
        _add_ticker(session, "XLK")
        _add_ticker(session, "AAPL")
        # Only constituent history; no XLK snapshots.
        _add_call(
            session,
            underlying="AAPL",
            snapshot_ts="2026-04-20T20:00:00Z",
            iv=0.40,
            underlying_price=100,
        )
        _add_call(
            session,
            underlying="AAPL",
            snapshot_ts="2026-04-27T20:00:00Z",
            iv=0.20,
            underlying_price=100,
        )

        assert loaders._select_etf_iv_spread_baseline(
            session,
            etf_ticker="XLK",
            constituents=["AAPL"],
            range_end=_AS_OF,
            baseline_days=_BASELINE_DAYS,
        ) == (0.0, 0.0)

    def test_zero_zero_when_constituent_history_empty(self, session: Session) -> None:
        _add_ticker(session, "XLK")
        _add_ticker(session, "AAPL")
        _add_call(
            session,
            underlying="XLK",
            snapshot_ts="2026-04-20T20:00:00Z",
            iv=0.50,
            underlying_price=300,
        )
        _add_call(
            session,
            underlying="XLK",
            snapshot_ts="2026-04-27T20:00:00Z",
            iv=0.50,
            underlying_price=300,
        )

        assert loaders._select_etf_iv_spread_baseline(
            session,
            etf_ticker="XLK",
            constituents=["AAPL"],
            range_end=_AS_OF,
            baseline_days=_BASELINE_DAYS,
        ) == (0.0, 0.0)

    def test_zero_zero_when_fewer_than_two_paired_points(self, session: Session) -> None:
        _add_ticker(session, "XLK")
        _add_ticker(session, "AAPL")
        # One paired point only → below _MIN_VARIANCE_SAMPLES (2).
        _add_call(
            session,
            underlying="XLK",
            snapshot_ts="2026-04-20T20:00:00Z",
            iv=0.50,
            underlying_price=300,
        )
        _add_call(
            session,
            underlying="AAPL",
            snapshot_ts="2026-04-20T20:00:00Z",
            iv=0.40,
            underlying_price=100,
        )

        assert loaders._select_etf_iv_spread_baseline(
            session,
            etf_ticker="XLK",
            constituents=["AAPL"],
            range_end=_AS_OF,
            baseline_days=_BASELINE_DAYS,
        ) == (0.0, 0.0)


# ---------------------------------------------------------------------------
# load_etf_iv_divergence_inputs
# ---------------------------------------------------------------------------


def _seed_resolved_sector(session: Session) -> None:
    """Seed a sector whose ETF + single-name + baseline all resolve."""
    for ticker in ("AAPL", "MSFT", "XLK"):
        _add_ticker(session, ticker)
    _add_sector(session, ticker="AAPL", sector_etf="XLK")  # only tickers[0] is consulted
    # ETF + single-name IV at as_of.
    _add_call(session, underlying="XLK", snapshot_ts=_AS_OF, iv=0.30, underlying_price=300)
    _add_call(
        session, underlying="AAPL", snapshot_ts=_AS_OF, iv=0.25, underlying_price=100, volume=100
    )
    _add_call(
        session, underlying="MSFT", snapshot_ts=_AS_OF, iv=0.35, underlying_price=200, volume=100
    )
    # Trailing history giving >= 2 paired points with non-zero variance. Only XLK +
    # AAPL history is seeded: the baseline pairs ETF against the per-date cross-sectional
    # single-name aggregate by matching snapshot_ts, and AAPL shares both dates with XLK,
    # so AAPL alone suffices to make stdev > 0 (MSFT has no trailing history, so it
    # contributes nothing here). The per-key emit is what this sector exercises; the
    # baseline math itself is pinned in TestSelectEtfIvSpreadBaseline.
    _add_call(
        session, underlying="XLK", snapshot_ts="2026-04-20T20:00:00Z", iv=0.30, underlying_price=300
    )
    _add_call(
        session, underlying="XLK", snapshot_ts="2026-04-27T20:00:00Z", iv=0.40, underlying_price=300
    )
    _add_call(
        session,
        underlying="AAPL",
        snapshot_ts="2026-04-20T20:00:00Z",
        iv=0.20,
        underlying_price=100,
    )
    _add_call(
        session,
        underlying="AAPL",
        snapshot_ts="2026-04-27T20:00:00Z",
        iv=0.20,
        underlying_price=100,
    )


class TestLoadEtfIvDivergenceInputs:
    def test_emits_one_fully_resolved_entry_per_sector(self, session: Session) -> None:
        _seed_resolved_sector(session)

        out = loaders.load_etf_iv_divergence_inputs(
            session,
            sorted_sector_tickers={"technology": ["AAPL", "MSFT"]},
            as_of=_AS_OF,
            baseline_days=_BASELINE_DAYS,
        )

        assert set(out) == {"technology"}
        entry = out["technology"]
        assert set(entry) == {
            "etf_ticker",
            "etf_iv",
            "single_name_aggregate_iv",
            "spread_baseline_mean",
            "spread_baseline_stdev",
        }
        assert entry["etf_ticker"] == "XLK"
        assert entry["etf_iv"] == pytest.approx(0.30)
        # Equal-volume AAPL(0.25)/MSFT(0.35) → mean 0.30.
        assert entry["single_name_aggregate_iv"] == pytest.approx(0.30)
        assert isinstance(entry["spread_baseline_stdev"], float)
        assert entry["spread_baseline_stdev"] > 0

    def test_omits_sector_with_no_tickers(self, session: Session) -> None:
        out = loaders.load_etf_iv_divergence_inputs(
            session,
            sorted_sector_tickers={"empty": []},
            as_of=_AS_OF,
            baseline_days=_BASELINE_DAYS,
        )
        assert out == {}

    def test_omits_sector_with_no_classification_for_first_ticker(self, session: Session) -> None:
        _add_ticker(session, "AAPL")  # in universe, but no SectorClassification row
        out = loaders.load_etf_iv_divergence_inputs(
            session,
            sorted_sector_tickers={"technology": ["AAPL"]},
            as_of=_AS_OF,
            baseline_days=_BASELINE_DAYS,
        )
        assert out == {}

    def test_omits_sector_with_no_etf_iv_at_as_of(self, session: Session) -> None:
        _add_ticker(session, "AAPL")
        _add_ticker(session, "XLK")
        _add_sector(session, ticker="AAPL", sector_etf="XLK")
        # AAPL has an as_of snapshot, but XLK (the ETF) has none → etf_iv None.
        _add_call(
            session,
            underlying="AAPL",
            snapshot_ts=_AS_OF,
            iv=0.25,
            underlying_price=100,
            volume=100,
        )

        out = loaders.load_etf_iv_divergence_inputs(
            session,
            sorted_sector_tickers={"technology": ["AAPL"]},
            as_of=_AS_OF,
            baseline_days=_BASELINE_DAYS,
        )
        assert out == {}

    def test_omits_sector_with_no_single_name_aggregate(self, session: Session) -> None:
        _add_ticker(session, "AAPL")
        _add_ticker(session, "XLK")
        _add_sector(session, ticker="AAPL", sector_etf="XLK")
        # ETF resolves at as_of, but AAPL has no as_of snapshot → single-name None.
        _add_call(session, underlying="XLK", snapshot_ts=_AS_OF, iv=0.30, underlying_price=300)

        out = loaders.load_etf_iv_divergence_inputs(
            session,
            sorted_sector_tickers={"technology": ["AAPL"]},
            as_of=_AS_OF,
            baseline_days=_BASELINE_DAYS,
        )
        assert out == {}

    def test_omits_sector_with_nonpositive_baseline_stdev(self, session: Session) -> None:
        _add_ticker(session, "AAPL")
        _add_ticker(session, "XLK")
        _add_sector(session, ticker="AAPL", sector_etf="XLK")
        # Only as_of snapshots (no trailing history) → fewer than 2 paired points
        # → baseline (0.0, 0.0) → stdev <= 0 → omitted.
        _add_call(session, underlying="XLK", snapshot_ts=_AS_OF, iv=0.30, underlying_price=300)
        _add_call(
            session,
            underlying="AAPL",
            snapshot_ts=_AS_OF,
            iv=0.25,
            underlying_price=100,
            volume=100,
        )

        out = loaders.load_etf_iv_divergence_inputs(
            session,
            sorted_sector_tickers={"technology": ["AAPL"]},
            as_of=_AS_OF,
            baseline_days=_BASELINE_DAYS,
        )
        assert out == {}
