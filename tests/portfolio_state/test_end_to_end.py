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

import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn

import pytest

from alphamind.portfolio_state import PortfolioStateConfig
from alphamind.portfolio_state.assembler import assemble_snapshot
from alphamind.portfolio_state.computations.exposure import SectorResolver
from alphamind.portfolio_state.consumers.analyst import project_analyst_view
from alphamind.portfolio_state.consumers.portfolio_manager import project_portfolio_manager_view
from alphamind.portfolio_state.consumers.strategist import project_strategist_view
from alphamind.portfolio_state.consumers.synthesizer import project_synthesizer_view
from alphamind.portfolio_state.freshness import AssembledSnapshot
from alphamind.portfolio_state.pricing import PriceQuote, StubCurrentPriceProvider
from alphamind.portfolio_state.records.thesis_quality import ThesisQualityAggregate
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


def _run(coro: Coroutine[Any, Any, AssembledSnapshot]) -> AssembledSnapshot:
    return asyncio.run(coro)


def _assemble(fixture_inputs: _FixtureInputs) -> AssembledSnapshot:
    """Construct stubs from fixture tuple and call assemble_snapshot."""
    repo_fixture, quotes, sector_resolver, config, now = fixture_inputs
    repo = StubPortfolioStateRepository(repo_fixture)
    price_provider = StubCurrentPriceProvider(quotes, now)
    return _run(
        assemble_snapshot(
            repository=repo,
            price_provider=price_provider,
            sector_resolver=sector_resolver,
            config=config,
            now=now,
        )
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
    """Cross-view consistency and information-hiding invariants on the multi-position fixture."""

    def setup_method(self) -> None:
        inputs = build_multi_position_snapshot_inputs()
        self._assembled = _assemble(inputs)
        self._snapshot = self._assembled.snapshot
        _, _, self._sector_resolver, _, _ = inputs
        total_value = self._snapshot.cash_ledger.current_cash_usd + sum(
            abs(p.current_market_value_usd) for p in self._snapshot.open_positions
        )
        self._synth_view = project_synthesizer_view(
            self._snapshot, sector_resolver=self._sector_resolver
        )
        self._analyst_view = project_analyst_view(
            self._snapshot,
            sector_resolver=self._sector_resolver,
            per_position_size_rule_id="max_position_size_usd",
            total_portfolio_value_usd=total_value,
        )
        self._strategist_view = project_strategist_view(self._snapshot)
        self._pm_view = project_portfolio_manager_view(self._snapshot)

    def test_synthesizer_positions_cover_open_and_pending(self) -> None:
        snap_ids = {p.position_id for p in self._snapshot.open_positions} | {
            p.position_id for p in self._snapshot.pending_positions
        }
        # Synthesizer emits one entry per position
        assert len(self._synth_view.positions) == len(snap_ids)

    def test_strategist_position_ids_match_snapshot(self) -> None:
        snap_ids = {p.position_id for p in self._snapshot.open_positions} | {
            p.position_id for p in self._snapshot.pending_positions
        }
        strat_ids = {pv.position.position_id for pv in self._strategist_view.positions}
        assert strat_ids == snap_ids

    def test_pm_position_ids_match_strategist(self) -> None:
        strat_ids = {pv.position.position_id for pv in self._strategist_view.positions}
        pm_ids = {pv.position.position_id for pv in self._pm_view.positions}
        assert pm_ids == strat_ids

    def test_analyst_held_positions_are_open_only(self) -> None:
        open_ids = {p.position_id for p in self._snapshot.open_positions}
        analyst_ids = {hp.position_id for hp in self._analyst_view.held_positions}
        assert analyst_ids == open_ids

    def test_synthesizer_sector_exposure_keys_match_strategist_sectors(self) -> None:
        strat_sectors = {e.sector for e in self._strategist_view.sector_exposure}
        synth_sectors = set(self._synth_view.exposure.sector_exposure_pct.keys())
        # Synthesizer only includes sectors with non-zero net — a subset of strategist's
        assert synth_sectors <= strat_sectors

    def test_synthesizer_gross_matches_strategist(self) -> None:
        assert (
            abs(
                self._synth_view.exposure.gross_exposure_pct
                - self._strategist_view.directional_exposure.gross_pct_of_portfolio
            )
            < 1e-9
        )

    def test_synthesizer_net_directional_matches_strategist(self) -> None:
        assert (
            abs(
                self._synth_view.exposure.net_directional_pct
                - self._strategist_view.directional_exposure.net_directional_pct_of_portfolio
            )
            < 1e-9
        )

    def test_synthesizer_does_not_expose_pnl(self) -> None:
        # SynthesizerView has no portfolio_pnl or drawdown attributes
        assert not hasattr(self._synth_view, "portfolio_pnl")
        assert not hasattr(self._synth_view, "drawdown")

    def test_synthesizer_does_not_expose_activity_log(self) -> None:
        assert not hasattr(self._synth_view, "intra_invocation_changelog")

    def test_analyst_held_positions_no_unrealized_pnl(self) -> None:
        for hp in self._analyst_view.held_positions:
            assert not hasattr(hp, "unrealized_pnl_usd")

    def test_analyst_held_positions_no_thesis_content(self) -> None:
        for hp in self._analyst_view.held_positions:
            assert not hasattr(hp, "thesis")

    def test_strategist_includes_full_thesis_components(self) -> None:
        positions_with_thesis = [
            pv for pv in self._strategist_view.positions if pv.thesis is not None
        ]
        assert len(positions_with_thesis) > 0
        for pv in positions_with_thesis:
            assert pv.thesis is not None
            assert len(pv.thesis.components) > 0

    def test_pm_includes_thesis_quality_aggregates(self) -> None:
        assert isinstance(self._pm_view.thesis_quality_aggregates, ThesisQualityAggregate)

    def test_pm_includes_position_modification_trail(self) -> None:
        # PM view has a position_modification_trail dict
        trail = self._pm_view.position_modification_trail
        assert isinstance(trail, dict)

    def test_pm_modification_trail_keys_are_valid_position_ids(self) -> None:
        snap_ids = {p.position_id for p in self._snapshot.open_positions} | {
            p.position_id for p in self._snapshot.pending_positions
        }
        for pid in self._pm_view.position_modification_trail:
            assert pid in snap_ids

    def test_pm_non_empty_trails_match_fixture(self) -> None:
        # POS-NVDA and POS-AMD have non-empty trails
        trail = self._pm_view.position_modification_trail
        assert len(trail.get("POS-NVDA", ())) > 0
        assert len(trail.get("POS-AMD", ())) > 0


# ---------------------------------------------------------------------------
# Section C — Assembler correctness against known formulas
# ---------------------------------------------------------------------------


class TestSectionCAssemblerCorrectness:
    """Verify computed fields match exact formulas from story specs."""

    def setup_method(self) -> None:
        self._assembled = _assemble(build_multi_position_snapshot_inputs())
        self._snapshot = self._assembled.snapshot

    def _pos_by_id(self, position_id: str) -> PositionView:
        p = self._snapshot.position_by_id(position_id)
        assert p is not None, f"position {position_id} not found"
        return p

    def test_nvda_long_market_value(self) -> None:
        pos = self._pos_by_id("POS-NVDA")
        expected = 100.0 * 510.0  # share_count * price
        assert abs(pos.current_market_value_usd - expected) < 1e-9

    def test_amd_short_market_value_is_negative(self) -> None:
        pos = self._pos_by_id("POS-AMD")
        expected = -(250.0 * 115.0)  # short: negative market value
        assert abs(pos.current_market_value_usd - expected) < 1e-9

    def test_jpm_long_market_value(self) -> None:
        pos = self._pos_by_id("POS-JPM")
        expected = 100.0 * 195.0
        assert abs(pos.current_market_value_usd - expected) < 1e-9

    def test_nvda_unrealized_pnl_long(self) -> None:
        pos = self._pos_by_id("POS-NVDA")
        cost = 100.0 * 500.0
        market_value = 100.0 * 510.0
        expected = market_value - cost  # LONG: mv - cost
        assert abs(pos.unrealized_pnl_usd - expected) < 1e-9

    def test_amd_unrealized_pnl_short(self) -> None:
        pos = self._pos_by_id("POS-AMD")
        cost = 250.0 * 120.0
        market_value_abs = 250.0 * 115.0
        expected = cost - market_value_abs  # SHORT: cost - abs(mv)
        assert abs(pos.unrealized_pnl_usd - expected) < 1e-9

    def test_portfolio_total_unrealized_pnl_is_sum_of_positions(self) -> None:
        pos_pnl = sum(p.unrealized_pnl_usd for p in self._snapshot.open_positions)
        assert abs(self._snapshot.portfolio_pnl.total_unrealized_pnl_usd - pos_pnl) < 1e-9

    def test_position_weights_are_abs_mv_over_total(self) -> None:
        total_value = (
            sum(abs(p.current_market_value_usd) for p in self._snapshot.open_positions)
            + sum(abs(p.current_market_value_usd) for p in self._snapshot.pending_positions)
            + self._snapshot.cash_ledger.current_cash_usd
        )
        for pos in self._snapshot.open_positions:
            expected = (abs(pos.current_market_value_usd) / total_value) * 100.0
            assert abs(pos.position_weight_pct - expected) < 1e-6

    def test_gross_exposure_formula(self) -> None:
        long_delta = sum(
            p.delta_adjusted_exposure_usd
            for p in self._snapshot.open_positions
            if p.delta_adjusted_exposure_usd > 0
        )
        short_delta = sum(
            abs(p.delta_adjusted_exposure_usd)
            for p in self._snapshot.open_positions
            if p.delta_adjusted_exposure_usd < 0
        )
        total_value = (
            sum(abs(p.current_market_value_usd) for p in self._snapshot.open_positions)
            + sum(abs(p.current_market_value_usd) for p in self._snapshot.pending_positions)
            + self._snapshot.cash_ledger.current_cash_usd
        )
        if total_value > 0:
            expected_gross = (long_delta + short_delta) / total_value * 100.0
            actual_gross = self._snapshot.directional_exposure.gross_pct_of_portfolio
            assert abs(actual_gross - expected_gross) < 1e-6

    def test_cash_pct_of_portfolio(self) -> None:
        cash = self._snapshot.cash_ledger.current_cash_usd
        total_value = (
            sum(abs(p.current_market_value_usd) for p in self._snapshot.open_positions)
            + sum(abs(p.current_market_value_usd) for p in self._snapshot.pending_positions)
            + cash
        )
        expected_pct = (cash / total_value) * 100.0
        assert abs(self._snapshot.cash_ledger.cash_pct_of_portfolio - expected_pct) < 1e-6

    def test_true_deployable_capital(self) -> None:
        ledger = self._snapshot.cash_ledger
        expected = ledger.settled_cash_usd - ledger.reserved_capital_usd - ledger.margin_held_usd
        assert abs(ledger.true_deployable_capital_usd - expected) < 1e-9


# ---------------------------------------------------------------------------
# Section D — Freshness reporting
# ---------------------------------------------------------------------------


class TestSectionDFreshness:
    """Stale, unknown-ticker, all-fresh, and phase1→snapshot boundary tests."""

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
        """now = phase1_at + 2s → phase1_to_snapshot_seconds == 2.0."""
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
        result = _run(
            assemble_snapshot(
                repository=repo,
                price_provider=price_provider,
                sector_resolver=_make_sector_resolver({}),
                config=config,
                now=now,
            )
        )
        assert abs(result.freshness.phase1_to_snapshot_seconds - 2.0) < 1e-9
        assert result.freshness.phase1_to_snapshot_within_threshold is True

    def test_phase1_boundary_exactly_at_threshold(self) -> None:
        """now = phase1_at + max_seconds → within_threshold True."""
        config = _make_config()
        max_s = config.snapshot_freshness_max_phase1_to_snapshot_seconds  # 300.0
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
        result = _run(
            assemble_snapshot(
                repository=repo,
                price_provider=price_provider,
                sector_resolver=_make_sector_resolver({}),
                config=config,
                now=now,
            )
        )
        assert result.freshness.phase1_to_snapshot_within_threshold is True

    def test_phase1_boundary_one_ms_past_threshold(self) -> None:
        """now = phase1_at + max_seconds + 1ms → within_threshold False."""
        config = _make_config()
        max_s = config.snapshot_freshness_max_phase1_to_snapshot_seconds  # 300.0
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
        result = _run(
            assemble_snapshot(
                repository=repo,
                price_provider=price_provider,
                sector_resolver=_make_sector_resolver({}),
                config=config,
                now=now,
            )
        )
        assert result.freshness.phase1_to_snapshot_within_threshold is False


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
            abs(p.current_market_value_usd) for p in r1.snapshot.open_positions
        )
        v1 = project_analyst_view(
            r1.snapshot,
            sector_resolver=sector_resolver,
            per_position_size_rule_id="max_position_size_usd",
            total_portfolio_value_usd=total_value,
        )
        v2 = project_analyst_view(
            r2.snapshot,
            sector_resolver=sector_resolver,
            per_position_size_rule_id="max_position_size_usd",
            total_portfolio_value_usd=total_value,
        )
        assert v1 == v2


