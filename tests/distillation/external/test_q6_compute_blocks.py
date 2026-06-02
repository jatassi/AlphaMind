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
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.output import OutputAudience
from alphamind.distillation.q6_macro import (
    DollarAttributionLabel,
    InflationRegimeLabel,
    YieldCurveRegimeLabel,
    compute_q6_blocks,
)
from alphamind.persistence.models import (
    DistillationCompositeState,
    EventCalendar,
    MacroObservations,
)

from .conftest import _build_distillation_config

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


def _seed_market_liquidity_component_history(
    session: Session,
    *,
    end: datetime,
    series_values: Mapping[str, list[float]],
) -> None:
    """Seed trailing per-component FRED observations strictly before ``end``.

    The loader pulls each component's history as a strictly-trailing
    window (``observation_date < end``) so the percentile rank of the
    current reading is well-defined. Tests pass a list of values per
    series; this helper writes them at daily intervals ending the day
    BEFORE ``end`` so they're all picked up by the trailing window.
    """
    for series_id, values in series_values.items():
        for offset, value in enumerate(values):
            observation_date = (end - timedelta(days=offset + 1)).strftime("%Y-%m-%d")
            session.add(
                MacroObservations(
                    source="FRED",
                    series_id=series_id,
                    observation_date=observation_date,
                    revision_number=0,
                    release_date=observation_date,
                    value=value,
                    units="pct",
                    frequency="d",
                    ingested_at=observation_date + "T00:00:00Z",
                )
            )


class TestMarketLiquidityBoundary:
    """The wrapper preserves story-08c's bottom-10th-percentile alert boundary.

    Per ALP-575, ``composite_value`` is now the sum of per-component
    percentile ranks (0..300) rather than a raw FRED sum. Trailing history
    rows must live on the same normalized scale, and per-component FRED
    history must be present so the percentile ranks are well-defined.
    """

    def test_alert_fires_when_composite_in_bottom_decile(self, session: Session) -> None:
        # Seed prior composite history on the normalized 0..300 scale,
        # climbing 100..159 — any new composite_value below ~106 lands in
        # the bottom decile of that distribution.
        _seed_market_liquidity_history(session, n_rows=60, base_value=100.0, increment=1.0)
        # Seed multi-day per-component FRED history with non-degenerate
        # distributions (zero-variance series collapse percentile_rank to
        # None); current readings sit below the entire history → 0th
        # percentile per component → normalized sum = 0, well below the
        # historical bottom decile.
        _seed_market_liquidity_component_history(
            session,
            end=AS_OF,
            series_values={
                "STLFSI4": [10.0 + i * 0.1 for i in range(30)],
                "BAMLC0A0CM": [10.0 + i * 0.1 for i in range(30)],
                "VIXCLS": [30.0 + i * 0.1 for i in range(30)],
            },
        )
        # Current-day FRED reads (below all trailing history).
        for series_id, value in (("STLFSI4", 1.0), ("BAMLC0A0CM", 1.0), ("VIXCLS", 1.0)):
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
        # composite_value must not degenerate to any single raw input
        # (normalization regression).
        for component_value in ml_block.payload["components"].values():
            assert ml_block.payload["composite_value"] != component_value

    def test_alert_suppressed_above_10th_percentile(self, session: Session) -> None:
        _seed_market_liquidity_history(session, n_rows=60, base_value=100.0, increment=1.0)
        # Seed per-component history with non-degenerate distributions; the
        # current readings rank near the middle of each → normalized sum
        # ~150, well above the 10th percentile.
        _seed_market_liquidity_component_history(
            session,
            end=AS_OF,
            series_values={
                "STLFSI4": [1.0, 2.0, 3.0, 4.0, 5.0] * 6,
                "BAMLC0A0CM": [1.0, 2.0, 3.0, 4.0, 5.0] * 6,
                "VIXCLS": [10.0, 20.0, 30.0, 40.0, 50.0] * 6,
            },
        )
        for series_id, value in (("STLFSI4", 3.0), ("BAMLC0A0CM", 3.0), ("VIXCLS", 30.0)):
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
        # composite_method is the normalized path (every component had history).
        assert ml_block.payload["composite_method"] == "normalized_percentile_sum"


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
            CalibrationState.ACCUMULATING,
            CalibrationState.UNAVAILABLE,
        }

    def test_funding_stress_no_history_emits_unavailable_block(self, session: Session) -> None:
        # No prior composite rows → ``refresh_funding_stress_composite`` sees
        # zero trailing observations. Per ALP-540 the new vocabulary tags
        # zero-observation cases UNAVAILABLE (collector failure / cold start
        # of the persistence row), not ACCUMULATING.
        config = _build_distillation_config()
        blocks = compute_q6_blocks(session, config=config, as_of=AS_OF)
        fs_block = next(b for b in blocks if b.block_id == "q6.funding_stress")
        assert fs_block.calibration_state is CalibrationState.UNAVAILABLE

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
