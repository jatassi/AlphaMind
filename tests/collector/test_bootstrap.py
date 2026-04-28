"""
Tests for alphamind.collector.bootstrap.run_all.

All vendor bootstrap_* functions are mocked — no real API calls or DB writes.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VENDOR_PATCHES = {
    "polygon_reference": "alphamind.collector.bootstrap.collect_reference",
    "seed_assets": "alphamind.collector.bootstrap._seed_asset_universe",
    "polygon_corp_actions": "alphamind.collector.bootstrap.bootstrap_corporate_actions",
    "polygon_equity": "alphamind.collector.bootstrap.bootstrap_universe_bars",
    "fred_daily": "alphamind.collector.bootstrap._fred_bootstrap_daily",
    "fred_monthly": "alphamind.collector.bootstrap._fred_bootstrap_monthly",
    "eia": "alphamind.collector.bootstrap.eia_bootstrap_series",
    "treasury": "alphamind.collector.bootstrap.bootstrap_auctions",
    "bls": "alphamind.collector.bootstrap.bls_bootstrap_series",
    "finnhub_earnings": "alphamind.collector.bootstrap.bootstrap_earnings_calendar",
    "finnhub_economic": "alphamind.collector.bootstrap.bootstrap_economic_calendar",
    "finnhub_ipo": "alphamind.collector.bootstrap.collect_ipo_calendar",
    "finnhub_fda": "alphamind.collector.bootstrap.collect_fda_calendar",
    "finnhub_estimate_revisions": "alphamind.collector.bootstrap.bootstrap_estimate_revisions",
    "finra_short_volume": "alphamind.collector.bootstrap.bootstrap_short_volume",
    "finra_short_interest": "alphamind.collector.bootstrap.bootstrap_short_interest",
}


def _patch_all_vendors():
    """Return a context manager that patches all vendor functions."""
    return {k: patch(v) for k, v in _VENDOR_PATCHES.items()}


# ---------------------------------------------------------------------------
# Test 1 (tracer bullet): run_all is importable and callable without error
# ---------------------------------------------------------------------------


def test_run_all_is_callable():
    patches = _patch_all_vendors()
    for p in patches.values():
        p.__enter__()
    try:
        from alphamind.collector.bootstrap import run_all

        run_all()
    finally:
        for p in patches.values():
            p.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Test 2: all vendor functions are called when only_vendor is None
# ---------------------------------------------------------------------------


def test_run_all_calls_all_vendors():
    with (
        patch("alphamind.collector.bootstrap.collect_reference") as mock_ref,
        patch("alphamind.collector.bootstrap._seed_asset_universe") as mock_seed,
        patch("alphamind.collector.bootstrap.bootstrap_corporate_actions") as mock_corp,
        patch("alphamind.collector.bootstrap.bootstrap_universe_bars") as mock_equity,
        patch("alphamind.collector.bootstrap._fred_bootstrap_daily") as mock_fred_daily,
        patch("alphamind.collector.bootstrap._fred_bootstrap_monthly") as mock_fred_monthly,
        patch("alphamind.collector.bootstrap.eia_bootstrap_series") as mock_eia,
        patch("alphamind.collector.bootstrap.bootstrap_auctions") as mock_treasury,
        patch("alphamind.collector.bootstrap.bls_bootstrap_series") as mock_bls,
        patch("alphamind.collector.bootstrap.bootstrap_earnings_calendar") as mock_earnings,
        patch("alphamind.collector.bootstrap.bootstrap_economic_calendar") as mock_econ,
        patch("alphamind.collector.bootstrap.collect_ipo_calendar") as mock_ipo,
        patch("alphamind.collector.bootstrap.collect_fda_calendar") as mock_fda,
        patch("alphamind.collector.bootstrap.bootstrap_estimate_revisions") as mock_est_rev,
        patch("alphamind.collector.bootstrap.bootstrap_short_volume") as mock_short_vol,
        patch("alphamind.collector.bootstrap.bootstrap_short_interest") as mock_short_int,
    ):
        from alphamind.collector.bootstrap import run_all

        run_all()

    mock_ref.assert_called_once()
    mock_seed.assert_called_once()
    mock_corp.assert_called_once()
    mock_equity.assert_called_once()
    mock_fred_daily.assert_called_once()
    mock_fred_monthly.assert_called_once()
    mock_eia.assert_called_once()
    mock_treasury.assert_called_once()
    mock_bls.assert_called_once()
    mock_earnings.assert_called_once()
    mock_econ.assert_called_once()
    mock_ipo.assert_called_once()
    mock_fda.assert_called_once()
    mock_est_rev.assert_called_once()
    mock_short_vol.assert_called_once()
    mock_short_int.assert_called_once()


# ---------------------------------------------------------------------------
# Test 3: lifecycle order — seed before polygon.reference before corp actions
#          before equity; then macro in any order; then calendar last
# ---------------------------------------------------------------------------


def test_run_all_lifecycle_order():
    call_order: list[str] = []

    def _make_side(name: str):
        def _fn(*args, **kwargs):
            call_order.append(name)

        return _fn

    with (
        patch(
            "alphamind.collector.bootstrap._seed_asset_universe",
            side_effect=_make_side("seed"),
        ),
        patch(
            "alphamind.collector.bootstrap.collect_reference",
            side_effect=_make_side("polygon.reference"),
        ),
        patch(
            "alphamind.collector.bootstrap.bootstrap_corporate_actions",
            side_effect=_make_side("polygon.corporate_actions"),
        ),
        patch(
            "alphamind.collector.bootstrap.bootstrap_universe_bars",
            side_effect=_make_side("polygon.equity"),
        ),
        patch(
            "alphamind.collector.bootstrap._fred_bootstrap_daily",
            side_effect=_make_side("fred.daily"),
        ),
        patch(
            "alphamind.collector.bootstrap._fred_bootstrap_monthly",
            side_effect=_make_side("fred.monthly"),
        ),
        patch(
            "alphamind.collector.bootstrap.eia_bootstrap_series",
            side_effect=_make_side("eia"),
        ),
        patch(
            "alphamind.collector.bootstrap.bootstrap_auctions",
            side_effect=_make_side("treasury"),
        ),
        patch(
            "alphamind.collector.bootstrap.bls_bootstrap_series",
            side_effect=_make_side("bls"),
        ),
        patch(
            "alphamind.collector.bootstrap.bootstrap_earnings_calendar",
            side_effect=_make_side("finnhub.earnings"),
        ),
        patch(
            "alphamind.collector.bootstrap.bootstrap_economic_calendar",
            side_effect=_make_side("finnhub.economic"),
        ),
        patch("alphamind.collector.bootstrap.collect_ipo_calendar"),
        patch("alphamind.collector.bootstrap.collect_fda_calendar"),
        patch("alphamind.collector.bootstrap.bootstrap_estimate_revisions"),
        patch("alphamind.collector.bootstrap.bootstrap_short_volume"),
        patch("alphamind.collector.bootstrap.bootstrap_short_interest"),
    ):
        from alphamind.collector.bootstrap import run_all

        run_all()

    # Step 1: seed then reference
    assert call_order.index("seed") < call_order.index("polygon.reference")
    # Step 2: corporate actions after reference
    assert call_order.index("polygon.reference") < call_order.index("polygon.corporate_actions")
    # Step 3: equity after corporate actions
    assert call_order.index("polygon.corporate_actions") < call_order.index("polygon.equity")
    # Macro steps 4 & 5 after equity
    for macro in ("fred.daily", "eia", "treasury", "fred.monthly", "bls"):
        assert call_order.index("polygon.equity") < call_order.index(macro), (
            f"{macro} must run after polygon.equity"
        )
    # Calendar (step 6) after all macro
    for macro in ("fred.daily", "eia", "treasury", "fred.monthly", "bls"):
        assert call_order.index(macro) < call_order.index("finnhub.earnings"), (
            f"finnhub.earnings must run after {macro}"
        )
    assert call_order.index("finnhub.earnings") < call_order.index("finnhub.economic")


# ---------------------------------------------------------------------------
# Test 4: assets.yaml seed inserts rows before any vendor API call
# ---------------------------------------------------------------------------


def test_seed_runs_before_vendor_api():
    call_order: list[str] = []

    def _make_side(name: str):
        def _fn(*args, **kwargs):
            call_order.append(name)

        return _fn

    with (
        patch(
            "alphamind.collector.bootstrap._seed_asset_universe",
            side_effect=_make_side("seed"),
        ),
        patch(
            "alphamind.collector.bootstrap.collect_reference",
            side_effect=_make_side("polygon.reference"),
        ),
        patch("alphamind.collector.bootstrap.bootstrap_corporate_actions"),
        patch("alphamind.collector.bootstrap.bootstrap_universe_bars"),
        patch("alphamind.collector.bootstrap._fred_bootstrap_daily"),
        patch("alphamind.collector.bootstrap._fred_bootstrap_monthly"),
        patch("alphamind.collector.bootstrap.eia_bootstrap_series"),
        patch("alphamind.collector.bootstrap.bootstrap_auctions"),
        patch("alphamind.collector.bootstrap.bls_bootstrap_series"),
        patch("alphamind.collector.bootstrap.bootstrap_earnings_calendar"),
        patch("alphamind.collector.bootstrap.bootstrap_economic_calendar"),
        patch("alphamind.collector.bootstrap.collect_ipo_calendar"),
        patch("alphamind.collector.bootstrap.collect_fda_calendar"),
        patch("alphamind.collector.bootstrap.bootstrap_estimate_revisions"),
        patch("alphamind.collector.bootstrap.bootstrap_short_volume"),
        patch("alphamind.collector.bootstrap.bootstrap_short_interest"),
    ):
        from alphamind.collector.bootstrap import run_all

        run_all()

    assert call_order[0] == "seed", "seed must be the very first operation"
    assert call_order.index("seed") < call_order.index("polygon.reference")


# ---------------------------------------------------------------------------
# Test 5: exception in one vendor does not abort the orchestrator
# ---------------------------------------------------------------------------


def test_exception_in_vendor_does_not_abort(caplog):
    with (
        patch("alphamind.collector.bootstrap._seed_asset_universe"),
        patch("alphamind.collector.bootstrap.collect_reference"),
        patch(
            "alphamind.collector.bootstrap.bootstrap_corporate_actions",
            side_effect=RuntimeError("network timeout"),
        ),
        patch("alphamind.collector.bootstrap.bootstrap_universe_bars") as mock_equity,
        patch("alphamind.collector.bootstrap._fred_bootstrap_daily"),
        patch("alphamind.collector.bootstrap._fred_bootstrap_monthly"),
        patch("alphamind.collector.bootstrap.eia_bootstrap_series"),
        patch("alphamind.collector.bootstrap.bootstrap_auctions"),
        patch("alphamind.collector.bootstrap.bls_bootstrap_series"),
        patch("alphamind.collector.bootstrap.bootstrap_earnings_calendar"),
        patch("alphamind.collector.bootstrap.bootstrap_economic_calendar"),
        patch("alphamind.collector.bootstrap.collect_ipo_calendar"),
        patch("alphamind.collector.bootstrap.collect_fda_calendar"),
        patch("alphamind.collector.bootstrap.bootstrap_estimate_revisions"),
        patch("alphamind.collector.bootstrap.bootstrap_short_volume"),
        patch("alphamind.collector.bootstrap.bootstrap_short_interest"),
    ):
        from alphamind.collector.bootstrap import run_all

        with caplog.at_level(logging.ERROR):
            run_all()  # must not raise

    # Equity still ran despite corporate_actions failing
    mock_equity.assert_called_once()
    assert any("polygon.corporate_actions" in r.message for r in caplog.records), (
        "expected error log for polygon.corporate_actions"
    )


# ---------------------------------------------------------------------------
# Test 6: --only polygon runs only polygon steps (steps 1, 2, 3)
# ---------------------------------------------------------------------------


def test_only_polygon_runs_polygon_steps_only():
    with (
        patch("alphamind.collector.bootstrap._seed_asset_universe") as mock_seed,
        patch("alphamind.collector.bootstrap.collect_reference") as mock_ref,
        patch("alphamind.collector.bootstrap.bootstrap_corporate_actions") as mock_corp,
        patch("alphamind.collector.bootstrap.bootstrap_universe_bars") as mock_equity,
        patch("alphamind.collector.bootstrap._fred_bootstrap_daily") as mock_fred_daily,
        patch("alphamind.collector.bootstrap._fred_bootstrap_monthly") as mock_fred_monthly,
        patch("alphamind.collector.bootstrap.eia_bootstrap_series") as mock_eia,
        patch("alphamind.collector.bootstrap.bootstrap_auctions") as mock_treasury,
        patch("alphamind.collector.bootstrap.bls_bootstrap_series") as mock_bls,
        patch("alphamind.collector.bootstrap.bootstrap_earnings_calendar") as mock_earnings,
        patch("alphamind.collector.bootstrap.bootstrap_economic_calendar") as mock_econ,
    ):
        from alphamind.collector.bootstrap import run_all

        run_all(only_vendor="polygon")

    mock_seed.assert_called_once()
    mock_ref.assert_called_once()
    mock_corp.assert_called_once()
    mock_equity.assert_called_once()
    mock_fred_daily.assert_not_called()
    mock_fred_monthly.assert_not_called()
    mock_eia.assert_not_called()
    mock_treasury.assert_not_called()
    mock_bls.assert_not_called()
    mock_earnings.assert_not_called()
    mock_econ.assert_not_called()


# ---------------------------------------------------------------------------
# Test 7: --only fred runs FRED daily + FRED monthly
# ---------------------------------------------------------------------------


def test_only_fred_runs_fred_steps():
    with (
        patch("alphamind.collector.bootstrap._seed_asset_universe") as mock_seed,
        patch("alphamind.collector.bootstrap.collect_reference") as mock_ref,
        patch("alphamind.collector.bootstrap.bootstrap_corporate_actions") as mock_corp,
        patch("alphamind.collector.bootstrap.bootstrap_universe_bars") as mock_equity,
        patch("alphamind.collector.bootstrap._fred_bootstrap_daily") as mock_fred_daily,
        patch("alphamind.collector.bootstrap._fred_bootstrap_monthly") as mock_fred_monthly,
        patch("alphamind.collector.bootstrap.eia_bootstrap_series") as mock_eia,
        patch("alphamind.collector.bootstrap.bootstrap_auctions") as mock_treasury,
        patch("alphamind.collector.bootstrap.bls_bootstrap_series") as mock_bls,
        patch("alphamind.collector.bootstrap.bootstrap_earnings_calendar") as mock_earnings,
        patch("alphamind.collector.bootstrap.bootstrap_economic_calendar") as mock_econ,
    ):
        from alphamind.collector.bootstrap import run_all

        run_all(only_vendor="fred")

    mock_fred_daily.assert_called_once()
    mock_fred_monthly.assert_called_once()
    mock_seed.assert_not_called()
    mock_ref.assert_not_called()
    mock_corp.assert_not_called()
    mock_equity.assert_not_called()
    mock_eia.assert_not_called()
    mock_treasury.assert_not_called()
    mock_bls.assert_not_called()
    mock_earnings.assert_not_called()
    mock_econ.assert_not_called()


# ---------------------------------------------------------------------------
# Test 8: --only eia runs EIA only
# ---------------------------------------------------------------------------


def test_only_eia_runs_eia_only():
    with (
        patch("alphamind.collector.bootstrap._seed_asset_universe") as mock_seed,
        patch("alphamind.collector.bootstrap.collect_reference") as mock_ref,
        patch("alphamind.collector.bootstrap.bootstrap_corporate_actions") as mock_corp,
        patch("alphamind.collector.bootstrap.bootstrap_universe_bars") as mock_equity,
        patch("alphamind.collector.bootstrap._fred_bootstrap_daily") as mock_fred_daily,
        patch("alphamind.collector.bootstrap._fred_bootstrap_monthly") as mock_fred_monthly,
        patch("alphamind.collector.bootstrap.eia_bootstrap_series") as mock_eia,
        patch("alphamind.collector.bootstrap.bootstrap_auctions") as mock_treasury,
        patch("alphamind.collector.bootstrap.bls_bootstrap_series") as mock_bls,
        patch("alphamind.collector.bootstrap.bootstrap_earnings_calendar") as mock_earnings,
        patch("alphamind.collector.bootstrap.bootstrap_economic_calendar") as mock_econ,
    ):
        from alphamind.collector.bootstrap import run_all

        run_all(only_vendor="eia")

    mock_eia.assert_called_once()
    mock_seed.assert_not_called()
    mock_ref.assert_not_called()
    mock_corp.assert_not_called()
    mock_equity.assert_not_called()
    mock_fred_daily.assert_not_called()
    mock_fred_monthly.assert_not_called()
    mock_treasury.assert_not_called()
    mock_bls.assert_not_called()
    mock_earnings.assert_not_called()
    mock_econ.assert_not_called()


# ---------------------------------------------------------------------------
# Test 9: progress logging emits per-vendor start/end lines
# ---------------------------------------------------------------------------


def test_progress_logging_emits_start_and_end(caplog):
    with (
        patch("alphamind.collector.bootstrap._seed_asset_universe"),
        patch("alphamind.collector.bootstrap.collect_reference"),
        patch("alphamind.collector.bootstrap.bootstrap_corporate_actions"),
        patch("alphamind.collector.bootstrap.bootstrap_universe_bars"),
        patch("alphamind.collector.bootstrap._fred_bootstrap_daily"),
        patch("alphamind.collector.bootstrap._fred_bootstrap_monthly"),
        patch("alphamind.collector.bootstrap.eia_bootstrap_series"),
        patch("alphamind.collector.bootstrap.bootstrap_auctions"),
        patch("alphamind.collector.bootstrap.bls_bootstrap_series"),
        patch("alphamind.collector.bootstrap.bootstrap_earnings_calendar"),
        patch("alphamind.collector.bootstrap.bootstrap_economic_calendar"),
        patch("alphamind.collector.bootstrap.collect_ipo_calendar"),
        patch("alphamind.collector.bootstrap.collect_fda_calendar"),
        patch("alphamind.collector.bootstrap.bootstrap_estimate_revisions"),
        patch("alphamind.collector.bootstrap.bootstrap_short_volume"),
        patch("alphamind.collector.bootstrap.bootstrap_short_interest"),
    ):
        from alphamind.collector.bootstrap import run_all

        with caplog.at_level(logging.INFO):
            run_all()

    messages = [r.message for r in caplog.records]
    # Should see start and done markers for polygon.reference
    assert any("polygon.reference" in m and "start" in m.lower() for m in messages), (
        "expected start log for polygon.reference"
    )
    assert any(
        "polygon.reference" in m
        and ("done" in m.lower() or "complete" in m.lower() or "finish" in m.lower())
        for m in messages
    ), "expected done log for polygon.reference"


# ---------------------------------------------------------------------------
# Test 10: unknown only_vendor raises ValueError
# ---------------------------------------------------------------------------


def test_unknown_vendor_raises():
    with (
        patch("alphamind.collector.bootstrap._seed_asset_universe"),
        patch("alphamind.collector.bootstrap.collect_reference"),
        patch("alphamind.collector.bootstrap.bootstrap_corporate_actions"),
        patch("alphamind.collector.bootstrap.bootstrap_universe_bars"),
        patch("alphamind.collector.bootstrap._fred_bootstrap_daily"),
        patch("alphamind.collector.bootstrap._fred_bootstrap_monthly"),
        patch("alphamind.collector.bootstrap.eia_bootstrap_series"),
        patch("alphamind.collector.bootstrap.bootstrap_auctions"),
        patch("alphamind.collector.bootstrap.bls_bootstrap_series"),
        patch("alphamind.collector.bootstrap.bootstrap_earnings_calendar"),
        patch("alphamind.collector.bootstrap.bootstrap_economic_calendar"),
    ):
        from alphamind.collector.bootstrap import run_all

        with pytest.raises(ValueError, match="unknown vendor"):
            run_all(only_vendor="nonexistent_vendor")
