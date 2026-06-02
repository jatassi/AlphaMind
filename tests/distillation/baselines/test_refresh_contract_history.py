"""Tests for ``refresh_contract_history`` — story 02-distillation-layer/07.

Cover the per-contract prediction-market refresh entry point: append the
newest snapshot, compute ``delta_pp_since_prior`` from the immediately
preceding ``distillation_contract_history`` row, idempotency, fault
injection.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from alphamind.distillation.baselines import refresh_contract_history
from alphamind.distillation.calibration import CalibrationState
from alphamind.persistence.models import (
    DistillationContractHistory,
    PredictionMarketContracts,
    PredictionMarketSnapshots,
)

CONTRACT_HISTORY_MIN_OBSERVATIONS = 1


def _add_contract(session: Session, contract_id: str) -> None:
    session.add(
        PredictionMarketContracts(
            contract_id=contract_id,
            platform="polymarket",
            description=f"Contract {contract_id}",
            category="monetary_policy",
            created_at="2026-04-01T00:00:00Z",
            last_seen_at="2026-04-26T00:00:00Z",
        )
    )


def _add_snapshot(
    session: Session,
    *,
    contract_id: str,
    snapshot_ts: str,
    yes_probability: float,
    liquidity_usd: float = 50_000.0,
) -> None:
    session.add(
        PredictionMarketSnapshots(
            contract_id=contract_id,
            snapshot_ts=snapshot_ts,
            yes_probability=yes_probability,
            volume_24h_usd=100_000.0,
            liquidity_usd=liquidity_usd,
            bid=None,
            ask=None,
            ingested_at=snapshot_ts,
        )
    )


# ---------------------------------------------------------------------------
# Happy path — first refresh produces a row with delta_pp = 0
# ---------------------------------------------------------------------------


class TestRefreshContractHistoryHappyPath:
    def test_first_run_writes_calibrated_row_with_zero_delta(self, session: Session) -> None:
        _add_contract(session, "pm-001")
        _add_snapshot(
            session,
            contract_id="pm-001",
            snapshot_ts="2026-04-25T13:00:00Z",
            yes_probability=0.42,
        )
        session.commit()

        result = refresh_contract_history(
            session,
            contract_scope=("pm-001",),
            as_of="2026-04-25T13:00:00Z",
            min_observations=CONTRACT_HISTORY_MIN_OBSERVATIONS,
        )

        cv = result["pm-001"]
        assert cv.state is CalibrationState.CALIBRATED
        assert cv.value["yes_probability"] == pytest.approx(0.42)
        # First refresh: no prior history row → delta_pp_since_prior is 0.
        assert cv.value["delta_pp_since_prior"] == pytest.approx(0.0)

        row = session.execute(
            select(DistillationContractHistory).where(
                DistillationContractHistory.contract_id == "pm-001",
            )
        ).scalar_one()
        assert row.calibration_state == "calibrated"
        assert row.delta_pp_since_prior == pytest.approx(0.0)

    def test_second_run_computes_delta_against_prior_history_row(self, session: Session) -> None:
        _add_contract(session, "pm-001")
        _add_snapshot(
            session,
            contract_id="pm-001",
            snapshot_ts="2026-04-24T13:00:00Z",
            yes_probability=0.42,
        )
        _add_snapshot(
            session,
            contract_id="pm-001",
            snapshot_ts="2026-04-25T13:00:00Z",
            yes_probability=0.50,
        )
        session.commit()

        # First refresh anchors the prior-history row at 0.42.
        refresh_contract_history(
            session,
            contract_scope=("pm-001",),
            as_of="2026-04-24T13:00:00Z",
            min_observations=CONTRACT_HISTORY_MIN_OBSERVATIONS,
        )
        # Second refresh sees prior row at 0.42, new snapshot at 0.50 →
        # delta_pp_since_prior should be (0.50 - 0.42) * 100 = 8.0 pp.
        result = refresh_contract_history(
            session,
            contract_scope=("pm-001",),
            as_of="2026-04-25T13:00:00Z",
            min_observations=CONTRACT_HISTORY_MIN_OBSERVATIONS,
        )
        cv = result["pm-001"]
        assert cv.value["yes_probability"] == pytest.approx(0.50)
        assert cv.value["delta_pp_since_prior"] == pytest.approx(8.0)


# ---------------------------------------------------------------------------
# Unavailable — no snapshot in scope → state is unavailable, row written
# (per ALP-540: zero observations indicates collector failure, not warm-up).
# ---------------------------------------------------------------------------


class TestRefreshContractHistoryUnavailablePath:
    def test_writes_unavailable_row_when_no_snapshot_yet(self, session: Session) -> None:
        _add_contract(session, "pm-001")
        # No snapshots seeded.
        session.commit()

        result = refresh_contract_history(
            session,
            contract_scope=("pm-001",),
            as_of="2026-04-25T13:00:00Z",
            min_observations=CONTRACT_HISTORY_MIN_OBSERVATIONS,
        )
        cv = result["pm-001"]
        assert cv.state is CalibrationState.UNAVAILABLE
        assert cv.bootstrap_reason is not None

        row = session.execute(
            select(DistillationContractHistory).where(
                DistillationContractHistory.contract_id == "pm-001",
            )
        ).scalar_one()
        assert row.calibration_state == "unavailable"


# ---------------------------------------------------------------------------
# Idempotent re-run
# ---------------------------------------------------------------------------


class TestRefreshContractHistoryIdempotent:
    def test_rerun_does_not_duplicate(self, session: Session) -> None:
        _add_contract(session, "pm-001")
        _add_snapshot(
            session,
            contract_id="pm-001",
            snapshot_ts="2026-04-25T13:00:00Z",
            yes_probability=0.42,
        )
        session.commit()
        kwargs: dict[str, Any] = dict(
            contract_scope=("pm-001",),
            as_of="2026-04-25T13:00:00Z",
            min_observations=CONTRACT_HISTORY_MIN_OBSERVATIONS,
        )
        refresh_contract_history(session, **kwargs)
        refresh_contract_history(session, **kwargs)
        count = session.scalar(select(func.count()).select_from(DistillationContractHistory))
        assert count == 1


# ---------------------------------------------------------------------------
# Fault injection
# ---------------------------------------------------------------------------


class TestRefreshContractHistoryFaultInjection:
    def test_query_failure_rolls_back_and_propagates(self, session: Session) -> None:
        _add_contract(session, "pm-001")
        _add_contract(session, "pm-002")
        _add_snapshot(
            session,
            contract_id="pm-001",
            snapshot_ts="2026-04-25T13:00:00Z",
            yes_probability=0.42,
        )
        _add_snapshot(
            session,
            contract_id="pm-002",
            snapshot_ts="2026-04-25T13:00:00Z",
            yes_probability=0.55,
        )
        session.commit()

        original_execute = session.execute
        call_state = {"snapshot_calls": 0}

        def failing_execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
            stmt_text = str(statement).lower()
            if "from prediction_market_snapshots" in stmt_text:
                call_state["snapshot_calls"] += 1
                if call_state["snapshot_calls"] >= 2:
                    raise RuntimeError("simulated mid-refresh DB failure")
            return original_execute(statement, *args, **kwargs)

        session.execute = failing_execute  # type: ignore[method-assign]
        try:
            with pytest.raises(RuntimeError, match="simulated mid-refresh"):
                refresh_contract_history(
                    session,
                    contract_scope=("pm-001", "pm-002"),
                    as_of="2026-04-25T13:00:00Z",
                    min_observations=CONTRACT_HISTORY_MIN_OBSERVATIONS,
                )
        finally:
            session.execute = original_execute  # type: ignore[method-assign]

        count = session.scalar(select(func.count()).select_from(DistillationContractHistory))
        assert count == 0
