"""Tests for paper-evaluation harness fee schedule and regulatory fee computation (ALP-524)."""

from __future__ import annotations

from typing import Literal

import pytest

from alphamind._kernel.money import price
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
        from pydantic import ValidationError

        with pytest.raises((ValidationError, AttributeError)):
            schedule.cat_per_executed_share = 0.999

    def test_fee_schedule_rejects_negative_cat(self) -> None:
        """Non-negative validator rejects negative cat_per_executed_share."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            FeeSchedule(
                cat_per_executed_share=-0.001,
                taf_per_share_sells=0.000145,
                sec_pct_of_notional_sells=0.0000080,
                orf_per_options_contract=0.02188,
                occ_per_options_contract=0.02,
            )

    def test_fee_schedule_has_all_five_rate_keys(self, schedule: FeeSchedule) -> None:
        """FeeSchedule exposes all five rate fields."""
        assert schedule.cat_per_executed_share == 0.000166
        assert schedule.taf_per_share_sells == 0.000145
        assert schedule.sec_pct_of_notional_sells == 0.0000080
        assert schedule.orf_per_options_contract == 0.02188
        assert schedule.occ_per_options_contract == 0.02

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


# ---------------------------------------------------------------------------
# Cycle 3-7: compute_regulatory_fees behaviour
# ---------------------------------------------------------------------------


class TestComputeRegulatoryFees:
    """compute_regulatory_fees dispatch table by instrument type and side."""

    def test_equity_buy_returns_cat_only(self, schedule: FeeSchedule) -> None:
        """EQUITY buy: only CAT applies (no TAF, no SEC)."""
        from decimal import Decimal

        from alphamind.execution.paper_evaluation_harness import compute_regulatory_fees

        result = compute_regulatory_fees(
            instrument_type=InstrumentType.EQUITY,
            side="buy",
            fill_quantity=100,
            fill_price=price("50"),
            schedule=schedule,
        )
        expected = Decimal(str(schedule.cat_per_executed_share)) * Decimal(100)
        assert result == expected

    def test_equity_sell_returns_cat_taf_sec(self, schedule: FeeSchedule) -> None:
        """EQUITY sell: CAT + TAF + SEC all apply."""
        from decimal import Decimal

        from alphamind.execution.paper_evaluation_harness import compute_regulatory_fees

        qty = 100
        px = Decimal(50)
        result = compute_regulatory_fees(
            instrument_type=InstrumentType.EQUITY,
            side="sell",
            fill_quantity=qty,
            fill_price=price("50"),
            schedule=schedule,
        )
        cat = Decimal(str(schedule.cat_per_executed_share)) * Decimal(str(qty))
        taf = Decimal(str(schedule.taf_per_share_sells)) * Decimal(str(qty))
        sec = Decimal(str(schedule.sec_pct_of_notional_sells)) * px * Decimal(str(qty))
        expected = cat + taf + sec
        assert result == expected

    def test_options_buy_returns_cat_orf_occ(self, schedule: FeeSchedule) -> None:
        """OPTIONS buy: CAT (per-share equivalent) + ORF + OCC; no SEC."""
        from decimal import Decimal

        from alphamind.execution.paper_evaluation_harness import compute_regulatory_fees

        qty = 1  # contracts
        result = compute_regulatory_fees(
            instrument_type=InstrumentType.OPTIONS,
            side="buy",
            fill_quantity=qty,
            fill_price=price("2.50"),
            schedule=schedule,
        )
        # CAT on options applies per underlying share = contracts * 100
        cat = Decimal(str(schedule.cat_per_executed_share)) * Decimal(str(qty * 100))
        orf = Decimal(str(schedule.orf_per_options_contract)) * Decimal(str(qty))
        occ = Decimal(str(schedule.occ_per_options_contract)) * Decimal(str(qty))
        expected = cat + orf + occ
        assert result == expected

    def test_options_sell_returns_cat_sec_orf_occ(self, schedule: FeeSchedule) -> None:
        """OPTIONS sell: CAT + SEC + ORF + OCC all apply."""
        from decimal import Decimal

        from alphamind.execution.paper_evaluation_harness import compute_regulatory_fees

        qty = 1  # contracts
        px = Decimal("2.50")
        result = compute_regulatory_fees(
            instrument_type=InstrumentType.OPTIONS,
            side="sell",
            fill_quantity=qty,
            fill_price=price("2.50"),
            schedule=schedule,
        )
        cat = Decimal(str(schedule.cat_per_executed_share)) * Decimal(str(qty * 100))
        # SEC notional for options: fill_price * fill_quantity * 100
        sec = Decimal(str(schedule.sec_pct_of_notional_sells)) * px * Decimal(str(qty * 100))
        orf = Decimal(str(schedule.orf_per_options_contract)) * Decimal(str(qty))
        occ = Decimal(str(schedule.occ_per_options_contract)) * Decimal(str(qty))
        expected = cat + sec + orf + occ
        assert result == expected

    def test_strategy_raises_not_implemented(self, schedule: FeeSchedule) -> None:
        """STRATEGY instrument type raises NotImplementedError."""
        from alphamind.execution.paper_evaluation_harness import compute_regulatory_fees

        with pytest.raises(NotImplementedError):
            compute_regulatory_fees(
                instrument_type=InstrumentType.STRATEGY,
                side="buy",
                fill_quantity=1,
                fill_price=price("10"),
                schedule=schedule,
            )

    def test_return_value_is_money_type(self, schedule: FeeSchedule) -> None:
        """Return value is Decimal-backed Money (non-negative)."""
        from decimal import Decimal

        from alphamind.execution.paper_evaluation_harness import compute_regulatory_fees

        result = compute_regulatory_fees(
            instrument_type=InstrumentType.EQUITY,
            side="buy",
            fill_quantity=100,
            fill_price=price("50"),
            schedule=schedule,
        )
        # Money is a NewType over Decimal, so check the underlying type
        assert isinstance(result, Decimal)
        assert result >= 0

    def test_return_value_non_negative_for_all_cases(self, schedule: FeeSchedule) -> None:
        """All fee computations return non-negative Money."""
        from alphamind.execution.paper_evaluation_harness import compute_regulatory_fees

        cases: list[tuple[InstrumentType, Literal["buy", "sell"]]] = [
            (InstrumentType.EQUITY, "buy"),
            (InstrumentType.EQUITY, "sell"),
            (InstrumentType.OPTIONS, "buy"),
            (InstrumentType.OPTIONS, "sell"),
        ]
        for instrument_type, side in cases:
            result = compute_regulatory_fees(
                instrument_type=instrument_type,
                side=side,
                fill_quantity=1,
                fill_price=price("10"),
                schedule=schedule,
            )
            assert result >= 0, f"negative fee for {instrument_type}, {side}"


# ---------------------------------------------------------------------------
# Cycle 8: __init__.py re-exports
# ---------------------------------------------------------------------------


class TestPackageReexports:
    def test_compute_regulatory_fees_importable_from_package(self) -> None:
        """compute_regulatory_fees is importable from the package top-level."""
        from alphamind.execution.paper_evaluation_harness import compute_regulatory_fees

        assert callable(compute_regulatory_fees)

    def test_compute_regulatory_fees_in_all(self) -> None:
        """compute_regulatory_fees is listed in __all__."""
        import alphamind.execution.paper_evaluation_harness as pkg

        assert "compute_regulatory_fees" in pkg.__all__
