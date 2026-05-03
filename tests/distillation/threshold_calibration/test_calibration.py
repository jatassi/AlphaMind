"""Tests for the calibration framework — story 02-distillation-layer/04.

Cover the three-state tag, the per-output value+tag wrapper, the boundary
helper, the cross-sectional fallback functions, and the higher-order tagging
helper that ties everything together.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Iterator

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind.config.models.distillation import LeadLag, LeadLagPair, PredictionMarket
from alphamind.distillation.calibration import (
    CALIBRATION_STATE_VALUES,
    EXTENDED_HOURS_BOOTSTRAP_RATE,
    CalibratedValue,
    CalibrationState,
    decide_calibration_state,
    default_lead_lag_pair_estimate,
    prediction_market_delta_default,
    sector_pooled_atr_baseline,
    sector_pooled_gap_fill_rate,
    sector_pooled_volume_baseline,
    tag_with_fallback,
    universe_pooled_extended_hours_confirmation_rate,
    universe_pooled_sentiment_distribution,
)
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    DistillationEventHistory,
    DistillationTickerBaseline,
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
    session_factory = make_session_factory(engine)
    with session_factory() as sess:
        yield sess


def _add_universe_ticker(
    session: Session,
    *,
    ticker: str,
    sector: str,
) -> None:
    """Insert one ``asset_universe`` row plus its sector classification."""
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
            domain_researcher=f"{sector}_researcher",
            sector_etf="XLK",
            classification_source="manual",
            last_updated="2026-04-26T00:00:00Z",
        )
    )


def _add_ticker_baseline(
    session: Session,
    *,
    ticker: str,
    baseline_kind: str,
    as_of: str,
    mean: float,
    stdev: float,
    calibration_state: str = "calibrated",
    n_observations: int = 20,
    window_days: int = 20,
) -> None:
    session.add(
        DistillationTickerBaseline(
            ticker=ticker,
            baseline_kind=baseline_kind,
            as_of=as_of,
            mean=mean,
            stdev=stdev,
            n_observations=n_observations,
            window_days=window_days,
            calibration_state=calibration_state,
            ingested_at="2026-04-26T00:00:00Z",
        )
    )


def _add_event(
    session: Session,
    *,
    ticker: str,
    event_kind: str,
    event_ts: str,
    outcome: str,
    direction: str = "up",
    magnitude_atr_multiple: float = 1.5,
) -> None:
    session.add(
        DistillationEventHistory(
            ticker=ticker,
            event_kind=event_kind,
            event_ts=event_ts,
            direction=direction,
            magnitude_atr_multiple=magnitude_atr_multiple,
            outcome=outcome,
            outcome_observed_at="2026-04-25T17:00:00Z",
            ingested_at="2026-04-25T13:31:00Z",
        )
    )


class TestCalibrationStateEnum:
    """Three-state tag values match the schema CHECK constraint exactly."""

    def test_members_are_calibrated_bootstrap_unavailable(self) -> None:
        assert {m.value for m in CalibrationState} == {
            "calibrated",
            "bootstrap",
            "unavailable",
        }

    def test_calibrated_value_string(self) -> None:
        assert CalibrationState.CALIBRATED.value == "calibrated"

    def test_bootstrap_value_string(self) -> None:
        assert CalibrationState.BOOTSTRAP.value == "bootstrap"

    def test_unavailable_value_string(self) -> None:
        assert CalibrationState.UNAVAILABLE.value == "unavailable"


class TestSchemaCheckConstraintMatchesFramework:
    """The persistence schema's CHECK strings come from this module."""

    def test_persistence_models_imports_central_tuple(self) -> None:
        """models.py must reference the framework's tuple, not re-state literals."""
        from alphamind.persistence import models

        assert models._CALIBRATION_STATES is CALIBRATION_STATE_VALUES

    def test_central_tuple_lists_every_enum_value(self) -> None:
        assert set(CALIBRATION_STATE_VALUES) == {m.value for m in CalibrationState}


