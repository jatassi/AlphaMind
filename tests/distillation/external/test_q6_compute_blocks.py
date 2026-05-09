"""Tests for ``compute_q6_blocks`` — the upstream-data wrapper around ``assemble_q6_blocks``.

Story scope: close the q6 row of ``_PHASE_2_PLACEHOLDER_GAPS`` by adding a
single entry point the orchestrator can call. The wrapper queries
``macro_observations`` (FRED yield curve, breakevens, dollar series, funding
proxies) plus ``event_calendar`` / ``earnings_event_details`` (surprise
histories), computes the trailing-window aggregates each classifier needs,
calls the existing q6 classifiers and composite-refresh primitives, and
packages the results via ``assemble_q6_blocks``.

Per ``external.md`` § 4 every q6 block carries
``audience = UNIVERSAL_BROADCAST``: the wrapper preserves that contract
unchanged.

Cold-start tolerance: when required series are missing from
``macro_observations`` the wrapper emits the corresponding block with
``BOOTSTRAP`` (or ``UNAVAILABLE``) calibration rather than blocking.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind.config.models.distillation import (
    AnomalyDetection,
    DistillationConfig,
    LeadLag,
    LeadLagPair,
    NarrativeLag,
    PersistenceWindows,
    PredictionMarket,
    RegimeClassification,
    RegimeTransition,
)
from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.output import OutputAudience
from alphamind.distillation.q6_macro import (
    DollarAttributionLabel,
    InflationRegimeLabel,
    YieldCurveRegimeLabel,
    compute_q6_blocks,
)
from alphamind.persistence.models import (
    Base,
    DistillationCompositeState,
    EventCalendar,
    MacroObservations,
)
from alphamind.persistence.session import make_engine, make_session_factory

# ---------------------------------------------------------------------------
# Engine / session fixtures
# ---------------------------------------------------------------------------


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
# Config builder
# ---------------------------------------------------------------------------


def _build_distillation_config() -> DistillationConfig:
    """Return a ``DistillationConfig`` with the YAML default thresholds."""
    return DistillationConfig(
        anomaly_detection=AnomalyDetection(
            volume_anomaly_sigma=2.5,
            price_move_atr_multiple=1.5,
            options_low_oi_volume_multiple=5.0,
            block_trade_min_shares=10_000,
            block_trade_min_notional_usd=500_000,
            dark_pool_one_sided_window_minutes=60,
            earnings_revision_cluster_count=3,
            earnings_revision_cluster_days=5,
            macro_surprise_percentile=90,
            funding_stress_component_alert_count=2,
            funding_stress_component_percentile=90,
            market_liquidity_alert_percentile=10,
            news_price_divergence_window_hours=12,
            news_price_divergence_min_articles=5,
        ),
        regime_classification=RegimeClassification(
            regime_low_vol_vix_max=14.0,
            regime_normal_vix_min=14.0,
            regime_normal_vix_max=22.0,
            regime_elevated_vix_min=22.0,
            regime_elevated_vix_max=35.0,
            regime_crisis_vix_min=35.0,
            regime_term_structure_backwardation_threshold=0.0,
            regime_vvix_high_percentile=80,
            regime_vvix_low_percentile=30,
        ),
        regime_transition=RegimeTransition(
            regime_transition_confirmed_invocations=2,
            regime_transition_indicator_agreement_min=3,
            regime_skip_emergency_trigger=True,
        ),
        lead_lag=LeadLag(
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
        ),
        narrative_lag=NarrativeLag(
            narrative_lag_correlation_shift_sigma=1.5,
            correlation_breakdown_sigma=3.0,
            narrative_lag_media_silence_hours=12,
        ),
        persistence_windows=PersistenceWindows(
            volume_baseline_days=20,
            atr_baseline_days=14,
            spread_baseline_days=20,
            correlation_short_days=20,
            correlation_long_days=60,
            sentiment_baseline_days=60,
            sentiment_min_observations=30,
            gap_fill_baseline_days=252,
            gap_fill_min_events=30,
            extended_hours_confirmation_days=90,
            extended_hours_min_events=20,
            prediction_market_history_days=30,
            funding_stress_baseline_days=60,
            market_liquidity_baseline_days=60,
        ),
        prediction_market=PredictionMarket(
            prediction_market_delta_pp_threshold=5.0,
            prediction_market_low_liquidity_volume_min_usd=10_000,
            tracked_default_min_volume_24h_usd=5_000,
            tracked_categories={},
        ),
    )


# ---------------------------------------------------------------------------
# Time fixtures
# ---------------------------------------------------------------------------


AS_OF = datetime(2026, 4, 25, 14, 30, tzinfo=UTC)
UNIVERSAL_BROADCAST = frozenset({OutputAudience.UNIVERSAL_BROADCAST})


# ---------------------------------------------------------------------------
# Macro-observations seeders — synthesize the FRED series the wrapper reads
# ---------------------------------------------------------------------------


def _seed_macro_series(
    session: Session,
    *,
    series_id: str,
    end: datetime,
    days: int,
    value: float,
    drift_per_day: float = 0.0,
) -> None:
    """Seed ``days`` daily macro observations ending at ``end`` for ``series_id``.

    ``value`` is the value at ``end``; earlier observations are
    ``value - drift_per_day * offset`` so a strict drift is testable.
    """
    for offset in range(days):
        observation_date = (end - timedelta(days=offset)).strftime("%Y-%m-%d")
        session.add(
            MacroObservations(
                source="FRED",
                series_id=series_id,
                observation_date=observation_date,
                revision_number=0,
                release_date=observation_date,
                value=value - drift_per_day * offset,
                units="pct",
                frequency="d",
                ingested_at=observation_date + "T00:00:00Z",
            )
        )


def _seed_yield_curve_history(session: Session, *, end: datetime, days: int) -> None:
    """Seed all five yield-curve series with values that produce a normal curve."""
    for series_id, value in (
        ("DGS3MO", 3.50),
        ("DGS2", 3.80),
        ("DGS5", 4.20),
        ("DGS10", 4.50),
        ("DGS30", 4.70),
    ):
        _seed_macro_series(session, series_id=series_id, end=end, days=days, value=value)


def _seed_breakeven_history(
    session: Session,
    *,
    end: datetime,
    days: int,
    current_value: float = 2.40,
) -> None:
    """Seed T10YIE breakevens at ``current_value`` with no drift."""
    _seed_macro_series(session, series_id="T10YIE", end=end, days=days, value=current_value)


def _seed_dollar_history(session: Session, *, end: datetime, days: int) -> None:
    """Seed DTWEXBGS values that move modestly day-over-day."""
    for offset in range(days):
        observation_date = (end - timedelta(days=offset)).strftime("%Y-%m-%d")
        # A small zigzag so the per-day return series is non-degenerate.
        value = 100.0 + (offset % 3) * 0.05
        session.add(
            MacroObservations(
                source="FRED",
                series_id="DTWEXBGS",
                observation_date=observation_date,
                revision_number=0,
                release_date=observation_date,
                value=value,
                units="index",
                frequency="d",
                ingested_at=observation_date + "T00:00:00Z",
            )
        )


# ---------------------------------------------------------------------------
# Empty-database tracer bullet
# ---------------------------------------------------------------------------


class TestEmptyDatabase:
    """Empty database returns a list without erroring (cold-start day-zero)."""

    def test_empty_database_returns_list(self, session: Session) -> None:
        config = _build_distillation_config()
        blocks = compute_q6_blocks(session, config=config, as_of=AS_OF)
        assert isinstance(blocks, list)


# ---------------------------------------------------------------------------
# Audience contract — every block is UNIVERSAL_BROADCAST per story 08c
# ---------------------------------------------------------------------------


class TestUniversalBroadcastAudience:
    """Every q6 block emitted by the wrapper carries the universal-broadcast audience.

    Story 08c pinned the audience choice for the full q6 family; the wrapper
    must preserve it unchanged (no sector partitioning of macro context).
    """

    def test_every_emitted_block_is_universal_broadcast(self, session: Session) -> None:
        # Seed enough macro data that at least one block emits.
        end = AS_OF
        _seed_yield_curve_history(session, end=end, days=60)
        _seed_breakeven_history(session, end=end, days=60)
        _seed_dollar_history(session, end=end, days=60)
        session.commit()

        config = _build_distillation_config()
        blocks = compute_q6_blocks(session, config=config, as_of=AS_OF)
        assert len(blocks) > 0
        for block in blocks:
            assert block.audience == UNIVERSAL_BROADCAST


# ---------------------------------------------------------------------------
# Yield-curve regime label — one of the five documented labels.
# ---------------------------------------------------------------------------


class TestYieldCurveLabel:
    """The yield-curve block carries one of the five classifier labels."""

    def test_yield_curve_block_carries_one_of_five_labels(self, session: Session) -> None:
        end = AS_OF
        _seed_yield_curve_history(session, end=end, days=60)
        _seed_breakeven_history(session, end=end, days=60)
        _seed_dollar_history(session, end=end, days=60)
        session.commit()

        config = _build_distillation_config()
        blocks = compute_q6_blocks(session, config=config, as_of=AS_OF)
        yc_block = next(b for b in blocks if b.block_id == "q6.yield_curve_regime")
        valid_labels = {label.value for label in YieldCurveRegimeLabel}
        assert yc_block.payload["label"] in valid_labels


# ---------------------------------------------------------------------------
# Inflation regime label — one of the four documented labels.
# ---------------------------------------------------------------------------


class TestInflationLabel:
    """The inflation block carries one of the four classifier labels."""

    def test_inflation_block_carries_one_of_four_labels(self, session: Session) -> None:
        end = AS_OF
        _seed_yield_curve_history(session, end=end, days=60)
        _seed_breakeven_history(session, end=end, days=120, current_value=2.40)
        _seed_dollar_history(session, end=end, days=60)
        session.commit()

        config = _build_distillation_config()
        blocks = compute_q6_blocks(session, config=config, as_of=AS_OF)
        infl_block = next(b for b in blocks if b.block_id == "q6.inflation_regime")
        valid_labels = {label.value for label in InflationRegimeLabel}
        assert infl_block.payload["label"] in valid_labels


# ---------------------------------------------------------------------------
# Dollar attribution label — one of the three documented labels.
# ---------------------------------------------------------------------------


class TestDollarLabel:
    """The dollar-attribution block carries one of the three classifier labels."""

    def test_dollar_block_carries_one_of_three_labels(self, session: Session) -> None:
        end = AS_OF
        _seed_yield_curve_history(session, end=end, days=60)
        _seed_breakeven_history(session, end=end, days=120)
        _seed_dollar_history(session, end=end, days=60)
        session.commit()

        config = _build_distillation_config()
        blocks = compute_q6_blocks(session, config=config, as_of=AS_OF)
        dollar_block = next(b for b in blocks if b.block_id == "q6.dollar_attribution")
        valid_labels = {label.value for label in DollarAttributionLabel}
        assert dollar_block.payload["label"] in valid_labels


# ---------------------------------------------------------------------------
# Funding-stress composite — 2-of-4 boundary preserved through the wrapper.
# ---------------------------------------------------------------------------


def _seed_funding_stress_component_history(
    session: Session,
    *,
    n_rows: int,
    base_components: dict[str, float],
) -> None:
    """Seed prior funding_stress composite rows so the per-component percentile
    distribution is well-defined when the wrapper computes it.
    """
    for index in range(n_rows):
        # Slight monotonic drift so each component's distribution has variance.
        components = {name: base_components[name] + index * 0.0001 for name in base_components}
        session.add(
            DistillationCompositeState(
                composite_kind="funding_stress",
                as_of=f"2026-02-{(index % 28) + 1:02d}T0{index % 10}:00:00Z",
                composite_value=sum(components.values()),
                component_breakdown_json=json.dumps(components),
                percentile_60d=0.0,
                alert_active=0,
                calibration_state="calibrated",
                ingested_at="2026-02-01T00:00:00Z",
            )
        )


class TestFundingStressBoundary:
    """The wrapper preserves story-08c's 2-of-4-components alert boundary."""

    def test_alert_fires_at_exactly_two_components_above_90th_percentile(
        self, session: Session
    ) -> None:
        # Seed the trailing-history per-component baselines around 0.10 so
        # the per-component 90th percentile sits well below the elevated
        # values the FRED proxy series carry below.
        _seed_funding_stress_component_history(
            session,
            n_rows=60,
            base_components={
                "sofr_ois_spread": 0.10,
                "repo_treasury_spread": 0.10,
                "term_repo_premium": 0.10,
                "mmf_flow": 0.10,
            },
        )
        # Seed FRED series so the wrapper's proxy reads land at known values.
        # SOFR (sofr_ois_spread) and RRPONTSYD (repo_treasury_spread) above
        # the trailing percentile; BAMLH0A0HYM2 (term_repo_premium) and
        # WRMFSL (mmf_flow) below.
        for series_id, value in (
            ("SOFR", 0.50),
            ("RRPONTSYD", 0.50),
            ("BAMLH0A0HYM2", 0.05),
            ("WRMFSL", 0.05),
        ):
            _seed_macro_series(session, series_id=series_id, end=AS_OF, days=1, value=value)
        # The wrapper's other paths still need their inputs to skip blocking.
        _seed_yield_curve_history(session, end=AS_OF, days=60)
        _seed_breakeven_history(session, end=AS_OF, days=120)
        _seed_dollar_history(session, end=AS_OF, days=60)
        session.commit()

        config = _build_distillation_config()
        blocks = compute_q6_blocks(session, config=config, as_of=AS_OF)
        fs_block = next(b for b in blocks if b.block_id == "q6.funding_stress")
        assert fs_block.payload["alert_active"] is True
        assert fs_block.payload["components_above_percentile"] == 2

    def test_alert_suppressed_at_one_component_above_90th_percentile(
        self, session: Session
    ) -> None:
        _seed_funding_stress_component_history(
            session,
            n_rows=60,
            base_components={
                "sofr_ois_spread": 0.10,
                "repo_treasury_spread": 0.10,
                "term_repo_premium": 0.10,
                "mmf_flow": 0.10,
            },
        )
        # Only SOFR is elevated; the other three sit at the baseline.
        for series_id, value in (
            ("SOFR", 0.50),
            ("RRPONTSYD", 0.05),
            ("BAMLH0A0HYM2", 0.05),
            ("WRMFSL", 0.05),
        ):
            _seed_macro_series(session, series_id=series_id, end=AS_OF, days=1, value=value)
        _seed_yield_curve_history(session, end=AS_OF, days=60)
        _seed_breakeven_history(session, end=AS_OF, days=120)
        _seed_dollar_history(session, end=AS_OF, days=60)
        session.commit()

        config = _build_distillation_config()
        blocks = compute_q6_blocks(session, config=config, as_of=AS_OF)
        fs_block = next(b for b in blocks if b.block_id == "q6.funding_stress")
        assert fs_block.payload["alert_active"] is False
        assert fs_block.payload["components_above_percentile"] == 1


