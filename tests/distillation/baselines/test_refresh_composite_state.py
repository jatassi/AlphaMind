"""Tests for ``refresh_composite_state`` — story 02-distillation-layer/07.

Cover the funding-stress / market-liquidity composite refresh entry point.
The refresh primitive computes the composite value, percentile against the
trailing 60-day distribution, and alert flag, and appends a new row.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind.distillation.baselines import refresh_composite_state
from alphamind.distillation.calibration import CalibrationState
from alphamind.persistence.models import (
    Base,
    DistillationCompositeState,
)
from alphamind.persistence.session import make_engine, make_session_factory

COMPOSITE_BASELINE_DAYS = 60
COMPOSITE_MIN_OBSERVATIONS = 60
COMPOSITE_PERCENTILE_ALERT_FUNDING = 90
COMPOSITE_PERCENTILE_ALERT_LIQUIDITY = 10


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


def _seed_composite_history(
    session: Session,
    *,
    composite_kind: str,
    n: int,
    base_value: float = 0.0,
    increment: float = 0.01,
) -> None:
    for i in range(n):
        session.add(
            DistillationCompositeState(
                composite_kind=composite_kind,
                as_of=f"2026-02-{(i % 28) + 1:02d}T0{i % 10}:00:00Z",
                composite_value=base_value + i * increment,
                component_breakdown_json=json.dumps({"seed": i}),
                percentile_60d=0.0,
                alert_active=0,
                calibration_state="calibrated",
                ingested_at="2026-02-01T00:00:00Z",
            )
        )


# ---------------------------------------------------------------------------
# Happy path — calibrated funding-stress composite with full history
# ---------------------------------------------------------------------------


class TestRefreshCompositeStateFundingStressCalibrated:
    def test_writes_calibrated_row_with_components_and_percentile(self, session: Session) -> None:
        # Seed 60 prior history rows whose values cluster around 0.1; the new
        # composite value of 0.5 lies above all of them so the percentile
        # reaches the upper-end alert threshold.
        _seed_composite_history(
            session,
            composite_kind="funding_stress",
            n=COMPOSITE_BASELINE_DAYS,
            base_value=0.0,
            increment=0.001,
        )
        session.commit()

        result = refresh_composite_state(
            session,
            composite_kind="funding_stress",
            components={
                "sofr_ois_spread": 0.2,
                "repo_treasury_spread": 0.15,
                "term_repo_premium": 0.10,
                "mmf_flow": 0.05,
            },
            as_of="2026-04-25T00:00:00Z",
            min_observations=COMPOSITE_MIN_OBSERVATIONS,
            alert_percentile=COMPOSITE_PERCENTILE_ALERT_FUNDING,
            alert_direction="upper",
        )

        assert result.state is CalibrationState.CALIBRATED
        # Composite value = sum of components.
        assert result.value["composite_value"] == pytest.approx(0.5)
        # New value 0.5 is well above the seeded 0..0.059 distribution →
        # top percentile fires the upper-direction alert.
        assert result.value["percentile_60d"] >= COMPOSITE_PERCENTILE_ALERT_FUNDING
        assert result.value["alert_active"] is True

        row = session.execute(
            select(DistillationCompositeState).where(
                DistillationCompositeState.composite_kind == "funding_stress",
                DistillationCompositeState.as_of == "2026-04-25T00:00:00Z",
            )
        ).scalar_one()
        components = json.loads(row.component_breakdown_json)
        assert components["sofr_ois_spread"] == pytest.approx(0.2)
        assert components["mmf_flow"] == pytest.approx(0.05)


# ---------------------------------------------------------------------------
# Bootstrap path — too few prior history rows
# ---------------------------------------------------------------------------


class TestRefreshCompositeStateBootstrapPath:
    def test_writes_bootstrap_row_when_history_below_min(self, session: Session) -> None:
        # Only 5 prior rows seeded — well below 60-observation minimum.
        _seed_composite_history(
            session,
            composite_kind="funding_stress",
            n=5,
        )
        session.commit()

        result = refresh_composite_state(
            session,
            composite_kind="funding_stress",
            components={
                "sofr_ois_spread": 0.2,
                "repo_treasury_spread": 0.15,
                "term_repo_premium": 0.10,
                "mmf_flow": 0.05,
            },
            as_of="2026-04-25T00:00:00Z",
            min_observations=COMPOSITE_MIN_OBSERVATIONS,
            alert_percentile=COMPOSITE_PERCENTILE_ALERT_FUNDING,
            alert_direction="upper",
        )

        assert result.state is CalibrationState.ACCUMULATING
        assert result.bootstrap_reason is not None
        assert "funding_stress_min_observations" in result.bootstrap_reason

        row = session.execute(
            select(DistillationCompositeState).where(
                DistillationCompositeState.as_of == "2026-04-25T00:00:00Z",
            )
        ).scalar_one()
        assert row.calibration_state == "accumulating"


# ---------------------------------------------------------------------------
# Idempotent re-run
# ---------------------------------------------------------------------------


class TestRefreshCompositeStateIdempotent:
    def test_rerun_does_not_duplicate(self, session: Session) -> None:
        _seed_composite_history(
            session,
            composite_kind="funding_stress",
            n=COMPOSITE_BASELINE_DAYS,
        )
        session.commit()
        kwargs: dict[str, Any] = dict(
            composite_kind="funding_stress",
            components={
                "sofr_ois_spread": 0.2,
                "repo_treasury_spread": 0.15,
                "term_repo_premium": 0.10,
                "mmf_flow": 0.05,
            },
            as_of="2026-04-25T00:00:00Z",
            min_observations=COMPOSITE_MIN_OBSERVATIONS,
            alert_percentile=COMPOSITE_PERCENTILE_ALERT_FUNDING,
            alert_direction="upper",
        )
        refresh_composite_state(session, **kwargs)
        first_count = session.scalar(
            select(func.count())
            .select_from(DistillationCompositeState)
            .where(DistillationCompositeState.as_of == "2026-04-25T00:00:00Z")
        )
        refresh_composite_state(session, **kwargs)
        second_count = session.scalar(
            select(func.count())
            .select_from(DistillationCompositeState)
            .where(DistillationCompositeState.as_of == "2026-04-25T00:00:00Z")
        )
        assert first_count == 1
        assert second_count == 1


# ---------------------------------------------------------------------------
# Market-liquidity composite — alert fires on the lower percentile
# ---------------------------------------------------------------------------


class TestRefreshCompositeStateMarketLiquidity:
    def test_alert_active_when_below_lower_percentile(self, session: Session) -> None:
        _seed_composite_history(
            session,
            composite_kind="market_liquidity",
            n=COMPOSITE_BASELINE_DAYS,
            base_value=10.0,
            increment=0.1,
        )
        session.commit()

        # New value is far below the seeded 10..15.9 range → bottom percentile.
        result = refresh_composite_state(
            session,
            composite_kind="market_liquidity",
            components={
                "spread_score": 1.0,
                "depth_score": 0.5,
                "volume_score": 0.5,
            },
            as_of="2026-04-25T00:00:00Z",
            min_observations=COMPOSITE_MIN_OBSERVATIONS,
            alert_percentile=COMPOSITE_PERCENTILE_ALERT_LIQUIDITY,
            alert_direction="lower",
        )

        assert result.value["percentile_60d"] <= COMPOSITE_PERCENTILE_ALERT_LIQUIDITY
        assert result.value["alert_active"] is True


# ---------------------------------------------------------------------------
# Fault injection
# ---------------------------------------------------------------------------


class TestRefreshCompositeStateFaultInjection:
    def test_query_failure_rolls_back_and_propagates(self, session: Session) -> None:
        _seed_composite_history(
            session,
            composite_kind="funding_stress",
            n=COMPOSITE_BASELINE_DAYS,
        )
        session.commit()
        baseline_count = session.scalar(
            select(func.count()).select_from(DistillationCompositeState)
        )

        original_execute = session.execute

        def failing_execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
            stmt_text = str(statement).lower()
            # Fail on the percentile-window history scan so any partial
            # write would have already happened by then.
            if "from distillation_composite_state" in stmt_text and "select" in stmt_text:
                raise RuntimeError("simulated mid-refresh DB failure")
            return original_execute(statement, *args, **kwargs)

        session.execute = failing_execute  # type: ignore[method-assign]
        try:
            with pytest.raises(RuntimeError, match="simulated mid-refresh"):
                refresh_composite_state(
                    session,
                    composite_kind="funding_stress",
                    components={
                        "sofr_ois_spread": 0.2,
                        "repo_treasury_spread": 0.15,
                        "term_repo_premium": 0.10,
                        "mmf_flow": 0.05,
                    },
                    as_of="2026-04-25T00:00:00Z",
                    min_observations=COMPOSITE_MIN_OBSERVATIONS,
                    alert_percentile=COMPOSITE_PERCENTILE_ALERT_FUNDING,
                    alert_direction="upper",
                )
        finally:
            session.execute = original_execute  # type: ignore[method-assign]

        # No new row was committed.
        count = session.scalar(select(func.count()).select_from(DistillationCompositeState))
        assert count == baseline_count