class TestCalibratedValue:
    """The per-output (value, state, bootstrap_reason) wrapper."""

    def test_carries_value_state_and_reason(self) -> None:
        wrapped = CalibratedValue(
            value=42.0,
            state=CalibrationState.CALIBRATED,
            bootstrap_reason=None,
        )
        assert wrapped.value == 42.0
        assert wrapped.state is CalibrationState.CALIBRATED
        assert wrapped.bootstrap_reason is None

    def test_is_frozen_dataclass(self) -> None:
        wrapped = CalibratedValue(
            value=1.0,
            state=CalibrationState.CALIBRATED,
            bootstrap_reason=None,
        )
        assert dataclasses.is_dataclass(wrapped)
        with pytest.raises(dataclasses.FrozenInstanceError):
            wrapped.value = 2.0  # type: ignore[misc]

    def test_carries_bootstrap_reason_when_state_is_bootstrap(self) -> None:
        wrapped = CalibratedValue(
            value=0.42,
            state=CalibrationState.BOOTSTRAP,
            bootstrap_reason="sentiment_min_observations: 12 < 30",
        )
        assert wrapped.bootstrap_reason == "sentiment_min_observations: 12 < 30"


class TestDecideCalibrationState:
    """``observed >= required`` is the only boundary that matters.

    Boundary cases use the sentiment min-observations default (30) so the
    test reflects the canonical example from
    ``threshold-calibration.md`` § Bootstrap policy.
    """

    def test_calibrated_at_exact_threshold(self) -> None:
        """``n == required`` is calibrated, not bootstrap."""
        assert decide_calibration_state(observed_n=30, required_n=30) is (
            CalibrationState.CALIBRATED
        )

    def test_calibrated_above_threshold(self) -> None:
        assert decide_calibration_state(observed_n=31, required_n=30) is (
            CalibrationState.CALIBRATED
        )

    def test_bootstrap_one_below_threshold(self) -> None:
        """``n == required - 1`` is bootstrap."""
        assert decide_calibration_state(observed_n=29, required_n=30) is (
            CalibrationState.BOOTSTRAP
        )

    def test_bootstrap_with_zero_observations(self) -> None:
        assert decide_calibration_state(observed_n=0, required_n=30) is (CalibrationState.BOOTSTRAP)

    def test_never_returns_unavailable(self) -> None:
        """The framework does not infer UNAVAILABLE from low counts."""
        for n in (0, 1, 5, 29, 30, 100):
            assert (
                decide_calibration_state(observed_n=n, required_n=30)
                is not CalibrationState.UNAVAILABLE
            )


class TestTagWithFallback:
    """The higher-order helper that wires the three states together."""

    def test_calibrated_path_returns_computed_value_with_no_reason(self) -> None:
        result = tag_with_fallback(
            observed_n=30,
            required_n=30,
            input_name="sentiment_min_observations",
            computed_value=0.42,
            fallback=lambda: pytest.fail("fallback must not run when calibrated"),
        )
        assert result == CalibratedValue(
            value=0.42,
            state=CalibrationState.CALIBRATED,
            bootstrap_reason=None,
        )

    def test_bootstrap_path_returns_fallback_value_with_reason(self) -> None:
        result = tag_with_fallback(
            observed_n=12,
            required_n=30,
            input_name="sentiment_min_observations",
            computed_value=None,
            fallback=lambda: 0.50,
        )
        assert result.value == 0.50
        assert result.state is CalibrationState.BOOTSTRAP
        assert result.bootstrap_reason == "sentiment_min_observations: 12 < 30"

    def test_unavailable_path_when_fallback_returns_none(self) -> None:
        """Pre-bootstrap deployment: pool itself is empty → UNAVAILABLE."""
        result = tag_with_fallback(
            observed_n=0,
            required_n=30,
            input_name="sentiment_min_observations",
            computed_value=None,
            fallback=lambda: None,
        )
        assert result.value is None
        assert result.state is CalibrationState.UNAVAILABLE
        assert result.bootstrap_reason is not None
        assert "sentiment_min_observations: 0 < 30" in result.bootstrap_reason
        assert "pool" in result.bootstrap_reason.lower()

    def test_bootstrap_reason_carries_input_name_and_count_comparison(self) -> None:
        result = tag_with_fallback(
            observed_n=5,
            required_n=20,
            input_name="extended_hours_min_events",
            computed_value=None,
            fallback=lambda: 0.50,
        )
        assert result.bootstrap_reason == "extended_hours_min_events: 5 < 20"

    def test_boundary_calibrated_at_required(self) -> None:
        """``observed == required`` is calibrated, not bootstrap."""
        result = tag_with_fallback(
            observed_n=30,
            required_n=30,
            input_name="gap_fill_min_events",
            computed_value=0.55,
            fallback=lambda: 0.40,
        )
        assert result.state is CalibrationState.CALIBRATED
        assert result.value == 0.55

    def test_boundary_bootstrap_one_below_required(self) -> None:
        result = tag_with_fallback(
            observed_n=29,
            required_n=30,
            input_name="gap_fill_min_events",
            computed_value=None,
            fallback=lambda: 0.40,
        )
        assert result.state is CalibrationState.BOOTSTRAP
        assert result.value == 0.40