# ---------------------------------------------------------------------------
# Market-liquidity composite — bottom-10th-percentile boundary preserved.
# ---------------------------------------------------------------------------


def _seed_market_liquidity_history(
    session: Session,
    *,
    n_rows: int,
    base_value: float,
    increment: float,
) -> None:
    """Seed prior market_liquidity composite rows with a monotonic upward drift."""
    for index in range(n_rows):
        session.add(
            DistillationCompositeState(
                composite_kind="market_liquidity",
                as_of=f"2026-02-{(index % 28) + 1:02d}T0{index % 10}:00:00Z",
                composite_value=base_value + index * increment,
                component_breakdown_json=json.dumps({"seed": index}),
                percentile_60d=0.0,
                alert_active=0,
                calibration_state="calibrated",
                ingested_at="2026-02-01T00:00:00Z",
            )
        )


class TestMarketLiquidityBoundary:
    """The wrapper preserves story-08c's bottom-10th-percentile alert boundary."""

    def test_alert_fires_when_composite_in_bottom_decile(self, session: Session) -> None:
        # Seed history climbing from 100..159 so any composite below 100
        # lands in the bottom percentile.
        _seed_market_liquidity_history(session, n_rows=60, base_value=100.0, increment=1.0)
        # The three FRED proxies all read at low values whose sum is far
        # below the trailing-history floor.
        for series_id, value in (
            ("STLFSI4", 1.0),
            ("BAMLC0A0CM", 1.0),
            ("VIXCLS", 1.0),
        ):
            _seed_macro_series(session, series_id=series_id, end=AS_OF, days=1, value=value)
        _seed_yield_curve_history(session, end=AS_OF, days=60)
        _seed_breakeven_history(session, end=AS_OF, days=120)
        _seed_dollar_history(session, end=AS_OF, days=60)
        session.commit()

        config = _build_distillation_config()
        blocks = compute_q6_blocks(session, config=config, as_of=AS_OF)
        ml_block = next(b for b in blocks if b.block_id == "q6.market_liquidity")
        assert ml_block.payload["alert_active"] is True
        assert ml_block.payload["percentile_60d"] <= 10.0

    def test_alert_suppressed_above_10th_percentile(self, session: Session) -> None:
        _seed_market_liquidity_history(session, n_rows=60, base_value=100.0, increment=1.0)
        # Reads sum to 130, near the median of [100, 159].
        for series_id, value in (
            ("STLFSI4", 50.0),
            ("BAMLC0A0CM", 50.0),
            ("VIXCLS", 30.0),
        ):
            _seed_macro_series(session, series_id=series_id, end=AS_OF, days=1, value=value)
        _seed_yield_curve_history(session, end=AS_OF, days=60)
        _seed_breakeven_history(session, end=AS_OF, days=120)
        _seed_dollar_history(session, end=AS_OF, days=60)
        session.commit()

        config = _build_distillation_config()
        blocks = compute_q6_blocks(session, config=config, as_of=AS_OF)
        ml_block = next(b for b in blocks if b.block_id == "q6.market_liquidity")
        assert ml_block.payload["alert_active"] is False
        assert ml_block.payload["percentile_60d"] > 10.0


