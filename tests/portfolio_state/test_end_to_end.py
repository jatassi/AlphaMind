"""End-to-end integration test suite for the portfolio state pipeline (story 09).

Exercises the full path from assemble_snapshot → compute_snapshot_freshness →
all four per-consumer view projections, using only in-tree stubs.

Sections:
  A — Smoke tests against the five fixture builders
  B — Per-consumer projection consistency (multi-position fixture)
  C — Assembler correctness against known formulas
  D — Freshness reporting (stale / unknown / fresh / phase1 boundary)
  E — Determinism
  F — Empty / degenerate edge cases
  G — Repository error propagation
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import NoReturn

import pytest

from alphamind._kernel.money import money, signed_money
from alphamind.portfolio_state import PortfolioStateConfig
from alphamind.portfolio_state.aggregates.thesis_quality import ThesisQualityAggregate
from alphamind.portfolio_state.assembler import assemble_snapshot
from alphamind.portfolio_state.computations.exposure import SectorResolver
from alphamind.portfolio_state.consumers.analyst import project_analyst_view
from alphamind.portfolio_state.consumers.portfolio_manager import project_portfolio_manager_view
from alphamind.portfolio_state.consumers.strategist import project_strategist_view
from alphamind.portfolio_state.consumers.synthesizer import project_synthesizer_view
from alphamind.portfolio_state.freshness import AssembledSnapshot
from alphamind.portfolio_state.pricing import (
    PriceQuote,
    StubCurrentPriceProvider,
    StubOptionPriceProvider,
)
from alphamind.portfolio_state.repository import (
    RepositoryConsistencyError,
    RepositoryFixture,
    RepositoryReadError,
    StubPortfolioStateRepository,
)
from alphamind.portfolio_state.snapshot import PortfolioStateSnapshot
from alphamind.portfolio_state.views.positions import PositionView
from tests.portfolio_state._fixtures import (
    _INV_ID,
    _make_base_fixture,
    _make_cash_ledger,
    _make_config,
    _make_invocation_metadata,
    _make_sector_resolver,
    build_minimal_snapshot_inputs,
    build_multi_position_snapshot_inputs,
    build_options_position_snapshot_inputs,
    build_stale_pricing_snapshot_inputs,
    build_unknown_ticker_snapshot_inputs,
)

# Type alias for the fixture tuple returned by each builder.
_FixtureInputs = tuple[
    RepositoryFixture, dict[str, PriceQuote], SectorResolver, PortfolioStateConfig, datetime
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assemble(fixture_inputs: _FixtureInputs) -> AssembledSnapshot:
    """Construct stubs from fixture tuple and call assemble_snapshot."""
    repo_fixture, quotes, sector_resolver, config, now = fixture_inputs
    repo = StubPortfolioStateRepository(repo_fixture)
    price_provider = StubCurrentPriceProvider(quotes, now)
    option_price_provider = StubOptionPriceProvider({}, now)
    return assemble_snapshot(
        repository=repo,
        price_provider=price_provider,
        option_price_provider=option_price_provider,
        sector_resolver=sector_resolver,
        config=config,
        now=now,
    )


# ---------------------------------------------------------------------------
# Section A — Smoke tests against each fixture builder
# ---------------------------------------------------------------------------


class TestSectionASmoke:
    """Verify assemble_snapshot succeeds for each of the five fixture builders."""

    def test_minimal_returns_assembled_snapshot(self) -> None:
        result = _assemble(build_minimal_snapshot_inputs())
        assert isinstance(result, AssembledSnapshot)

    def test_minimal_snapshot_is_valid(self) -> None:
        result = _assemble(build_minimal_snapshot_inputs())
        assert isinstance(result.snapshot, PortfolioStateSnapshot)

    def test_minimal_freshness_zero_positions(self) -> None:
        result = _assemble(build_minimal_snapshot_inputs())
        assert result.freshness.total_positions == 0

    def test_minimal_freshness_no_oldest_price(self) -> None:
        result = _assemble(build_minimal_snapshot_inputs())
        assert result.freshness.oldest_price_age_seconds is None

    def test_multi_position_returns_assembled_snapshot(self) -> None:
        result = _assemble(build_multi_position_snapshot_inputs())
        assert isinstance(result, AssembledSnapshot)

    def test_multi_position_snapshot_is_valid(self) -> None:
        result = _assemble(build_multi_position_snapshot_inputs())
        assert isinstance(result.snapshot, PortfolioStateSnapshot)

    def test_multi_position_freshness_four_positions(self) -> None:
        result = _assemble(build_multi_position_snapshot_inputs())
        # 3 open + 1 pending
        assert result.freshness.total_positions == 4

    def test_options_returns_assembled_snapshot(self) -> None:
        result = _assemble(build_options_position_snapshot_inputs())
        assert isinstance(result, AssembledSnapshot)

    def test_stale_pricing_returns_assembled_snapshot(self) -> None:
        result = _assemble(build_stale_pricing_snapshot_inputs())
        assert isinstance(result, AssembledSnapshot)

    def test_stale_pricing_snapshot_is_valid(self) -> None:
        result = _assemble(build_stale_pricing_snapshot_inputs())
        assert isinstance(result.snapshot, PortfolioStateSnapshot)

    def test_unknown_ticker_returns_assembled_snapshot(self) -> None:
        result = _assemble(build_unknown_ticker_snapshot_inputs())
        assert isinstance(result, AssembledSnapshot)

    def test_unknown_ticker_snapshot_is_valid(self) -> None:
        result = _assemble(build_unknown_ticker_snapshot_inputs())
        assert isinstance(result.snapshot, PortfolioStateSnapshot)


# ---------------------------------------------------------------------------
# Section B — Per-consumer projection consistency
# ---------------------------------------------------------------------------


class TestSectionBProjectionConsistency:
    """Cross-view consistency and information-hiding invariants on the multi-position fixture.

    These are the ONLY tests that assert the four consumer projections agree with each
    other and with the snapshot.  test_assembler does not exercise cross-view invariants.
    """

    def setup_method(self) -> None:
        inputs = build_multi_position_snapshot_inputs()
        self._assembled = _assemble(inputs)
        self._snapshot = self._assembled.snapshot
        _, _, self._sector_resolver, _, _ = inputs
        total_value = self._snapshot.cash_ledger.current_cash_usd + sum(
            abs(float(p.current_market_value_usd)) for p in self._snapshot.open_positions
        )
        self._synth_view = project_synthesizer_view(
            self._snapshot, sector_resolver=self._sector_resolver
        )
        self._analyst_view = project_analyst_view(
            self._snapshot,
            sector_resolver=self._sector_resolver,
            per_position_size_rule_id="position_max_size_pct",
            total_portfolio_value_usd=money(total_value),
            available_capital_usd=signed_money(
                self._snapshot.cash_ledger.true_deployable_capital_usd
            ),
        )
        self._strategist_view = project_strategist_view(self._snapshot)
        self._pm_view = project_portfolio_manager_view(self._snapshot)

    def test_position_id_set_equality_across_all_four_views(self) -> None:
        """synth / strat / pm / analyst all see the same position-id set."""
        snap_ids = {p.position_id for p in self._snapshot.open_positions} | {
            p.position_id for p in self._snapshot.pending_positions
        }
        # Synthesizer: one entry per position
        assert len(self._synth_view.positions) == len(snap_ids)
        # Strategist and PM: equal to each other and to snapshot
        strat_ids = {pv.position.position_id for pv in self._strategist_view.positions}
        pm_ids = {pv.position.position_id for pv in self._pm_view.positions}
        assert strat_ids == snap_ids
        assert pm_ids == strat_ids
        # Analyst held-positions
        analyst_ids = {hp.position_id for hp in self._analyst_view.held_positions}
        assert analyst_ids == snap_ids

    def test_synthesizer_sector_keys_subset_of_strategist_and_gross_net_match(self) -> None:
        """Synth sector keys ⊆ strat sectors; synth gross == strat gross within 1e-9;
        synth net_directional == strat net_directional within 1e-9."""
        strat_sectors = {e.sector for e in self._strategist_view.sector_exposure}
        synth_sectors = set(self._synth_view.exposure.sector_exposure_pct.keys())
        # Synthesizer only includes sectors with non-zero net — a subset of strategist's
        assert synth_sectors <= strat_sectors
        # Gross exposure agrees across synthesizer and strategist
        assert (
            abs(
                self._synth_view.exposure.gross_exposure_pct
                - self._strategist_view.directional_exposure.gross_pct_of_portfolio
            )
            < 1e-9
        )
        # Net directional agrees
        assert (
            abs(
                self._synth_view.exposure.net_directional_pct
                - self._strategist_view.directional_exposure.net_directional_pct_of_portfolio
            )
            < 1e-9
        )

    def test_synthesizer_information_hiding(self) -> None:
        """SynthesizerView must not expose PnL, drawdown, or activity log."""
        assert not hasattr(self._synth_view, "portfolio_pnl")
        assert not hasattr(self._synth_view, "drawdown")
        assert not hasattr(self._synth_view, "intra_invocation_changelog")

    def test_analyst_information_hiding(self) -> None:
        """Analyst held-positions must not expose unrealized_pnl_usd or thesis content."""
        for hp in self._analyst_view.held_positions:
            assert not hasattr(hp, "unrealized_pnl_usd")
            assert not hasattr(hp, "thesis")

    def test_strategist_and_pm_privileged_fields(self) -> None:
        """Strategist exposes full thesis components; PM exposes thesis quality + trail."""
        # Strategist: at least one position with non-empty thesis components
        positions_with_thesis = [
            pv for pv in self._strategist_view.positions if pv.thesis is not None
        ]
        assert len(positions_with_thesis) > 0
        for pv in positions_with_thesis:
            assert pv.thesis is not None
            assert len(pv.thesis.components) > 0
        # PM: thesis quality aggregates and modification trail
        assert isinstance(self._pm_view.thesis_quality_aggregates, ThesisQualityAggregate)
        trail = self._pm_view.position_modification_trail
        assert isinstance(trail, dict)
        snap_ids = {p.position_id for p in self._snapshot.open_positions} | {
            p.position_id for p in self._snapshot.pending_positions
        }
        for pid in trail:
            assert pid in snap_ids
        # POS-NVDA and POS-AMD have non-empty trails per fixture
        assert len(trail.get("POS-NVDA", ())) > 0
        assert len(trail.get("POS-AMD", ())) > 0


# ---------------------------------------------------------------------------
# Section C — Assembler correctness against known formulas
# ---------------------------------------------------------------------------


class TestSectionCAssemblerCorrectness:
    """Verify computed fields match exact formulas from story specs.

    Uses the multi-position fixture (NVDA long, AMD short, JPM long, plus a
    pending position) which provides distinct per-direction and per-ticker values.
    """

    def setup_method(self) -> None:
        self._assembled = _assemble(build_multi_position_snapshot_inputs())
        self._snapshot = self._assembled.snapshot

    def _pos_by_id(self, position_id: str) -> PositionView:
        p = self._snapshot.position_by_id(position_id)
        assert p is not None, f"position {position_id} not found"
        return p

    def test_per_ticker_market_values(self) -> None:
        """NVDA long, AMD short (negative), JPM long — distinct per-ticker formulas."""
        nvda = self._pos_by_id("POS-NVDA")
        amd = self._pos_by_id("POS-AMD")
        jpm = self._pos_by_id("POS-JPM")
        assert abs(float(nvda.current_market_value_usd) - 100.0 * 510.0) < 1e-9
        assert abs(float(amd.current_market_value_usd) - (-(250.0 * 115.0))) < 1e-9  # short → neg
        assert abs(float(jpm.current_market_value_usd) - 100.0 * 195.0) < 1e-9

    def test_per_direction_unrealized_pnl_and_portfolio_total(self) -> None:
        """LONG: mv-cost; SHORT: cost-abs(mv); portfolio total == sum of positions (Decimal)."""
        from decimal import Decimal

        nvda = self._pos_by_id("POS-NVDA")
        amd = self._pos_by_id("POS-AMD")
        assert abs(float(nvda.unrealized_pnl_usd) - (100.0 * 510.0 - 100.0 * 500.0)) < 1e-9
        assert abs(float(amd.unrealized_pnl_usd) - (250.0 * 120.0 - 250.0 * 115.0)) < 1e-9
        pos_pnl = sum(p.unrealized_pnl_usd for p in self._snapshot.open_positions)
        assert abs(
            self._snapshot.portfolio_pnl.total_unrealized_pnl_usd - Decimal(str(pos_pnl))
        ) < Decimal("1e-9")

    def test_position_weights_are_abs_mv_over_total(self) -> None:
        total_value = (
            sum(abs(float(p.current_market_value_usd)) for p in self._snapshot.open_positions)
            + sum(abs(float(p.current_market_value_usd)) for p in self._snapshot.pending_positions)
            + self._snapshot.cash_ledger.current_cash_usd
        )
        for pos in self._snapshot.open_positions:
            expected = (abs(float(pos.current_market_value_usd)) / total_value) * 100.0
            assert abs(pos.position_weight_pct - expected) < 1e-6

    def test_gross_exposure_formula(self) -> None:
        live_positions = (
            *self._snapshot.open_positions,
            *self._snapshot.pending_positions,
        )
        long_delta = sum(
            float(p.delta_adjusted_exposure_usd)
            for p in live_positions
            if p.delta_adjusted_exposure_usd > 0
        )
        short_delta = sum(
            abs(float(p.delta_adjusted_exposure_usd))
            for p in live_positions
            if p.delta_adjusted_exposure_usd < 0
        )
        total_value = (
            sum(abs(float(p.current_market_value_usd)) for p in live_positions)
            + self._snapshot.cash_ledger.current_cash_usd
        )
        if total_value > 0:
            expected_gross = (long_delta + short_delta) / total_value * 100.0
            actual_gross = self._snapshot.directional_exposure.gross_pct_of_portfolio
            assert abs(actual_gross - expected_gross) < 1e-6

    def test_cash_pct_and_true_deployable_capital(self) -> None:
        """cash_pct_of_portfolio and true_deployable_capital match their exact formulas."""
        cash = self._snapshot.cash_ledger.current_cash_usd
        total_value = (
            sum(abs(float(p.current_market_value_usd)) for p in self._snapshot.open_positions)
            + sum(abs(float(p.current_market_value_usd)) for p in self._snapshot.pending_positions)
            + cash
        )
        expected_pct = (cash / total_value) * 100.0
        assert abs(self._snapshot.cash_ledger.cash_pct_of_portfolio - expected_pct) < 1e-6
        ledger = self._snapshot.cash_ledger
        expected_deployable = (
            ledger.settled_cash_usd - ledger.reserved_capital_usd - ledger.margin_held_usd
        )
        assert abs(ledger.true_deployable_capital_usd - expected_deployable) < 1e-9


# ---------------------------------------------------------------------------
# Section D — Freshness reporting
# ---------------------------------------------------------------------------


class TestSectionDFreshness:
    """Stale, unknown-ticker, all-fresh, and fill_collection→snapshot boundary tests."""

    def test_stale_positions_reported(self) -> None:
        result = _assemble(build_stale_pricing_snapshot_inputs())
        assert "POS-NVDA-STALE" in result.freshness.position_ids_priced_stale
        assert "POS-JPM-STALE" in result.freshness.position_ids_priced_stale

    def test_stale_count_fresh_is_zero(self) -> None:
        result = _assemble(build_stale_pricing_snapshot_inputs())
        assert result.freshness.count_priced_fresh == 0

    def test_stale_all_position_prices_fresh_is_false(self) -> None:
        result = _assemble(build_stale_pricing_snapshot_inputs())
        assert result.freshness.all_position_prices_fresh is False

    def test_stale_is_position_price_stale_returns_true(self) -> None:
        result = _assemble(build_stale_pricing_snapshot_inputs())
        assert result.freshness.is_position_price_stale("POS-NVDA-STALE") is True
        assert result.freshness.is_position_price_stale("POS-JPM-STALE") is True

    def test_unknown_ticker_reported(self) -> None:
        result = _assemble(build_unknown_ticker_snapshot_inputs())
        assert "POS-UNKNOWN" in result.freshness.position_ids_unknown_ticker

    def test_unknown_is_position_price_stale_returns_true(self) -> None:
        result = _assemble(build_unknown_ticker_snapshot_inputs())
        assert result.freshness.is_position_price_stale("POS-UNKNOWN") is True

    def test_known_ticker_is_not_stale(self) -> None:
        result = _assemble(build_unknown_ticker_snapshot_inputs())
        assert result.freshness.is_position_price_stale("POS-KNOWN") is False

    def test_all_fresh_multi_position(self) -> None:
        result = _assemble(build_multi_position_snapshot_inputs())
        assert result.freshness.all_position_prices_fresh is True
        assert result.freshness.count_priced_stale == 0
        assert result.freshness.count_unknown_ticker == 0

    def test_phase1_to_snapshot_two_seconds(self) -> None:
        """now = phase1_at + 2s → fill_collection_to_snapshot_seconds == 2.0."""
        phase1_at = datetime(2025, 6, 1, 9, 0, 0, tzinfo=UTC)
        now = phase1_at + timedelta(seconds=2)

        fixture = _make_base_fixture(
            cash_ledger=_make_cash_ledger(),
            invocation_metadata=_make_invocation_metadata(
                invocation_id=_INV_ID, phase1_at=phase1_at
            ),
            now=now,
        )
        config = _make_config()
        repo = StubPortfolioStateRepository(fixture)
        price_provider = StubCurrentPriceProvider({}, now)
        result = assemble_snapshot(
            repository=repo,
            price_provider=price_provider,
            option_price_provider=StubOptionPriceProvider({}, now),
            sector_resolver=_make_sector_resolver({}),
            config=config,
            now=now,
        )
        assert abs(result.freshness.fill_collection_to_snapshot_seconds - 2.0) < 1e-9
        assert result.freshness.fill_collection_to_snapshot_within_threshold is True

    def test_phase1_boundary_exactly_at_threshold(self) -> None:
        """now = phase1_at + max_seconds → within_threshold True."""
        config = _make_config()
        max_s = config.snapshot_freshness_max_fill_collection_to_snapshot_seconds  # 300.0
        phase1_at = datetime(2025, 6, 1, 9, 0, 0, tzinfo=UTC)
        now = phase1_at + timedelta(seconds=max_s)

        fixture = _make_base_fixture(
            cash_ledger=_make_cash_ledger(),
            invocation_metadata=_make_invocation_metadata(
                invocation_id=_INV_ID, phase1_at=phase1_at
            ),
            now=now,
        )
        repo = StubPortfolioStateRepository(fixture)
        price_provider = StubCurrentPriceProvider({}, now)
        result = assemble_snapshot(
            repository=repo,
            price_provider=price_provider,
            option_price_provider=StubOptionPriceProvider({}, now),
            sector_resolver=_make_sector_resolver({}),
            config=config,
            now=now,
        )
        assert result.freshness.fill_collection_to_snapshot_within_threshold is True

    def test_phase1_boundary_one_ms_past_threshold(self) -> None:
        """now = phase1_at + max_seconds + 1ms → within_threshold False."""
        config = _make_config()
        max_s = config.snapshot_freshness_max_fill_collection_to_snapshot_seconds  # 300.0
        phase1_at = datetime(2025, 6, 1, 9, 0, 0, tzinfo=UTC)
        now = phase1_at + timedelta(seconds=max_s) + timedelta(milliseconds=1)

        fixture = _make_base_fixture(
            cash_ledger=_make_cash_ledger(),
            invocation_metadata=_make_invocation_metadata(
                invocation_id=_INV_ID, phase1_at=phase1_at
            ),
            now=now,
        )
        repo = StubPortfolioStateRepository(fixture)
        price_provider = StubCurrentPriceProvider({}, now)
        result = assemble_snapshot(
            repository=repo,
            price_provider=price_provider,
            option_price_provider=StubOptionPriceProvider({}, now),
            sector_resolver=_make_sector_resolver({}),
            config=config,
            now=now,
        )
        assert result.freshness.fill_collection_to_snapshot_within_threshold is False


# ---------------------------------------------------------------------------
# Section E — Determinism
# ---------------------------------------------------------------------------


class TestSectionEDeterminism:
    """Identical inputs must produce identical outputs throughout the pipeline."""

    def test_two_snapshots_are_equal(self) -> None:
        inputs = build_multi_position_snapshot_inputs()
        result1 = _assemble(inputs)
        result2 = _assemble(inputs)
        assert result1 == result2

    def test_two_synthesizer_views_are_equal(self) -> None:
        inputs = build_multi_position_snapshot_inputs()
        _, _, sector_resolver, _, _ = inputs
        r1 = _assemble(inputs)
        r2 = _assemble(inputs)
        v1 = project_synthesizer_view(r1.snapshot, sector_resolver=sector_resolver)
        v2 = project_synthesizer_view(r2.snapshot, sector_resolver=sector_resolver)
        assert v1 == v2

    def test_two_strategist_views_are_equal(self) -> None:
        inputs = build_multi_position_snapshot_inputs()
        r1 = _assemble(inputs)
        r2 = _assemble(inputs)
        assert project_strategist_view(r1.snapshot) == project_strategist_view(r2.snapshot)

    def test_two_pm_views_are_equal(self) -> None:
        inputs = build_multi_position_snapshot_inputs()
        r1 = _assemble(inputs)
        r2 = _assemble(inputs)
        assert project_portfolio_manager_view(r1.snapshot) == project_portfolio_manager_view(
            r2.snapshot
        )

    def test_two_analyst_views_are_equal(self) -> None:
        inputs = build_multi_position_snapshot_inputs()
        _, _, sector_resolver, _, _ = inputs
        r1 = _assemble(inputs)
        r2 = _assemble(inputs)
        total_value = r1.snapshot.cash_ledger.current_cash_usd + sum(
            abs(float(p.current_market_value_usd)) for p in r1.snapshot.open_positions
        )
        v1 = project_analyst_view(
            r1.snapshot,
            sector_resolver=sector_resolver,
            per_position_size_rule_id="position_max_size_pct",
            total_portfolio_value_usd=money(total_value),
            available_capital_usd=signed_money(r1.snapshot.cash_ledger.true_deployable_capital_usd),
        )
        v2 = project_analyst_view(
            r2.snapshot,
            sector_resolver=sector_resolver,
            per_position_size_rule_id="position_max_size_pct",
            total_portfolio_value_usd=money(total_value),
            available_capital_usd=signed_money(r2.snapshot.cash_ledger.true_deployable_capital_usd),
        )
        assert v1 == v2


# ---------------------------------------------------------------------------
# Section F — Empty / degenerate edge cases
# ---------------------------------------------------------------------------


class TestSectionFEdgeCases:
    """Minimal (empty) portfolio and zero-value edge cases.

    test_assembler's empty-portfolio test covers snapshot shape and zero pnl.
    These tests add the distinct view-projection assertions not present there.
    """

    def setup_method(self) -> None:
        inputs = build_minimal_snapshot_inputs()
        self._assembled = _assemble(inputs)
        self._snapshot = self._assembled.snapshot
        _, _, self._sector_resolver, _, _ = inputs

    def test_all_consumer_projections_succeed_on_empty_portfolio(self) -> None:
        """All four consumer projections succeed on an empty snapshot."""
        synth = project_synthesizer_view(self._snapshot, sector_resolver=self._sector_resolver)
        analyst = project_analyst_view(
            self._snapshot,
            sector_resolver=self._sector_resolver,
            per_position_size_rule_id="position_max_size_pct",
            total_portfolio_value_usd=money(100_000.0),
            available_capital_usd=signed_money(
                self._snapshot.cash_ledger.true_deployable_capital_usd
            ),
        )
        strat = project_strategist_view(self._snapshot)
        pm = project_portfolio_manager_view(self._snapshot)
        assert synth is not None
        assert analyst is not None
        assert strat is not None
        assert pm is not None

    def test_empty_portfolio_sector_and_exposure_zero(self) -> None:
        """No positions → empty sector dict; zero gross and unrealized PnL."""
        synth_view = project_synthesizer_view(self._snapshot, sector_resolver=self._sector_resolver)
        strat_view = project_strategist_view(self._snapshot)
        # Synthesizer: no positions → empty sector exposure dict
        assert synth_view.exposure.sector_exposure_pct == {}
        # Strategist: no positions → zero gross and zero unrealized PnL
        assert strat_view.directional_exposure.gross_pct_of_portfolio == 0.0
        assert strat_view.portfolio_pnl.total_unrealized_pnl_usd == 0.0

    def test_empty_portfolio_cash_and_freshness(self) -> None:
        """Cash-only portfolio: 100% cash pct; freshness reflects zero positions."""
        # Freshness
        assert self._assembled.freshness.total_positions == 0
        assert self._assembled.freshness.oldest_price_age_seconds is None
        # Snapshot shape
        assert self._snapshot.open_positions == ()
        assert self._snapshot.pending_positions == ()
        # Cash = 100% of portfolio when there are no positions
        assert abs(self._snapshot.cash_ledger.cash_pct_of_portfolio - 100.0) < 1e-6
        # Rolling realized PnL all zero for empty portfolio
        for v in self._snapshot.portfolio_pnl.rolling_realized_pnl.values():
            assert v == 0.0


# ---------------------------------------------------------------------------
# Section G — Repository error propagation (not-wrapped assertions)
# ---------------------------------------------------------------------------

# Propagation tests (pytest.raises only) are covered by test_assembler's
# _FailingRepository / _ConsistencyErrorRepository suites.  Only the
# "not-wrapped" message-asserting variants are unique to this file.

_G_NOW = datetime(2025, 6, 1, 9, 30, 0, tzinfo=UTC)


class _RaisingRepo:
    """Stub repository that re-raises a pre-constructed error on every call.

    Shared by the two Section G not-wrapped tests so the helper function
    stays below the C901 complexity ceiling.
    """

    def __init__(self, error: RepositoryReadError | RepositoryConsistencyError) -> None:
        self._error = error

    def _raise(self) -> NoReturn:
        raise self._error

    def get_open_positions(self) -> NoReturn:
        self._raise()

    def get_pending_positions(self) -> NoReturn:
        self._raise()

    def get_drawdown_state(self) -> NoReturn:
        self._raise()

    def get_portfolio_pnl_inputs(self) -> NoReturn:
        self._raise()

    def get_active_theses(self) -> NoReturn:
        self._raise()

    def get_recent_thesis_resolutions(self, *, lookback_trading_days: int) -> NoReturn:
        del lookback_trading_days
        self._raise()

    def get_cash_ledger(self) -> NoReturn:
        self._raise()

    def get_regt_excess_aggregates(self, now: datetime) -> NoReturn:
        del now
        self._raise()

    def get_pending_orders(self) -> NoReturn:
        self._raise()

    def get_active_risk_parameters(self) -> NoReturn:
        self._raise()

    def get_intra_invocation_changelog(self, *, invocation_id: str) -> NoReturn:
        del invocation_id
        self._raise()

    def get_recent_pm_decision_log(self, *, sliding_window_invocations: int) -> NoReturn:
        del sliding_window_invocations
        self._raise()

    def get_position_modification_trail(self, *, position_ids: tuple[str, ...]) -> NoReturn:
        del position_ids
        self._raise()

    def get_thesis_quality_aggregates(self) -> NoReturn:
        self._raise()

    def get_brackets_for_positions(self, *, position_ids: tuple[str, ...]) -> NoReturn:
        del position_ids
        self._raise()

    def get_current_invocation_metadata(self) -> NoReturn:
        self._raise()

    def get_prior_invocation_context(self) -> NoReturn:
        self._raise()


def _run_with_raising_repo(
    error: RepositoryReadError | RepositoryConsistencyError,
) -> AssembledSnapshot:
    """Invoke assemble_snapshot with a repo that raises *error* immediately."""
    return assemble_snapshot(
        repository=_RaisingRepo(error),
        price_provider=StubCurrentPriceProvider({}, _G_NOW),
        option_price_provider=StubOptionPriceProvider({}, _G_NOW),
        sector_resolver=_make_sector_resolver({}),
        config=_make_config(),
        now=_G_NOW,
    )


class TestSectionGRepositoryErrors:
    """Verify repository errors propagate unmodified from assemble_snapshot.

    Propagation (pytest.raises) is covered by test_assembler; only the
    not-wrapped / message-asserting variants are unique here.
    """

    def test_repository_read_error_is_not_wrapped(self) -> None:
        """The exception should be exactly RepositoryReadError, not a wrapped variant."""
        try:
            _run_with_raising_repo(RepositoryReadError("simulated DB error"))
        except RepositoryReadError as exc:
            assert "simulated DB error" in str(exc)
        except Exception as exc:
            pytest.fail(f"Expected RepositoryReadError; got {type(exc).__name__}: {exc}")

    def test_repository_consistency_error_is_not_wrapped(self) -> None:
        try:
            _run_with_raising_repo(RepositoryConsistencyError("simulated consistency violation"))
        except RepositoryConsistencyError as exc:
            assert "simulated consistency violation" in str(exc)
        except Exception as exc:
            pytest.fail(f"Expected RepositoryConsistencyError; got {type(exc).__name__}: {exc}")