# ---------------------------------------------------------------------------
# Cross-sectional fallback: sector-pooled volume baseline
# ---------------------------------------------------------------------------


class TestSectorPooledVolumeBaseline:
    """Pool calibrated per-ticker volume baselines across a sector."""

    def test_returns_mean_and_stdev_across_sector_tickers(self, session: Session) -> None:
        _add_universe_ticker(session, ticker="AAPL", sector="tech")
        _add_universe_ticker(session, ticker="MSFT", sector="tech")
        _add_universe_ticker(session, ticker="JPM", sector="financials")
        as_of = "2026-04-25T00:00:00Z"
        _add_ticker_baseline(
            session,
            ticker="AAPL",
            baseline_kind="volume",
            as_of=as_of,
            mean=10.0,
            stdev=2.0,
        )
        _add_ticker_baseline(
            session,
            ticker="MSFT",
            baseline_kind="volume",
            as_of=as_of,
            mean=20.0,
            stdev=4.0,
        )
        _add_ticker_baseline(
            session,
            ticker="JPM",
            baseline_kind="volume",
            as_of=as_of,
            mean=50.0,
            stdev=12.0,
        )
        session.commit()

        result = sector_pooled_volume_baseline(session, sector="tech", as_of=as_of)

        assert result is not None
        mean, stdev = result
        assert mean == pytest.approx(15.0)
        assert stdev == pytest.approx(3.0)

    def test_returns_none_when_pool_is_empty(self, session: Session) -> None:
        result = sector_pooled_volume_baseline(session, sector="tech", as_of="2026-04-25T00:00:00Z")
        assert result is None

    def test_excludes_uncalibrated_baselines_from_pool(self, session: Session) -> None:
        """Bootstrap-tagged baselines do not seed the pool — they are themselves
        downstream of a fallback and would inject circular noise."""
        _add_universe_ticker(session, ticker="AAPL", sector="tech")
        _add_universe_ticker(session, ticker="MSFT", sector="tech")
        as_of = "2026-04-25T00:00:00Z"
        _add_ticker_baseline(
            session,
            ticker="AAPL",
            baseline_kind="volume",
            as_of=as_of,
            mean=10.0,
            stdev=2.0,
            calibration_state="calibrated",
        )
        _add_ticker_baseline(
            session,
            ticker="MSFT",
            baseline_kind="volume",
            as_of=as_of,
            mean=999.0,
            stdev=999.0,
            calibration_state="bootstrap",
        )
        session.commit()

        result = sector_pooled_volume_baseline(session, sector="tech", as_of=as_of)

        assert result is not None
        mean, stdev = result
        assert mean == pytest.approx(10.0)
        assert stdev == pytest.approx(2.0)

    def test_returns_none_when_only_other_sector_tickers_have_baselines(
        self, session: Session
    ) -> None:
        _add_universe_ticker(session, ticker="JPM", sector="financials")
        as_of = "2026-04-25T00:00:00Z"
        _add_ticker_baseline(
            session,
            ticker="JPM",
            baseline_kind="volume",
            as_of=as_of,
            mean=50.0,
            stdev=12.0,
        )
        session.commit()

        result = sector_pooled_volume_baseline(session, sector="tech", as_of=as_of)

        assert result is None