# ---------------------------------------------------------------------------
# Macro-surprise anomaly — fires at the 90th percentile boundary.
# ---------------------------------------------------------------------------


def _seed_macro_release_history(
    session: Session,
    *,
    series_id: str,
    event_type: str,
    end: datetime,
    actual_diffs: list[float],
) -> None:
    """Seed an ``event_calendar`` row plus FRED observations whose successive
    differences match ``actual_diffs``.

    The wrapper computes ``actual - prior`` per release as the surprise
    proxy (since macro consensus is not stored universe-wide). We seed
    a monthly cadence ending at ``end`` so each diff lands on a distinct
    observation date.
    """
    n = len(actual_diffs) + 1
    # Walk dates monthly backwards from the end so the most recent diff
    # corresponds to the latest event.
    values: list[float] = [0.0]
    for diff in actual_diffs:
        values.append(values[-1] + diff)
    # Re-base so values are positive and roughly look like a CPI level.
    base = 100.0
    rebased = [v + base for v in values]
    # Now seed observations at monthly intervals.
    for offset, value in enumerate(rebased):
        observation_date = (end - timedelta(days=30 * (n - 1 - offset))).strftime("%Y-%m-%d")
        session.add(
            MacroObservations(
                source="FRED",
                series_id=series_id,
                observation_date=observation_date,
                revision_number=0,
                release_date=observation_date,
                value=value,
                units="index",
                frequency="m",
                ingested_at=observation_date + "T00:00:00Z",
            )
        )
    # Latest event_calendar row marks the most recent release as completed.
    latest_release_date = (end - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
    session.add(
        EventCalendar(
            event_id=f"event-{event_type}-latest",
            event_type=event_type,
            ticker=None,
            scheduled_at=latest_release_date,
            description=f"{event_type} latest",
            status="completed",
            source="test",
            ingested_at=latest_release_date,
            last_updated=latest_release_date,
        )
    )


class TestMacroSurpriseAnomalyBoundary:
    """The wrapper preserves story-08c's 90th-percentile macro-surprise boundary."""

    def test_anomaly_fires_when_diff_lands_at_or_above_90th_percentile(
        self, session: Session
    ) -> None:
        # Construct 100 trailing diffs of 1..100; the latest diff lands on
        # the 90th-percentile boundary (= 90).
        trailing_diffs = [float(i + 1) for i in range(100)]
        latest_surprise_diff = 90.0
        actual_diffs = [*trailing_diffs, latest_surprise_diff]
        _seed_macro_release_history(
            session,
            series_id="CPIAUCSL",
            event_type="cpi_release",
            end=AS_OF,
            actual_diffs=actual_diffs,
        )
        _seed_yield_curve_history(session, end=AS_OF, days=60)
        _seed_breakeven_history(session, end=AS_OF, days=120)
        _seed_dollar_history(session, end=AS_OF, days=60)
        session.commit()

        config = _build_distillation_config()
        blocks = compute_q6_blocks(session, config=config, as_of=AS_OF)
        anomaly_blocks = [b for b in blocks if b.block_id.startswith("q6.macro_surprise_anomaly.")]
        assert any(b.payload["indicator"] == "CPIAUCSL" for b in anomaly_blocks)

    def test_anomaly_suppressed_below_90th_percentile(self, session: Session) -> None:
        # Trailing 100 diffs of 1..100; latest diff is 1.0 — far below the
        # 90th-percentile cut.
        trailing_diffs = [float(i + 1) for i in range(100)]
        latest_surprise_diff = 1.0
        actual_diffs = [*trailing_diffs, latest_surprise_diff]
        _seed_macro_release_history(
            session,
            series_id="CPIAUCSL",
            event_type="cpi_release",
            end=AS_OF,
            actual_diffs=actual_diffs,
        )
        _seed_yield_curve_history(session, end=AS_OF, days=60)
        _seed_breakeven_history(session, end=AS_OF, days=120)
        _seed_dollar_history(session, end=AS_OF, days=60)
        session.commit()

        config = _build_distillation_config()
        blocks = compute_q6_blocks(session, config=config, as_of=AS_OF)
        anomaly_blocks = [b for b in blocks if b.block_id.startswith("q6.macro_surprise_anomaly.")]
        assert not any(b.payload["indicator"] == "CPIAUCSL" for b in anomaly_blocks)


# ---------------------------------------------------------------------------
# Cold-start: missing series produce BOOTSTRAP / UNAVAILABLE rather than crash.
# ---------------------------------------------------------------------------


class TestColdStart:
    """When required FRED series are missing, the wrapper emits bootstrap-tagged
    blocks rather than blocking the orchestrator.
    """

    def test_yield_curve_missing_series_emits_bootstrap_block(self, session: Session) -> None:
        # Database carries some macro data but no DGS series; the yield-curve
        # block must be emitted as BOOTSTRAP rather than omitted or raising.
        _seed_breakeven_history(session, end=AS_OF, days=120)
        session.commit()

        config = _build_distillation_config()
        blocks = compute_q6_blocks(session, config=config, as_of=AS_OF)
        yc_block = next(b for b in blocks if b.block_id == "q6.yield_curve_regime")
        assert yc_block.calibration_state in {
            CalibrationState.BOOTSTRAP,
            CalibrationState.UNAVAILABLE,
        }

    def test_funding_stress_no_history_emits_bootstrap_block(self, session: Session) -> None:
        # No prior composite rows → ``refresh_funding_stress_composite``
        # tags the result BOOTSTRAP because trailing history < min_observations.
        config = _build_distillation_config()
        blocks = compute_q6_blocks(session, config=config, as_of=AS_OF)
        fs_block = next(b for b in blocks if b.block_id == "q6.funding_stress")
        assert fs_block.calibration_state is CalibrationState.BOOTSTRAP

    def test_no_series_at_all_does_not_crash(self, session: Session) -> None:
        # Empty database is the absolute cold start. The wrapper still
        # returns a list and the persistence-state composites still emit
        # BOOTSTRAP-tagged blocks.
        config = _build_distillation_config()
        blocks = compute_q6_blocks(session, config=config, as_of=AS_OF)
        assert isinstance(blocks, list)
        # The persistence-state blocks always emit (even cold-start).
        block_ids = {b.block_id for b in blocks}
        assert "q6.funding_stress" in block_ids
        assert "q6.market_liquidity" in block_ids


# ---------------------------------------------------------------------------
# Determinism — two invocations against the same fixture produce identical output.
# ---------------------------------------------------------------------------


def _block_signature(block: object) -> tuple[object, ...]:
    """A hashable, comparison-friendly signature of an :class:`OutputBlock`."""
    # ``OutputBlock`` is a frozen dataclass with a Mapping payload that may
    # be a dict (unhashable). Reduce to a tuple of the load-bearing fields
    # so two blocks compare byte-identical even if their payload mappings
    # are distinct dict instances.
    from alphamind.distillation.output import OutputBlock

    assert isinstance(block, OutputBlock)
    return (
        block.block_id,
        tuple(sorted(a.value for a in block.audience)),
        block.freshness_ts.isoformat(),
        block.calibration_state.value,
        block.bootstrap_reason,
        tuple(sorted(block.payload.items(), key=lambda kv: kv[0])),
        tuple((f.name, f.magnitude, f.severity) for f in block.anomaly_flags),
        block.regime_context,
    )


class TestDeterminism:
    """Two invocations against the same fixture produce identical block lists."""

    def test_two_calls_produce_identical_blocks(self, session: Session) -> None:
        end = AS_OF
        _seed_yield_curve_history(session, end=end, days=60)
        _seed_breakeven_history(session, end=end, days=120)
        _seed_dollar_history(session, end=end, days=60)
        session.commit()

        config = _build_distillation_config()
        first = compute_q6_blocks(session, config=config, as_of=AS_OF)
        second = compute_q6_blocks(session, config=config, as_of=AS_OF)

        assert len(first) == len(second)
        first_signatures = [_block_signature(b) for b in first]
        second_signatures = [_block_signature(b) for b in second]
        assert first_signatures == second_signatures
