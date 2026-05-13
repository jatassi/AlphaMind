"""Tests for sector and directional exposure rollup (story 05c)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from alphamind._kernel.ids import (
    Symbol,
)
from alphamind.execution.constants import LISTED_OPTION_CONTRACT_MULTIPLIER
from alphamind.portfolio_state.computations.exposure import (
    SectorResolver,
    compute_directional_exposure,
    compute_sector_exposure,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    LocateStatus,
    OptionContractType,
    OptionGreeks,
    OptionsPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
)
from alphamind.portfolio_state.snapshot import DirectionalExposure, SectorExposureEntry
from alphamind.portfolio_state.views.positions import PositionView


def _resolve_tech(_: PositionRecord) -> str | None:
    return "tech"


def _resolve_none(_: PositionRecord) -> str | None:
    return None


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

_NOW = datetime.now(tz=UTC)
_FILL = PositionFill(
    fill_timestamp=_NOW,
    fill_price=150.0,
    fill_quantity=100.0,
    slippage=0.01,
    fees=1.0,
)


def _make_long_equity(
    position_id: str,
    ticker: str,
    delta_adjusted_exposure_usd: float,
    notional: float = 10_000.0,
) -> PositionView:
    record = PositionRecord.model_validate(
        {
            "position_id": position_id,
            "thesis_id": None,
            "bracket_id": None,
            "status": PositionStatus.OPEN,
            "direction": Direction.LONG,
            "entry_timestamp": _NOW,
            "details": EquityPositionDetails(
                ticker=Symbol(ticker),
                share_count=100.0,
                average_cost_basis_per_share=notional / 100.0,
            ),
            "execution_history": (_FILL,),
            "realized_pnl_to_date_usd": None,
            "corporate_action_adjustment_needed": False,
            "parent_position_id": None,
            "origin": None,
        }
    )
    return PositionView(
        record=record,
        current_market_value_usd=notional,
        unrealized_pnl_usd=0.0,
        unrealized_pnl_pct=0.0,
        position_weight_pct=10.0,
        position_age_hours=24.0,
        notional_exposure_usd=notional,
        delta_adjusted_exposure_usd=delta_adjusted_exposure_usd,
        distance_to_target_usd=None,
        distance_to_stop_usd=None,
        risk_reward_at_current=None,
    )


def _make_short_equity(
    position_id: str,
    ticker: str,
    delta_adjusted_exposure_usd: float,
    notional: float = 5_000.0,
) -> PositionView:
    record = PositionRecord.model_validate(
        {
            "position_id": position_id,
            "thesis_id": None,
            "bracket_id": None,
            "status": PositionStatus.OPEN,
            "direction": Direction.SHORT,
            "entry_timestamp": _NOW,
            "details": EquityPositionDetails(
                ticker=Symbol(ticker),
                share_count=100.0,
                average_cost_basis_per_share=notional / 100.0,
                borrow_rate_pct=0.5,
                locate_status=LocateStatus.LOCATED,
                margin_held_usd=1_000.0,
            ),
            "execution_history": (_FILL,),
            "realized_pnl_to_date_usd": None,
            "corporate_action_adjustment_needed": False,
            "parent_position_id": None,
            "origin": None,
        }
    )
    return PositionView(
        record=record,
        current_market_value_usd=notional,
        unrealized_pnl_usd=0.0,
        unrealized_pnl_pct=0.0,
        position_weight_pct=5.0,
        position_age_hours=24.0,
        notional_exposure_usd=notional,
        delta_adjusted_exposure_usd=delta_adjusted_exposure_usd,
        distance_to_target_usd=None,
        distance_to_stop_usd=None,
        risk_reward_at_current=None,
    )


def _make_long_option(
    position_id: str,
    ticker: str,
    delta_adjusted_exposure_usd: float,
    delta: float = -0.4,
) -> PositionView:
    """Build a long put (negative delta) for testing sign-based bucket assignment."""
    greeks = OptionGreeks(delta=delta, gamma=0.05, theta=-0.01, vega=0.3)
    options_details = OptionsPositionDetails(
        underlying_ticker=Symbol(ticker),
        strike_price=200.0,
        expiration_date=date(2025, 12, 31),
        contract_type=OptionContractType.PUT,
        contract_count=2.0,
        contract_multiplier=LISTED_OPTION_CONTRACT_MULTIPLIER,
        premium_paid_per_contract=5.0,
        greeks=greeks,
    )
    record = PositionRecord.model_validate(
        {
            "position_id": position_id,
            "thesis_id": None,
            "bracket_id": None,
            "status": PositionStatus.OPEN,
            "direction": Direction.LONG,
            "entry_timestamp": _NOW,
            "details": options_details,
            "execution_history": (_FILL,),
            "realized_pnl_to_date_usd": None,
            "corporate_action_adjustment_needed": False,
            "parent_position_id": None,
            "origin": None,
        }
    )
    return PositionView(
        record=record,
        current_market_value_usd=1_000.0,
        unrealized_pnl_usd=0.0,
        unrealized_pnl_pct=0.0,
        position_weight_pct=2.0,
        position_age_hours=10.0,
        notional_exposure_usd=8_000.0,
        delta_adjusted_exposure_usd=delta_adjusted_exposure_usd,
        distance_to_target_usd=None,
        distance_to_stop_usd=None,
        risk_reward_at_current=None,
    )


# ---------------------------------------------------------------------------
# SectorResolver type alias
# ---------------------------------------------------------------------------


class TestSectorResolverTypeAlias:
    def test_callable_resolves_to_string_or_none(self) -> None:
        # SectorResolver must be importable and usable as a type
        def my_resolver(pos: PositionRecord) -> str | None:
            return "tech"

        resolver: SectorResolver = my_resolver
        pos = _make_long_equity("POS-001", "NVDA", 10_000.0)
        assert resolver(pos.record) == "tech"


# ---------------------------------------------------------------------------
# compute_sector_exposure
# ---------------------------------------------------------------------------


class TestComputeSectorExposureHappyPath:
    def test_three_positions_two_sectors(self) -> None:
        """NVDA tech long, AMD tech short, JPM financials long."""
        nvda = _make_long_equity("POS-NVDA", "NVDA", 20_000.0)
        amd = _make_short_equity("POS-AMD", "AMD", -5_000.0)  # short: negative dae
        jpm = _make_long_equity("POS-JPM", "JPM", 15_000.0)

        def resolver(pos: PositionRecord) -> str | None:
            ticker_map = {"NVDA": "tech", "AMD": "tech", "JPM": "financials"}
            if isinstance(pos.details, EquityPositionDetails):
                return ticker_map.get(pos.details.ticker)
            return None

        total = 100_000.0
        result = compute_sector_exposure((nvda, amd, jpm), resolver, total)

        assert len(result) == 2
        sector_map = {e.sector: e for e in result}
        assert "tech" in sector_map
        assert "financials" in sector_map

        tech = sector_map["tech"]
        # NVDA is LONG: goes into long bucket; AMD is SHORT: abs(-5000) = 5000 into short
        assert tech.long_delta_adjusted_usd == pytest.approx(20_000.0)
        assert tech.short_delta_adjusted_usd == pytest.approx(5_000.0)
        assert tech.long_pct_of_portfolio == pytest.approx(20.0)
        assert tech.short_pct_of_portfolio == pytest.approx(5.0)
        assert tech.long_short_ratio == pytest.approx(4.0)  # 20000/5000

        fin = sector_map["financials"]
        assert fin.long_delta_adjusted_usd == pytest.approx(15_000.0)
        assert fin.short_delta_adjusted_usd == pytest.approx(0.0)
        assert fin.long_pct_of_portfolio == pytest.approx(15.0)
        assert fin.short_pct_of_portfolio == pytest.approx(0.0)
        assert fin.long_short_ratio is None  # no short positions

    def test_imports_sector_exposure_entry_from_snapshot(self) -> None:
        """SectorExposureEntry comes from snapshot, not redeclared."""
        nvda = _make_long_equity("POS-NVDA", "NVDA", 10_000.0)
        resolver: SectorResolver = _resolve_tech
        result = compute_sector_exposure((nvda,), resolver, 100_000.0)
        assert isinstance(result[0], SectorExposureEntry)


class TestComputeSectorExposureUnclassified:
    def test_none_resolver_aggregates_under_unclassified(self) -> None:
        """Position with resolver returning None -> UNCLASSIFIED."""
        pos = _make_long_equity("POS-UNKNOWN", "XYZ", 3_000.0)
        resolver: SectorResolver = _resolve_none
        result = compute_sector_exposure((pos,), resolver, 50_000.0)

        assert len(result) == 1
        assert result[0].sector == "UNCLASSIFIED"
        assert result[0].long_delta_adjusted_usd == pytest.approx(3_000.0)
        assert result[0].short_delta_adjusted_usd == pytest.approx(0.0)

    def test_unclassified_mixed_with_classified(self) -> None:
        """Mix of classified and unclassified."""
        known = _make_long_equity("POS-NVDA", "NVDA", 10_000.0)
        unknown = _make_long_equity("POS-XYZ", "XYZ", 2_000.0)

        def resolver(pos: PositionRecord) -> str | None:
            if isinstance(pos.details, EquityPositionDetails) and pos.details.ticker == "NVDA":
                return "tech"
            return None

        result = compute_sector_exposure((known, unknown), resolver, 100_000.0)
        sectors = {e.sector for e in result}
        assert "tech" in sectors
        assert "UNCLASSIFIED" in sectors


class TestComputeSectorExposureEmpty:
    def test_empty_portfolio_returns_empty_tuple(self) -> None:
        resolver: SectorResolver = _resolve_tech
        result = compute_sector_exposure((), resolver, 100_000.0)
        assert result == ()


class TestComputeSectorExposureZeroTotal:
    def test_zero_total_returns_zero_percentages(self) -> None:
        pos = _make_long_equity("POS-001", "NVDA", 10_000.0)
        resolver: SectorResolver = _resolve_tech
        result = compute_sector_exposure((pos,), resolver, 0.0)

        assert len(result) == 1
        assert result[0].long_pct_of_portfolio == pytest.approx(0.0)
        assert result[0].short_pct_of_portfolio == pytest.approx(0.0)


class TestComputeSectorExposureNegativeTotal:
    def test_negative_total_raises_value_error(self) -> None:
        pos = _make_long_equity("POS-001", "NVDA", 10_000.0)
        resolver: SectorResolver = _resolve_tech
        with pytest.raises(ValueError, match="total_portfolio_value_usd"):
            compute_sector_exposure((pos,), resolver, -1.0)


class TestComputeSectorExposureUnenriched:
    def test_none_delta_adjusted_raises_with_position_id(self) -> None:
        pos = _make_long_equity("POS-UNENRICHED", "NVDA", 0.0)
        # Bypass pydantic to inject None
        unenriched = pos.model_copy(update={"delta_adjusted_exposure_usd": None})
        resolver: SectorResolver = _resolve_tech
        with pytest.raises(ValueError, match="POS-UNENRICHED"):
            compute_sector_exposure((unenriched,), resolver, 100_000.0)


class TestComputeSectorExposureOrdering:
    def test_entries_ordered_by_sector_label_ascending(self) -> None:
        """Should sort alphabetically: energy < financials < tech."""
        nvda = _make_long_equity("POS-NVDA", "NVDA", 10_000.0)
        jpm = _make_long_equity("POS-JPM", "JPM", 8_000.0)
        xom = _make_long_equity("POS-XOM", "XOM", 6_000.0)

        def resolver(pos: PositionRecord) -> str | None:
            if isinstance(pos.details, EquityPositionDetails):
                return {"NVDA": "tech", "JPM": "financials", "XOM": "energy"}.get(
                    pos.details.ticker
                )
            return None

        result = compute_sector_exposure((nvda, jpm, xom), resolver, 100_000.0)
        assert [e.sector for e in result] == ["energy", "financials", "tech"]


class TestComputeSectorExposureLongShortRatio:
    def test_ratio_none_when_no_short_positions(self) -> None:
        pos = _make_long_equity("POS-001", "NVDA", 10_000.0)
        resolver: SectorResolver = _resolve_tech
        result = compute_sector_exposure((pos,), resolver, 100_000.0)
        assert result[0].long_short_ratio is None

    def test_ratio_none_when_no_long_positions(self) -> None:
        pos = _make_short_equity("POS-001", "AMD", -8_000.0)
        resolver: SectorResolver = _resolve_tech
        result = compute_sector_exposure((pos,), resolver, 100_000.0)
        # short bucket has 8000, long bucket has 0 -> ratio is None (division by zero)
        assert result[0].long_short_ratio is None

    def test_ratio_finite_when_both_long_and_short(self) -> None:
        long_pos = _make_long_equity("POS-LONG", "NVDA", 12_000.0)
        short_pos = _make_short_equity("POS-SHORT", "AMD", -4_000.0)
        resolver: SectorResolver = _resolve_tech
        result = compute_sector_exposure((long_pos, short_pos), resolver, 100_000.0)
        assert result[0].long_short_ratio == pytest.approx(3.0)  # 12000/4000


# ---------------------------------------------------------------------------
# compute_directional_exposure
# ---------------------------------------------------------------------------


class TestComputeDirectionalExposureHappyPath:
    def test_two_long_one_short_position(self) -> None:
        """Two positive-delta positions and one negative-delta position."""
        pos_a = _make_long_equity("POS-A", "NVDA", 20_000.0)  # positive -> long bucket
        pos_b = _make_long_equity("POS-B", "MSFT", 15_000.0)  # positive -> long bucket
        pos_c = _make_short_equity("POS-C", "AMD", -8_000.0)  # negative -> short bucket

        total = 100_000.0
        result = compute_directional_exposure((pos_a, pos_b, pos_c), total)

        assert isinstance(result, DirectionalExposure)
        assert result.total_long_delta_adjusted_usd == pytest.approx(35_000.0)
        assert result.total_short_delta_adjusted_usd == pytest.approx(8_000.0)
        assert result.net_directional_pct_of_portfolio == pytest.approx(
            (35_000.0 - 8_000.0) / total * 100
        )
        assert result.gross_pct_of_portfolio == pytest.approx((35_000.0 + 8_000.0) / total * 100)

    def test_imports_directional_exposure_from_snapshot(self) -> None:
        """DirectionalExposure comes from snapshot, not redeclared."""
        result = compute_directional_exposure((), 100_000.0)
        assert isinstance(result, DirectionalExposure)


class TestComputeDirectionalExposureLongPut:
    def test_long_put_negative_delta_goes_to_short_bucket(self) -> None:
        """Direction.LONG but negative delta_adjusted_exposure_usd -> short bucket."""
        long_put = _make_long_option("POS-PUT", "SPY", -5_000.0, delta=-0.4)
        total = 100_000.0
        result = compute_directional_exposure((long_put,), total)

        # Negative exposure -> short bucket (by sign, not direction)
        assert result.total_long_delta_adjusted_usd == pytest.approx(0.0)
        assert result.total_short_delta_adjusted_usd == pytest.approx(5_000.0)


class TestComputeDirectionalExposureEmptyPortfolio:
    def test_empty_portfolio_returns_all_zeros(self) -> None:
        result = compute_directional_exposure((), 100_000.0)

        assert result.total_long_delta_adjusted_usd == pytest.approx(0.0)
        assert result.total_short_delta_adjusted_usd == pytest.approx(0.0)
        assert result.net_directional_pct_of_portfolio == pytest.approx(0.0)
        assert result.gross_pct_of_portfolio == pytest.approx(0.0)


class TestComputeDirectionalExposureZeroTotal:
    def test_zero_total_returns_zero_percentages(self) -> None:
        pos = _make_long_equity("POS-001", "NVDA", 10_000.0)
        result = compute_directional_exposure((pos,), 0.0)

        assert result.net_directional_pct_of_portfolio == pytest.approx(0.0)
        assert result.gross_pct_of_portfolio == pytest.approx(0.0)


class TestComputeDirectionalExposureNegativeTotal:
    def test_negative_total_raises_value_error(self) -> None:
        pos = _make_long_equity("POS-001", "NVDA", 10_000.0)
        with pytest.raises(ValueError, match="total_portfolio_value_usd"):
            compute_directional_exposure((pos,), -1.0)


class TestComputeDirectionalExposureUnenriched:
    def test_none_delta_adjusted_raises_with_position_id(self) -> None:
        pos = _make_long_equity("POS-UNENRICHED", "NVDA", 0.0)
        unenriched = pos.model_copy(update={"delta_adjusted_exposure_usd": None})
        with pytest.raises(ValueError, match="POS-UNENRICHED"):
            compute_directional_exposure((unenriched,), 100_000.0)


class TestDeterminism:
    def test_sector_exposure_deterministic(self) -> None:
        nvda = _make_long_equity("POS-NVDA", "NVDA", 20_000.0)
        amd = _make_short_equity("POS-AMD", "AMD", -5_000.0)

        def resolver(pos: PositionRecord) -> str | None:
            if isinstance(pos.details, EquityPositionDetails) and pos.details.ticker in (
                "NVDA",
                "AMD",
            ):
                return "tech"
            return None

        total = 100_000.0
        result_a = compute_sector_exposure((nvda, amd), resolver, total)
        result_b = compute_sector_exposure((nvda, amd), resolver, total)
        assert result_a == result_b

    def test_directional_exposure_deterministic(self) -> None:
        nvda = _make_long_equity("POS-NVDA", "NVDA", 20_000.0)
        amd = _make_short_equity("POS-AMD", "AMD", -5_000.0)
        total = 100_000.0
        result_a = compute_directional_exposure((nvda, amd), total)
        result_b = compute_directional_exposure((nvda, amd), total)
        assert result_a == result_b