# ---------------------------------------------------------------------------
# Section F — Empty / degenerate edge cases
# ---------------------------------------------------------------------------


class TestSectionFEdgeCases:
    """Minimal (empty) portfolio and zero-value edge cases."""

    def setup_method(self) -> None:
        inputs = build_minimal_snapshot_inputs()
        self._assembled = _assemble(inputs)
        self._snapshot = self._assembled.snapshot
        _, _, self._sector_resolver, _, _ = inputs

    def test_synthesizer_view_succeeds(self) -> None:
        view = project_synthesizer_view(self._snapshot, sector_resolver=self._sector_resolver)
        assert view is not None

    def test_analyst_view_succeeds(self) -> None:
        view = project_analyst_view(
            self._snapshot,
            sector_resolver=self._sector_resolver,
            per_position_size_rule_id="max_position_size_usd",
            total_portfolio_value_usd=100_000.0,
        )
        assert view is not None

    def test_strategist_view_succeeds(self) -> None:
        view = project_strategist_view(self._snapshot)
        assert view is not None

    def test_pm_view_succeeds(self) -> None:
        view = project_portfolio_manager_view(self._snapshot)
        assert view is not None

    def test_empty_sector_exposure(self) -> None:
        synth_view = project_synthesizer_view(self._snapshot, sector_resolver=self._sector_resolver)
        # No positions → empty sector dict
        assert synth_view.exposure.sector_exposure_pct == {}

    def test_gross_exposure_is_zero(self) -> None:
        strat_view = project_strategist_view(self._snapshot)
        assert strat_view.directional_exposure.gross_pct_of_portfolio == 0.0

    def test_total_unrealized_pnl_is_zero(self) -> None:
        strat_view = project_strategist_view(self._snapshot)
        assert strat_view.portfolio_pnl.total_unrealized_pnl_usd == 0.0

    def test_freshness_total_positions_is_zero(self) -> None:
        assert self._assembled.freshness.total_positions == 0

    def test_freshness_oldest_price_age_is_none(self) -> None:
        assert self._assembled.freshness.oldest_price_age_seconds is None

    def test_cash_only_portfolio_value_is_cash(self) -> None:
        # 100k cash, no positions → total portfolio = 100k
        # cash_pct_of_portfolio should be 100.0
        assert abs(self._snapshot.cash_ledger.cash_pct_of_portfolio - 100.0) < 1e-6

    def test_empty_positions_tuple(self) -> None:
        assert self._snapshot.open_positions == ()
        assert self._snapshot.pending_positions == ()

    def test_rolling_pnl_all_zero(self) -> None:
        for v in self._snapshot.portfolio_pnl.rolling_realized_pnl.values():
            assert v == 0.0