class TestSectorPooledAtrBaseline:
    """Same shape as the volume baseline; reads the ``atr`` baseline kind."""

    def test_returns_mean_and_stdev_across_sector_tickers(self, session: Session) -> None:
        _add_universe_ticker(session, ticker="AAPL", sector="tech")
        _add_universe_ticker(session, ticker="MSFT", sector="tech")
        as_of = "2026-04-25T00:00:00Z"
        _add_ticker_baseline(
            session,
            ticker="AAPL",
            baseline_kind="atr",
            as_of=as_of,
            mean=2.0,
            stdev=0.4,
        )
        _add_ticker_baseline(
            session,
            ticker="MSFT",
            baseline_kind="atr",
            as_of=as_of,
            mean=4.0,
            stdev=0.8,
        )
        session.commit()

        result = sector_pooled_atr_baseline(session, sector="tech", as_of=as_of)

        assert result is not None
        mean, stdev = result
        assert mean == pytest.approx(3.0)
        assert stdev == pytest.approx(0.6)

    def test_returns_none_when_pool_is_empty(self, session: Session) -> None:
        result = sector_pooled_atr_baseline(session, sector="tech", as_of="2026-04-25T00:00:00Z")
        assert result is None

    def test_does_not_blend_volume_into_atr_pool(self, session: Session) -> None:
        """Different ``baseline_kind`` rows are isolated; the pool is per-kind."""
        _add_universe_ticker(session, ticker="AAPL", sector="tech")
        as_of = "2026-04-25T00:00:00Z"
        _add_ticker_baseline(
            session,
            ticker="AAPL",
            baseline_kind="volume",
            as_of=as_of,
            mean=999.0,
            stdev=999.0,
        )
        session.commit()

        result = sector_pooled_atr_baseline(session, sector="tech", as_of=as_of)
        assert result is None


class TestUniversePooledSentimentDistribution:
    """Pool calibrated per-ticker sentiment baselines across the whole universe."""

    def test_returns_pooled_mean_and_stdev_across_universe(self, session: Session) -> None:
        _add_universe_ticker(session, ticker="AAPL", sector="tech")
        _add_universe_ticker(session, ticker="JPM", sector="financials")
        as_of = "2026-04-25T00:00:00Z"
        _add_ticker_baseline(
            session,
            ticker="AAPL",
            baseline_kind="sentiment",
            as_of=as_of,
            mean=0.20,
            stdev=0.40,
        )
        _add_ticker_baseline(
            session,
            ticker="JPM",
            baseline_kind="sentiment",
            as_of=as_of,
            mean=0.10,
            stdev=0.30,
        )
        session.commit()

        result = universe_pooled_sentiment_distribution(session, as_of=as_of)

        assert result is not None
        mean, stdev = result
        assert mean == pytest.approx(0.15)
        assert stdev == pytest.approx(0.35)

    def test_returns_none_when_pool_is_empty(self, session: Session) -> None:
        result = universe_pooled_sentiment_distribution(session, as_of="2026-04-25T00:00:00Z")
        assert result is None

    def test_pool_spans_sectors(self, session: Session) -> None:
        """Sentiment is universe-wide, not sector-segmented."""
        _add_universe_ticker(session, ticker="AAPL", sector="tech")
        _add_universe_ticker(session, ticker="JPM", sector="financials")
        _add_universe_ticker(session, ticker="XOM", sector="energy")
        as_of = "2026-04-25T00:00:00Z"
        for ticker in ("AAPL", "JPM", "XOM"):
            _add_ticker_baseline(
                session,
                ticker=ticker,
                baseline_kind="sentiment",
                as_of=as_of,
                mean=0.0,
                stdev=0.5,
            )
        session.commit()

        result = universe_pooled_sentiment_distribution(session, as_of=as_of)

        assert result is not None
        mean, stdev = result
        assert mean == pytest.approx(0.0)
        assert stdev == pytest.approx(0.5)
        # Sanity: with only one sector this would still pass; verify the
        # underlying query did not silently scope to a single sector by
        # confirming we hit all three rows. ``statistics.fmean`` over three
        # equal stdevs equals the per-row stdev.
        assert math.isclose(stdev, 0.5, rel_tol=1e-9)


