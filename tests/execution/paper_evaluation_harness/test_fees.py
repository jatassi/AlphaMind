"""Tests for paper-evaluation harness fee schedule and regulatory fee computation (ALP-524)."""

from __future__ import annotations

import pytest

from alphamind._kernel.money import Money, price
from alphamind.config.models.execution import FeeSchedule
from alphamind.portfolio_state.records.positions import InstrumentType


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def schedule() -> FeeSchedule:
    """A FeeSchedule with known rates for deterministic fee calculations."""
    return FeeSchedule(
        cat_per_executed_share=0.000166,
        taf_per_share_sells=0.000145,
        sec_pct_of_notional_sells=0.0000080,
        orf_per_options_contract=0.02188,
        occ_per_options_contract=0.02,
    )


# ---------------------------------------------------------------------------
# Cycle 1: FeeSchedule model
# ---------------------------------------------------------------------------

class TestFeeScheduleModel:
    def test_fee_schedule_is_importable(self) -> None:
        """FeeSchedule is importable from alphamind.config.models.execution."""
        assert FeeSchedule is not None

    def test_fee_schedule_is_frozen(self, schedule: FeeSchedule) -> None:
        """FeeSchedule instances are immutable (frozen Pydantic model)."""
        with pytest.raises(Exception):  # ValidationError or AttributeError
            schedule.cat_per_executed_share = 0.999  # type: ignore[misc]

    def test_fee_schedule_rejects_negative_cat(self) -> None:
        """Non-negative validator rejects negative cat_per_executed_share."""
        with pytest.raises(Exception):
            FeeSchedule(
                cat_per_executed_share=-0.001,
                taf_per_share_sells=0.000145,
                sec_pct_of_notional_sells=0.0000080,
                orf_per_options_contract=0.02188,
                occ_per_options_contract=0.02,
            )

    def test_fee_schedule_accepts_zero_rates(self) -> None:
        """Rates of exactly 0.0 are valid (some fees may be waived)."""
        fs = FeeSchedule(
            cat_per_executed_share=0.0,
            taf_per_share_sells=0.0,
            sec_pct_of_notional_sells=0.0,
            orf_per_options_contract=0.0,
            occ_per_options_contract=0.0,
        )
        assert fs.cat_per_executed_share == 0.0


# ---------------------------------------------------------------------------
# Cycle 2: Config YAML loads with fee_schedule block
# ---------------------------------------------------------------------------

class TestConfigLoading:
    def test_execution_yaml_loads_with_fee_schedule(self) -> None:
        """Loading execution.yaml succeeds and surfaces fee_schedule on PaperHarness."""
        from pathlib import Path

        import yaml

        from alphamind.config.models.execution import ExecutionConfig

        config_path = Path(__file__).parents[3] / "config" / "execution.yaml"
        with config_path.open() as f:
            raw = yaml.safe_load(f)

        config = ExecutionConfig.model_validate(raw)
        fs = config.paper_harness.fee_schedule

        # All five named rate keys present
        assert hasattr(fs, "cat_per_executed_share")
        assert hasattr(fs, "taf_per_share_sells")
        assert hasattr(fs, "sec_pct_of_notional_sells")
        assert hasattr(fs, "orf_per_options_contract")
        assert hasattr(fs, "occ_per_options_contract")

        # All rates are non-negative
        assert fs.cat_per_executed_share >= 0
        assert fs.taf_per_share_sells >= 0
        assert fs.sec_pct_of_notional_sells >= 0
        assert fs.orf_per_options_contract >= 0
        assert fs.occ_per_options_contract >= 0