# ---------------------------------------------------------------------------
# Section G — Repository error propagation
# ---------------------------------------------------------------------------


class _RaisingRepositoryReadError:
    """Raises RepositoryReadError on every method call."""

    async def get_open_positions(self) -> NoReturn:
        raise RepositoryReadError("simulated DB error")

    async def get_pending_positions(self) -> NoReturn:
        raise RepositoryReadError("simulated DB error")

    async def get_drawdown_state(self) -> NoReturn:
        raise RepositoryReadError("simulated DB error")

    async def get_portfolio_pnl_inputs(self) -> NoReturn:
        raise RepositoryReadError("simulated DB error")

    async def get_active_theses(self) -> NoReturn:
        raise RepositoryReadError("simulated DB error")

    async def get_recent_thesis_resolutions(self, *, lookback_trading_days: int) -> NoReturn:
        raise RepositoryReadError("simulated DB error")

    async def get_cash_ledger(self) -> NoReturn:
        raise RepositoryReadError("simulated DB error")

    async def get_pending_orders(self) -> NoReturn:
        raise RepositoryReadError("simulated DB error")

    async def get_risk_budget_consumption(self) -> NoReturn:
        raise RepositoryReadError("simulated DB error")

    async def get_active_risk_parameters(self) -> NoReturn:
        raise RepositoryReadError("simulated DB error")

    async def get_intra_invocation_changelog(self, *, invocation_id: str) -> NoReturn:
        raise RepositoryReadError("simulated DB error")

    async def get_recent_pm_decision_log(self, *, sliding_window_invocations: int) -> NoReturn:
        raise RepositoryReadError("simulated DB error")

    async def get_position_modification_trail(self, *, position_ids: tuple[str, ...]) -> NoReturn:
        raise RepositoryReadError("simulated DB error")

    async def get_thesis_quality_aggregates(self) -> NoReturn:
        raise RepositoryReadError("simulated DB error")

    async def get_brackets_for_positions(self, *, position_ids: tuple[str, ...]) -> NoReturn:
        raise RepositoryReadError("simulated DB error")

    async def get_current_invocation_metadata(self) -> NoReturn:
        raise RepositoryReadError("simulated DB error")

    async def get_prior_invocation_context(self) -> NoReturn:
        raise RepositoryReadError("simulated DB error")