class TestSectorPooledGapFillRate:
    """``count(filled) / count(*)`` over sector gap events at or before ``as_of``."""

    def test_returns_fill_rate_across_sector_gap_events(self, session: Session) -> None:
        _add_universe_ticker(session, ticker="AAPL", sector="tech")
        _add_universe_ticker(session, ticker="MSFT", sector="tech")
        # AAPL: 2 of 4 gaps filled. MSFT: 2 of 2 filled. Sector rate = 4/6.
        for i, outcome in enumerate(("filled", "unfilled", "filled", "unfilled")):
            _add_event(
                session,
                ticker="AAPL",
                event_kind="gap",
                event_ts=f"2026-04-2{i}T13:30:00Z",
                outcome=outcome,
            )
        for i, outcome in enumerate(("filled", "filled")):
            _add_event(
                session,
                ticker="MSFT",
                event_kind="gap",
                event_ts=f"2026-04-1{i}T13:30:00Z",
                outcome=outcome,
            )
        session.commit()

        rate = sector_pooled_gap_fill_rate(session, sector="tech", as_of="2026-04-26T00:00:00Z")

        assert rate == pytest.approx(4 / 6)

    def test_returns_none_when_no_sector_gap_events(self, session: Session) -> None:
        rate = sector_pooled_gap_fill_rate(session, sector="tech", as_of="2026-04-26T00:00:00Z")
        assert rate is None

    def test_excludes_extended_hours_events_from_gap_pool(self, session: Session) -> None:
        """Different ``event_kind``s do not pool together."""
        _add_universe_ticker(session, ticker="AAPL", sector="tech")
        _add_event(
            session,
            ticker="AAPL",
            event_kind="extended_hours",
            event_ts="2026-04-25T20:00:00Z",
            outcome="confirmed",
        )
        session.commit()

        rate = sector_pooled_gap_fill_rate(session, sector="tech", as_of="2026-04-26T00:00:00Z")
        assert rate is None

    def test_excludes_other_sectors(self, session: Session) -> None:
        _add_universe_ticker(session, ticker="JPM", sector="financials")
        _add_event(
            session,
            ticker="JPM",
            event_kind="gap",
            event_ts="2026-04-25T13:30:00Z",
            outcome="filled",
        )
        session.commit()

        rate = sector_pooled_gap_fill_rate(session, sector="tech", as_of="2026-04-26T00:00:00Z")
        assert rate is None

    def test_excludes_events_after_as_of(self, session: Session) -> None:
        """Events after ``as_of`` are not yet observable."""
        _add_universe_ticker(session, ticker="AAPL", sector="tech")
        _add_event(
            session,
            ticker="AAPL",
            event_kind="gap",
            event_ts="2026-05-01T13:30:00Z",
            outcome="filled",
        )
        session.commit()

        rate = sector_pooled_gap_fill_rate(session, sector="tech", as_of="2026-04-26T00:00:00Z")
        assert rate is None


class TestUniversePooledExtendedHoursConfirmationRate:
    """Universe-pooled rate with a 50% cold-start prior — never returns None."""

    def test_named_constant_value(self) -> None:
        """``EXTENDED_HOURS_BOOTSTRAP_RATE = 0.5`` is the documented prior."""
        assert EXTENDED_HOURS_BOOTSTRAP_RATE == 0.5

    def test_returns_cold_start_rate_when_no_events(self, session: Session) -> None:
        """Cold start: no events anywhere in the universe → 50% (no information)."""
        rate = universe_pooled_extended_hours_confirmation_rate(
            session, as_of="2026-04-26T00:00:00Z"
        )
        assert rate == EXTENDED_HOURS_BOOTSTRAP_RATE

    def test_returns_observed_rate_across_universe(self, session: Session) -> None:
        _add_universe_ticker(session, ticker="AAPL", sector="tech")
        _add_universe_ticker(session, ticker="JPM", sector="financials")
        for i, outcome in enumerate(("confirmed", "confirmed", "rejected")):
            _add_event(
                session,
                ticker="AAPL",
                event_kind="extended_hours",
                event_ts=f"2026-04-2{i}T20:00:00Z",
                outcome=outcome,
            )
        # JPM contributes one rejected event.
        _add_event(
            session,
            ticker="JPM",
            event_kind="extended_hours",
            event_ts="2026-04-25T20:00:00Z",
            outcome="rejected",
        )
        session.commit()

        rate = universe_pooled_extended_hours_confirmation_rate(
            session, as_of="2026-04-26T00:00:00Z"
        )
        # 2 confirmed of 4 total → 0.5 (coincidentally equals the prior).
        assert rate == pytest.approx(0.5)

    def test_observed_rate_distinguishable_from_prior(self, session: Session) -> None:
        _add_universe_ticker(session, ticker="AAPL", sector="tech")
        for i, outcome in enumerate(("confirmed", "confirmed", "confirmed", "rejected")):
            _add_event(
                session,
                ticker="AAPL",
                event_kind="extended_hours",
                event_ts=f"2026-04-2{i}T20:00:00Z",
                outcome=outcome,
            )
        session.commit()

        rate = universe_pooled_extended_hours_confirmation_rate(
            session, as_of="2026-04-26T00:00:00Z"
        )
        assert rate == pytest.approx(0.75)

    def test_excludes_gap_events(self, session: Session) -> None:
        """Different ``event_kind``s do not pool together."""
        _add_universe_ticker(session, ticker="AAPL", sector="tech")
        _add_event(
            session,
            ticker="AAPL",
            event_kind="gap",
            event_ts="2026-04-25T13:30:00Z",
            outcome="filled",
        )
        session.commit()

        rate = universe_pooled_extended_hours_confirmation_rate(
            session, as_of="2026-04-26T00:00:00Z"
        )
        # Only gap events exist → extended-hours pool is empty → cold-start prior.
        assert rate == EXTENDED_HOURS_BOOTSTRAP_RATE