class _RaisingRepositoryConsistencyError:
    """Raises RepositoryConsistencyError on every method call."""

    async def get_open_positions(self) -> NoReturn:
        raise RepositoryConsistencyError("simulated consistency violation")

    async def get_pending_positions(self) -> NoReturn:
        raise RepositoryConsistencyError("simulated consistency violation")

    async def get_drawdown_state(self) -> NoReturn:
        raise RepositoryConsistencyError("simulated consistency violation")

    async def get_portfolio_pnl_inputs(self) -> NoReturn:
        raise RepositoryConsistencyError("simulated consistency violation")

    async def get_active_theses(self) -> NoReturn:
        raise RepositoryConsistencyError("simulated consistency violation")

    async def get_recent_thesis_resolutions(self, *, lookback_trading_days: int) -> NoReturn:
        raise RepositoryConsistencyError("simulated consistency violation")

    async def get_cash_ledger(self) -> NoReturn:
        raise RepositoryConsistencyError("simulated consistency violation")

    async def get_pending_orders(self) -> NoReturn:
        raise RepositoryConsistencyError("simulated consistency violation")

    async def get_risk_budget_consumption(self) -> NoReturn:
        raise RepositoryConsistencyError("simulated consistency violation")

    async def get_active_risk_parameters(self) -> NoReturn:
        raise RepositoryConsistencyError("simulated consistency violation")

    async def get_intra_invocation_changelog(self, *, invocation_id: str) -> NoReturn:
        raise RepositoryConsistencyError("simulated consistency violation")

    async def get_recent_pm_decision_log(self, *, sliding_window_invocations: int) -> NoReturn:
        raise RepositoryConsistencyError("simulated consistency violation")

    async def get_position_modification_trail(self, *, position_ids: tuple[str, ...]) -> NoReturn:
        raise RepositoryConsistencyError("simulated consistency violation")

    async def get_thesis_quality_aggregates(self) -> NoReturn:
        raise RepositoryConsistencyError("simulated consistency violation")

    async def get_brackets_for_positions(self, *, position_ids: tuple[str, ...]) -> NoReturn:
        raise RepositoryConsistencyError("simulated consistency violation")

    async def get_current_invocation_metadata(self) -> NoReturn:
        raise RepositoryConsistencyError("simulated consistency violation")

    async def get_prior_invocation_context(self) -> NoReturn:
        raise RepositoryConsistencyError("simulated consistency violation")


class TestSectionGRepositoryErrors:
    """Verify repository errors propagate unmodified from assemble_snapshot."""

    def _run_with_repo(
        self,
        repo: _RaisingRepositoryReadError | _RaisingRepositoryConsistencyError,
    ) -> AssembledSnapshot:
        config = _make_config()
        price_provider = StubCurrentPriceProvider({}, datetime(2025, 6, 1, 9, 30, 0, tzinfo=UTC))
        return _run(
            assemble_snapshot(
                repository=repo,
                price_provider=price_provider,
                sector_resolver=_make_sector_resolver({}),
                config=config,
                now=datetime(2025, 6, 1, 9, 30, 0, tzinfo=UTC),
            )
        )

    def test_repository_read_error_propagates(self) -> None:
        with pytest.raises(RepositoryReadError):
            self._run_with_repo(_RaisingRepositoryReadError())

    def test_repository_consistency_error_propagates(self) -> None:
        with pytest.raises(RepositoryConsistencyError):
            self._run_with_repo(_RaisingRepositoryConsistencyError())

    def test_repository_read_error_is_not_wrapped(self) -> None:
        """The exception should be exactly RepositoryReadError, not a wrapped variant."""
        try:
            self._run_with_repo(_RaisingRepositoryReadError())
        except RepositoryReadError as exc:
            assert "simulated DB error" in str(exc)
        except Exception as exc:
            pytest.fail(f"Expected RepositoryReadError; got {type(exc).__name__}: {exc}")

    def test_repository_consistency_error_is_not_wrapped(self) -> None:
        try:
            self._run_with_repo(_RaisingRepositoryConsistencyError())
        except RepositoryConsistencyError as exc:
            assert "simulated consistency violation" in str(exc)
        except Exception as exc:
            pytest.fail(f"Expected RepositoryConsistencyError; got {type(exc).__name__}: {exc}")