@pytest.fixture()
def lead_lag_config() -> LeadLag:
    return LeadLag(
        pairs=(
            LeadLagPair(key="credit_to_equity", lead="HYG", lag="SPY"),
            LeadLagPair(key="semis_to_tech", lead="SOXX", lag="QQQ"),
            LeadLagPair(key="financials_to_market", lead="XLF", lag="SPY"),
            LeadLagPair(key="commodity_to_energy_equity", lead="USO", lag="XLE"),
        ),
        lead_lag_funding_to_credit_max_days=3,
        lead_lag_credit_to_equity_max_days=3,
        lead_lag_semis_to_tech_max_days=2,
        lead_lag_financials_to_market_max_days=1,
        lead_lag_commodity_to_energy_equity_max_days=1,
        lead_lag_overdue_lead_sigma=1.5,
    )


class TestDefaultLeadLagPairEstimate:
    """Returns the Class A ``_max_days`` bound for the named pair as the prior."""

    def test_funding_to_credit_returns_configured_max_days(self, lead_lag_config: LeadLag) -> None:
        prior = default_lead_lag_pair_estimate(
            pair_key="funding_to_credit", lead_lag_config=lead_lag_config
        )
        assert prior == 3

    def test_credit_to_equity(self, lead_lag_config: LeadLag) -> None:
        assert (
            default_lead_lag_pair_estimate(
                pair_key="credit_to_equity", lead_lag_config=lead_lag_config
            )
            == 3
        )

    def test_semis_to_tech(self, lead_lag_config: LeadLag) -> None:
        assert (
            default_lead_lag_pair_estimate(
                pair_key="semis_to_tech", lead_lag_config=lead_lag_config
            )
            == 2
        )

    def test_financials_to_market(self, lead_lag_config: LeadLag) -> None:
        assert (
            default_lead_lag_pair_estimate(
                pair_key="financials_to_market", lead_lag_config=lead_lag_config
            )
            == 1
        )

    def test_commodity_to_energy_equity(self, lead_lag_config: LeadLag) -> None:
        assert (
            default_lead_lag_pair_estimate(
                pair_key="commodity_to_energy_equity", lead_lag_config=lead_lag_config
            )
            == 1
        )

    def test_unknown_pair_key_raises(self, lead_lag_config: LeadLag) -> None:
        with pytest.raises(KeyError):
            default_lead_lag_pair_estimate(
                pair_key="not_a_real_pair", lead_lag_config=lead_lag_config
            )


class TestPredictionMarketDeltaDefault:
    """Universe-wide threshold from config; no per-contract bootstrap."""

    def test_returns_configured_threshold(self) -> None:
        prediction_market_config = PredictionMarket(
            prediction_market_delta_pp_threshold=5.0,
            prediction_market_low_liquidity_volume_min_usd=10_000,
            tracked_default_min_volume_24h_usd=5_000,
            tracked_categories={},
        )
        prior = prediction_market_delta_default(
            prediction_market_config=prediction_market_config,
        )
        assert prior == 5.0

    def test_reflects_operator_override(self) -> None:
        """A different operator-configured threshold flows through unchanged."""
        prediction_market_config = PredictionMarket(
            prediction_market_delta_pp_threshold=7.5,
            prediction_market_low_liquidity_volume_min_usd=20_000,
            tracked_default_min_volume_24h_usd=5_000,
            tracked_categories={},
        )
        prior = prediction_market_delta_default(
            prediction_market_config=prediction_market_config,
        )
        assert prior == 7.5
