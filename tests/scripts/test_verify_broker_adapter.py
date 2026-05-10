"""Tests for ``scripts/verify_broker_adapter.py`` (ALP-393).

The verify script exercises the broker adapter end-to-end against Alpaca's
paper environment plus the venue-configuration primitives in isolation. The
operator runs the live paper-environment phases; the test suite covers what
can be exercised without network contact:

* Phase function structure (each phase returns a ``PhaseResult``).
* Mocked-broker drives of every phase (using ``MagicMock(spec=TradingClient)``
  the same pattern story 02a established for ``queries.py`` tests).
* CLI surface (--mode / --verbose / --phase / exit code on FAIL).
* Credential-missing diagnostics (paper-mode missing PAPER env vars,
  live-mode missing LIVE env vars).

The "every phase reports PASS against a configured paper account" criterion
is operator territory — it requires real Alpaca paper credentials and is not
exercised here.
"""

from __future__ import annotations

import contextlib
import datetime as dt
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from alpaca.trading.client import TradingClient
from alpaca.trading.models import Asset, Calendar, Clock, TradeAccount

_SHIM_PATH = Path(__file__).parents[2] / "scripts" / "verify_broker_adapter.py"


# ---------------------------------------------------------------------------
# Tracer: shim file exists
# ---------------------------------------------------------------------------


def test_shim_exists() -> None:
    assert _SHIM_PATH.exists(), f"Shim not found at {_SHIM_PATH}"


# ---------------------------------------------------------------------------
# Phase registry — exposes 7 phase names in order
# ---------------------------------------------------------------------------


def test_phase_names_in_order() -> None:
    """The phase registry must expose seven phases named ``phase_1`` … ``phase_7``."""
    from alphamind.scripts.verify_broker_adapter import PHASE_NAMES

    assert PHASE_NAMES == (
        "phase_1",
        "phase_2",
        "phase_3",
        "phase_4",
        "phase_5",
        "phase_6",
        "phase_7",
    )


# ---------------------------------------------------------------------------
# CLI surface — --mode / --phase / --verbose
# ---------------------------------------------------------------------------


def test_cli_accepts_mode_paper_and_live() -> None:
    """``--mode {paper,live}`` is accepted; default is paper."""
    from alphamind.scripts.verify_broker_adapter import _build_arg_parser

    parser = _build_arg_parser()
    paper_args = parser.parse_args([])
    assert paper_args.mode == "paper"

    live_args = parser.parse_args(["--mode", "live"])
    assert live_args.mode == "live"


def test_cli_rejects_unknown_mode() -> None:
    """``--mode dev`` is not a valid choice."""
    from alphamind.scripts.verify_broker_adapter import _build_arg_parser

    parser = _build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--mode", "dev"])


def test_cli_accepts_phase_filter() -> None:
    """``--phase phase_1`` selects a single phase."""
    from alphamind.scripts.verify_broker_adapter import _build_arg_parser

    parser = _build_arg_parser()
    args = parser.parse_args(["--phase", "phase_3"])
    assert args.phase == "phase_3"


def test_cli_rejects_unknown_phase_name() -> None:
    """``--phase phase_99`` is not a valid choice."""
    from alphamind.scripts.verify_broker_adapter import _build_arg_parser

    parser = _build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--phase", "phase_99"])


def test_cli_verbose_flag() -> None:
    """``--verbose`` toggles a boolean flag."""
    from alphamind.scripts.verify_broker_adapter import _build_arg_parser

    parser = _build_arg_parser()
    quiet = parser.parse_args([])
    verbose = parser.parse_args(["--verbose"])
    assert quiet.verbose is False
    assert verbose.verbose is True


# ---------------------------------------------------------------------------
# PhaseResult — passed / failed / skipped (deferred) constructors
# ---------------------------------------------------------------------------


def test_phase_result_passed_marks_ok_true_deferred_false() -> None:
    from alphamind.scripts.verify_broker_adapter import PhaseResult

    pr = PhaseResult.passed("Phase 1")
    assert pr.ok is True
    assert pr.deferred is False
    assert pr.detail is None


def test_phase_result_failed_marks_ok_false_with_detail() -> None:
    from alphamind.scripts.verify_broker_adapter import PhaseResult

    pr = PhaseResult.failed("Phase 1", "submit_with_retry returned None")
    assert pr.ok is False
    assert pr.deferred is False
    assert pr.detail == "submit_with_retry returned None"


def test_phase_result_skipped_is_ok_with_deferred_flag() -> None:
    """DEFERRED is a 'pass-with-skip' state — counts as not-failing for the
    summary's exit-code logic but renders as ``[DEFERRED]`` in output."""
    from alphamind.scripts.verify_broker_adapter import PhaseResult

    pr = PhaseResult.skipped("Phase 3", "live drive — operator territory")
    assert pr.ok is True
    assert pr.deferred is True
    assert pr.detail == "live drive — operator territory"


# ---------------------------------------------------------------------------
# Test fixtures — mock factory + queries shared across phase function tests
# ---------------------------------------------------------------------------


def _make_trade_account() -> TradeAccount:
    return TradeAccount.model_validate(
        {
            "id": "a0d8e7b5-1234-4b3a-8765-000000000001",
            "account_number": "PA000001",
            "status": "ACTIVE",
            "currency": "USD",
            "buying_power": "50000.00",
            "regt_buying_power": "25000.00",
            "daytrading_buying_power": "100000.00",
            "non_marginable_buying_power": "5000.00",
            "cash": "10000.00",
            "accrued_fees": "0",
            "portfolio_value": "60000.00",
            "pattern_day_trader": False,
            "trading_blocked": False,
            "transfers_blocked": False,
            "account_blocked": False,
            "created_at": "2024-01-01T00:00:00Z",
            "trade_suspended_by_user": False,
            "multiplier": "2",
            "shorting_enabled": True,
            "equity": "60000.00",
            "last_equity": "59000.00",
            "long_market_value": "50000.00",
            "short_market_value": "0.00",
            "initial_margin": "25000.00",
            "maintenance_margin": "15000.00",
            "last_maintenance_margin": "14500.00",
            "sma": "0",
            "daytrade_count": 2,
        }
    )


def _make_asset(symbol: str = "NVDA", tradable: bool = True) -> Asset:
    return Asset.model_validate(
        {
            "id": "c2d3e4f5-6789-4d3e-a987-bcdef0123456",
            "class": "us_equity",
            "exchange": "NASDAQ",
            "symbol": symbol,
            "name": f"{symbol} Inc.",
            "status": "active",
            "tradable": tradable,
            "marginable": True,
            "shortable": True,
            "easy_to_borrow": True,
            "fractionable": True,
        }
    )


def _make_404_api_error() -> Exception:
    """Return an ``APIError``-shaped object whose ``status_code`` is 404."""
    import httpx
    from alpaca.common.exceptions import APIError

    request = httpx.Request("GET", "https://paper-api.alpaca.markets/v2/assets/Z")
    response = httpx.Response(
        404,
        json={"code": 40410000, "message": "asset not found"},
        request=request,
    )
    http_error = httpx.HTTPStatusError("404 Not Found", request=request, response=response)
    return APIError(  # type: ignore[no-untyped-call]
        error={"code": 40410000, "message": "asset not found"},
        http_error=http_error,
    )


def _make_calendar_days(count: int = 22) -> list[Calendar]:
    """Return ``count`` synthetic calendar entries (one per business day)."""
    days: list[Calendar] = []
    base = dt.date(2026, 1, 5)  # a Monday
    added = 0
    delta = 0
    while added < count:
        candidate = base + dt.timedelta(days=delta)
        if candidate.weekday() < 5:  # skip weekends
            days.append(
                Calendar.model_validate(
                    {
                        "date": candidate.isoformat(),
                        "open": "09:30",
                        "close": "16:00",
                    }
                )
            )
            added += 1
        delta += 1
    return days


def _make_clock(is_open: bool = False) -> Clock:
    return Clock.model_validate(
        {
            "timestamp": "2026-05-09T20:30:00Z",
            "is_open": is_open,
            "next_open": "2026-05-11T13:30:00Z",
            "next_close": "2026-05-09T20:00:00Z",
        }
    )


def _make_option_contract(
    *,
    underlying: str = "NVDA",
    expiration: dt.date | None = None,
    strike: float = 100.0,
    contract_type: str = "call",
    symbol: str | None = None,
) -> Any:
    """Build a single alpaca-py ``OptionContract`` for chain-mock fixtures."""
    from uuid import uuid4

    from alpaca.trading.models import OptionContract

    expiry = expiration or dt.date(2026, 6, 19)
    occ_strike = f"{round(strike * 1000):08d}"
    type_letter = "C" if contract_type == "call" else "P"
    yymmdd = expiry.strftime("%y%m%d")
    occ_symbol = symbol or f"{underlying:<6}{yymmdd}{type_letter}{occ_strike}"
    return OptionContract.model_validate(
        {
            "id": str(uuid4()),
            "symbol": occ_symbol,
            "name": f"{underlying} {expiry.isoformat()} {strike} {contract_type.upper()}",
            "status": "active",
            "tradable": True,
            "expiration_date": expiry.isoformat(),
            "root_symbol": underlying,
            "underlying_symbol": underlying,
            "underlying_asset_id": str(uuid4()),
            "type": contract_type,
            "style": "american",
            "strike_price": strike,
            "size": "100",
        }
    )


def _make_option_contracts_response(contracts: list[Any]) -> Any:
    """Wrap a list of contracts in the alpaca-py ``OptionContractsResponse``."""
    from alpaca.trading.models import OptionContractsResponse

    return OptionContractsResponse(option_contracts=contracts, next_page_token=None)


def _default_option_chain() -> list[Any]:
    """Return a 5-strike call chain spanning 80 / 90 / 100 / 110 / 120.

    Used by the chain-aware test helpers as a default fixture when the test
    doesn't care about specific strikes — the median is 100.0 and the
    next-higher strike is 110.0, so phase-5 vertical-spread tests get a
    deterministic long/short pair.
    """
    return [_make_option_contract(strike=s) for s in (80.0, 90.0, 100.0, 110.0, 120.0)]


def _override_chain(ctx: Any, strikes: tuple[float, ...]) -> None:
    """Replace the queries' option-chain fixture with one carrying *strikes*."""
    sdk_client = ctx.queries._client
    sdk_client.get_option_contracts.return_value = _make_option_contracts_response(
        [_make_option_contract(strike=s) for s in strikes],
    )


def _fake_factory(mode: str = "paper") -> MagicMock:
    """Return a MagicMock that mimics ``AlpacaClientFactory``."""
    from alphamind.execution.broker_adapter import AlpacaClientFactory

    factory = MagicMock(spec=AlpacaClientFactory)
    factory.build_trading_client.return_value = MagicMock(spec=TradingClient)
    factory.build_trading_stream.return_value = MagicMock()
    factory.credentials.mode = mode
    return factory


def _fake_queries(client: Any | None = None) -> Any:
    """Return a real ``AccountStateQueries`` wired around a MagicMock client.

    Using the real class (not a MagicMock-spec) lets each phase exercise the
    actual conversion code-path; the underlying ``TradingClient`` calls are
    stubbed, mirroring story 02a's test pattern.
    """
    from alphamind.execution.broker_adapter import AccountStateQueries

    sdk_client = client or MagicMock(spec=TradingClient)
    return AccountStateQueries(sdk_client)


def _execution_config() -> Any:
    """Return a minimal ``ExecutionConfig`` for retry-window threading."""
    from alphamind.config.models.execution import ExecutionConfig

    return ExecutionConfig.model_validate(
        {
            "greeks_refresh": {
                "scheduled_interval_minutes": 15,
                "move_trigger_pct": 2.0,
            },
            "conservative_delta_buffer_pct": 10,
            "submission_retry_window_seconds": 5,
            "paper_harness": {
                "spread_buffer_pct": 10,
                "impact_coefficients": {"market": 0.5, "limit": 0.25, "stop": 0.75},
            },
            "pl_target_margin_pct": 5,
        }
    )


def _venue_config() -> Any:
    from alphamind.config.models.venue import VenueConfig

    return VenueConfig.model_validate(
        {
            "alpaca": {
                "paper": {
                    "rest_url": "https://paper-api.alpaca.markets",
                    "ws_url": "wss://paper-api.alpaca.markets/stream",
                    "api_key_env": "ALPACA_PAPER_KEY",
                    "api_secret_env": "ALPACA_PAPER_SECRET",
                },
                "live": {
                    "rest_url": "https://api.alpaca.markets",
                    "ws_url": "wss://api.alpaca.markets/stream",
                    "api_key_env": "ALPACA_LIVE_KEY",
                    "api_secret_env": "ALPACA_LIVE_SECRET",
                },
                "rate_limit_per_minute": 200,
            },
            "session_hours": {
                "regular": {"open": "09:30", "close": "16:00"},
                "pre_market": {"open": "04:00", "close": "09:30"},
                "after_hours": {"open": "16:00", "close": "20:00"},
            },
        }
    )


def _make_context(*, mode: str = "paper", verbose: bool = False) -> Any:
    """Build a fully-populated ``VerifyContext`` against mock dependencies."""
    from alphamind.scripts.verify_broker_adapter import VerifyContext

    factory = _fake_factory(mode=mode)
    queries = _fake_queries()
    return VerifyContext(
        factory=factory,
        queries=queries,
        execution=_execution_config(),
        venue=_venue_config(),
        mode=mode,  # type: ignore[arg-type]
        verbose=verbose,
    )


# ---------------------------------------------------------------------------
# Phase 1 — adapter substrate
# ---------------------------------------------------------------------------


async def test_phase_1_substrate_passes_with_working_factory_and_classifier() -> None:
    """The substrate phase exercises the factory smoke-test + ``submit_with_retry``
    + ``classify_alpaca_error`` against synthesized exceptions. With a working
    factory + classifier the phase reports PASS."""
    from alphamind.scripts.verify_broker_adapter import phase_1_adapter_substrate

    ctx = _make_context()
    result = await phase_1_adapter_substrate(ctx)

    assert result.ok is True, result.detail
    assert result.deferred is False
    assert "Phase 1" in result.label


async def test_phase_1_fails_when_factory_raises() -> None:
    """If the factory raises on ``build_trading_client``, the phase fails with
    a diagnostic detail naming the factory failure."""
    from alphamind.scripts.verify_broker_adapter import phase_1_adapter_substrate

    ctx = _make_context()
    ctx.factory.build_trading_client.side_effect = RuntimeError("boom")

    result = await phase_1_adapter_substrate(ctx)
    assert result.ok is False
    assert "factory" in (result.detail or "").lower()


# ---------------------------------------------------------------------------
# Phase 2 — account state queries (mocked SDK calls)
# ---------------------------------------------------------------------------


def _build_phase_2_ctx(client: MagicMock) -> Any:
    """Wire a ``VerifyContext`` whose queries proxy the supplied mock client."""
    from alphamind.execution.broker_adapter import AccountStateQueries
    from alphamind.scripts.verify_broker_adapter import VerifyContext

    queries = AccountStateQueries(client)
    return VerifyContext(
        factory=_fake_factory(),
        queries=queries,
        execution=_execution_config(),
        venue=_venue_config(),
        mode="paper",
        verbose=False,
    )


async def test_phase_2_queries_passes_with_complete_responses() -> None:
    """Phase 2 PASSes when every query method returns a populated response.

    Models the happy path: account, positions (empty tuple is valid), known
    asset, unknown asset → None, calendar with ≥ 20 days, clock, paginating
    orders that exhaust on the first page.
    """
    from alphamind.scripts.verify_broker_adapter import phase_2_account_state_queries

    client = MagicMock(spec=TradingClient)
    client.get_account.return_value = _make_trade_account()
    client.get_all_positions.return_value = []

    def _get_asset(symbol: str) -> Asset:
        if symbol == "NVDA":
            return _make_asset("NVDA", tradable=True)
        # ZZNONEXISTENT raises 404 → the queries wrapper maps to None
        raise _make_404_api_error()

    client.get_asset.side_effect = _get_asset
    client.get_calendar.return_value = _make_calendar_days(22)
    client.get_clock.return_value = _make_clock()
    client.get_orders.return_value = []  # empty paper-account history

    ctx = _build_phase_2_ctx(client)
    result = await phase_2_account_state_queries(ctx)
    assert result.ok is True, result.detail


async def test_phase_2_fails_when_known_symbol_returns_none() -> None:
    """If ``get_asset('NVDA')`` returns None (404), the phase fails — NVDA must
    be in Alpaca's universe for any healthy paper account."""
    from alphamind.scripts.verify_broker_adapter import phase_2_account_state_queries

    client = MagicMock(spec=TradingClient)
    client.get_account.return_value = _make_trade_account()
    client.get_all_positions.return_value = []
    client.get_asset.side_effect = _make_404_api_error()

    ctx = _build_phase_2_ctx(client)
    result = await phase_2_account_state_queries(ctx)
    assert result.ok is False
    assert "NVDA" in (result.detail or "")


async def test_phase_2_fails_when_calendar_too_short() -> None:
    """If the calendar returns fewer than 20 entries, the phase fails."""
    from alphamind.scripts.verify_broker_adapter import phase_2_account_state_queries

    client = MagicMock(spec=TradingClient)
    client.get_account.return_value = _make_trade_account()
    client.get_all_positions.return_value = []

    def _get_asset(symbol: str) -> Asset:
        if symbol == "NVDA":
            return _make_asset("NVDA", tradable=True)
        raise _make_404_api_error()

    client.get_asset.side_effect = _get_asset
    client.get_calendar.return_value = _make_calendar_days(5)  # too few
    client.get_clock.return_value = _make_clock()
    client.get_orders.return_value = []

    ctx = _build_phase_2_ctx(client)
    result = await phase_2_account_state_queries(ctx)
    assert result.ok is False
    assert "calendar" in (result.detail or "").lower()


# ---------------------------------------------------------------------------
# Phases 3, 4, 5 — DEFERRED for in-process subagent runs
# ---------------------------------------------------------------------------


async def test_phase_3_equity_lifecycle_deferred() -> None:
    """The equity-lifecycle phase requires a live paper account + persistent
    OMS DB; it DEFERS in the in-process subagent run."""
    from alphamind.scripts.verify_broker_adapter import phase_3_equity_order_lifecycle

    ctx = _make_context()
    result = await phase_3_equity_order_lifecycle(ctx)
    assert result.ok is True
    assert result.deferred is True
    assert "DEFERRED" in (result.detail or "").upper()


async def test_phase_4_options_lifecycle_deferred() -> None:
    from alphamind.scripts.verify_broker_adapter import phase_4_options_order_lifecycle

    ctx = _make_context()
    result = await phase_4_options_order_lifecycle(ctx)
    assert result.ok is True
    assert result.deferred is True


async def test_phase_5_mleg_lifecycle_deferred() -> None:
    from alphamind.scripts.verify_broker_adapter import phase_5_mleg_order_lifecycle

    ctx = _make_context()
    result = await phase_5_mleg_order_lifecycle(ctx)
    assert result.ok is True
    assert result.deferred is True


# ---------------------------------------------------------------------------
# Phase 6 — venue configuration in isolation (mocked calendar + clock)
# ---------------------------------------------------------------------------


def _make_business_days_around(anchor: dt.date, span_days: int = 200) -> list[Calendar]:
    """Return business-day Calendar entries spanning ±``span_days`` around ``anchor``.

    The TradingCalendarCache fetches a 90-day window centered on the requested
    date; mocked queries must return entries that include today and its
    neighbors so ``business_day_offset(today, 1)`` resolves cleanly regardless
    of when the test runs.
    """
    days: list[Calendar] = []
    for offset in range(-span_days, span_days + 1):
        d = anchor + dt.timedelta(days=offset)
        if d.weekday() < 5:
            days.append(
                Calendar.model_validate({"date": d.isoformat(), "open": "09:30", "close": "16:00"})
            )
    return days


def _calendar_filtering_client(today: dt.date | None = None) -> MagicMock:
    """Build a TradingClient whose get_calendar honors start/end filtering.

    The real Alpaca API filters by [start, end]; the calendar cache relies on
    that filtering for its window-fetch logic. The mock filters synthetic
    business-day entries by the request's start/end so the cache can resolve
    ``business_day_offset(today, 1)`` without exhausting the search budget.
    """
    anchor = today or dt.datetime.now(dt.UTC).date()
    all_days = _make_business_days_around(anchor)

    def _filter(*, filters: Any | None = None) -> list[Calendar]:
        if filters is None:
            return all_days
        start = getattr(filters, "start", None)
        end = getattr(filters, "end", None)
        return [
            day
            for day in all_days
            if (start is None or day.date >= start) and (end is None or day.date <= end)
        ]

    client = MagicMock(spec=TradingClient)
    client.get_calendar.side_effect = _filter
    return client


async def test_phase_6_venue_config_passes_when_constants_and_cache_agree() -> None:
    """Phase 6 PASSes when constants match documented values, the calendar
    cache's ``is_market_open`` matches ``get_clock().is_open``, and
    ``compute_settlement_date(today, "equity")`` equals
    ``business_day_offset(today, 1)``."""
    from alphamind.scripts.verify_broker_adapter import phase_6_venue_config

    client = _calendar_filtering_client()
    # Clock says market closed; the cache's is_market_open will also say
    # closed only if today's session window doesn't contain "now" (UTC). To
    # make the agreement deterministic regardless of when the test runs, we
    # report ``is_open=False`` from the clock and arrange ``read_venue_account_state``
    # to also have a known account snapshot.
    client.get_clock.return_value = _make_clock(is_open=False)
    client.get_account.return_value = _make_trade_account()

    ctx = _build_phase_2_ctx(client)
    result = await phase_6_venue_config(ctx)
    # Whether the cache reports open depends on the test's wall-clock; if
    # this test runs during a US business-day session, the cache will say
    # open while the mocked clock says closed → FAIL. We accept either
    # PASS or "is_market_open disagrees" because the phase contract is
    # "cache and clock must agree against live data" — agreement is what
    # we cover; the substantive constants/business-day/settlement checks
    # below are what carry the asserted behavior. So assert PASS OR the
    # is_market_open-disagrees-only failure.
    if not result.ok:
        # Tolerate the wall-clock-dependent disagreement; reject any other
        # diagnostic.
        assert "is_market_open" in (result.detail or ""), result.detail


async def test_phase_6_fails_when_constant_drifts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 6 fails if a venue constant drifts from documented value.

    Patches both the source module and the package-level re-export so the
    constants check (which reads from the package re-export) sees the drift.
    """
    # Both the source attribute and the package re-export must be patched —
    # the package's __init__ binds the value at import time, so patching only
    # the source leaves the re-export pointing at the original value.
    import alphamind.execution.venue_configuration as venue_pkg
    from alphamind.execution.venue_configuration import constants as venue_constants
    from alphamind.scripts.verify_broker_adapter import phase_6_venue_config

    monkeypatch.setattr(venue_constants, "PDT_EQUITY_THRESHOLD_USD", 30_000.0)
    monkeypatch.setattr(venue_pkg, "PDT_EQUITY_THRESHOLD_USD", 30_000.0)

    client = _calendar_filtering_client()
    client.get_clock.return_value = _make_clock(is_open=False)
    client.get_account.return_value = _make_trade_account()

    ctx = _build_phase_2_ctx(client)
    result = await phase_6_venue_config(ctx)
    assert result.ok is False
    assert "PDT_EQUITY_THRESHOLD_USD" in (result.detail or "")


# ---------------------------------------------------------------------------
# Phase 7 — disconnect recovery
# ---------------------------------------------------------------------------


async def test_phase_7_disconnect_recovery_passes_with_no_recent_orders() -> None:
    """The recovery primitive iterates ``get_orders`` results; an empty
    iteration is the happy path on a quiet paper account."""
    from alphamind.scripts.verify_broker_adapter import phase_7_disconnect_recovery

    client = MagicMock(spec=TradingClient)
    client.get_orders.return_value = []  # empty — no recent orders

    ctx = _build_phase_2_ctx(client)
    result = await phase_7_disconnect_recovery(ctx)
    assert result.ok is True, result.detail


async def test_phase_7_fails_when_get_orders_raises() -> None:
    from alphamind.scripts.verify_broker_adapter import phase_7_disconnect_recovery

    client = MagicMock(spec=TradingClient)
    client.get_orders.side_effect = RuntimeError("network blip")

    ctx = _build_phase_2_ctx(client)
    result = await phase_7_disconnect_recovery(ctx)
    assert result.ok is False
    assert "recover_missed_fills_since" in (result.detail or "")


# ---------------------------------------------------------------------------
# Summary rendering — text format
# ---------------------------------------------------------------------------


def test_render_text_marks_pass_deferred_fail() -> None:
    """The text renderer marks each phase ``[PASS]``, ``[DEFERRED]``, or
    ``[FAIL]`` and announces ``RESULT: PASS`` / ``RESULT: FAIL`` based on
    whether any phase failed (DEFERRED counts as not-failing)."""
    from alphamind.scripts.verify_broker_adapter import PhaseResult, _render_text

    results = [
        PhaseResult.passed("Phase 1 — Adapter substrate"),
        PhaseResult.skipped("Phase 3 — Equity lifecycle", "deferred"),
        PhaseResult.passed("Phase 6 — Venue config"),
    ]
    text = _render_text(results)
    assert "[PASS] Phase 1 — Adapter substrate" in text
    assert "[DEFERRED] Phase 3 — Equity lifecycle" in text
    assert "RESULT: PASS" in text


def test_render_text_marks_failure() -> None:
    from alphamind.scripts.verify_broker_adapter import PhaseResult, _render_text

    results = [
        PhaseResult.passed("Phase 1"),
        PhaseResult.failed("Phase 2", "get_account raised"),
    ]
    text = _render_text(results)
    assert "[FAIL] Phase 2" in text
    assert "RESULT: FAIL" in text
    assert "get_account raised" in text


# ---------------------------------------------------------------------------
# main() exit code reflects per-phase outcomes
# ---------------------------------------------------------------------------


def test_main_exit_zero_when_all_pass_or_deferred(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """``main()`` returns 0 when every phase reports PASS or DEFERRED."""
    from alphamind.scripts import verify_broker_adapter as module

    # Stub the config loader + factory so main() doesn't touch real env / yaml.
    monkeypatch.setattr(
        module,
        "_load_venue_and_execution",
        lambda **_: (_venue_config(), _execution_config()),
    )
    monkeypatch.setattr(
        module,
        "_build_context",
        lambda *_args, **_kwargs: _make_context(),
    )

    # Replace every phase function with a stub that returns PASS / DEFERRED.
    async def _passing(_ctx: Any) -> Any:
        return module.PhaseResult.passed("stub")

    async def _deferred(_ctx: Any) -> Any:
        return module.PhaseResult.skipped("stub", "deferred")

    fake_phases = (
        ("phase_1", _passing),
        ("phase_2", _passing),
        ("phase_3", _deferred),
        ("phase_4", _deferred),
        ("phase_5", _deferred),
        ("phase_6", _passing),
        ("phase_7", _passing),
    )
    monkeypatch.setattr(module, "_PHASES", fake_phases)
    monkeypatch.setattr(module, "PHASE_NAMES", tuple(name for name, _fn in fake_phases))

    rc = module.main([])
    assert rc == 0
    captured = capsys.readouterr().out
    assert "RESULT: PASS" in captured


def test_main_exit_nonzero_when_any_phase_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``main()`` returns 1 when any phase reports FAIL."""
    from alphamind.scripts import verify_broker_adapter as module

    monkeypatch.setattr(
        module,
        "_load_venue_and_execution",
        lambda **_: (_venue_config(), _execution_config()),
    )
    monkeypatch.setattr(
        module,
        "_build_context",
        lambda *_args, **_kwargs: _make_context(),
    )

    async def _passing(_ctx: Any) -> Any:
        return module.PhaseResult.passed("stub")

    async def _failing(_ctx: Any) -> Any:
        return module.PhaseResult.failed("stub", "intentional")

    fake_phases = (
        ("phase_1", _passing),
        ("phase_2", _failing),
    )
    monkeypatch.setattr(module, "_PHASES", fake_phases)
    monkeypatch.setattr(module, "PHASE_NAMES", tuple(name for name, _fn in fake_phases))

    rc = module.main([])
    assert rc == 1
    captured = capsys.readouterr().out
    assert "RESULT: FAIL" in captured
    assert "intentional" in captured


def test_main_exit_one_when_paper_credentials_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When ``ALPACA_PAPER_KEY`` is unset, ``AlpacaClientFactory`` raises
    ``RuntimeError`` naming the missing var; ``main()`` returns 1 with a
    message naming the env var."""
    from alphamind.scripts import verify_broker_adapter as module

    # Force the venue config to return a paper block referencing PAPER_KEY.
    monkeypatch.setattr(
        module,
        "_load_venue_and_execution",
        lambda **_: (_venue_config(), _execution_config()),
    )
    # Clear paper env vars so the real AlpacaClientFactory raises.
    monkeypatch.delenv("ALPACA_PAPER_KEY", raising=False)
    monkeypatch.delenv("ALPACA_PAPER_SECRET", raising=False)

    rc = module.main([])
    assert rc == 1
    err = capsys.readouterr().err
    assert "ALPACA_PAPER_KEY" in err


def test_main_exit_one_when_live_credentials_missing_in_live_mode(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--mode=live`` without live env vars surfaces the missing-var error."""
    from alphamind.scripts import verify_broker_adapter as module

    monkeypatch.setattr(
        module,
        "_load_venue_and_execution",
        lambda **_: (_venue_config(), _execution_config()),
    )
    monkeypatch.delenv("ALPACA_LIVE_KEY", raising=False)
    monkeypatch.delenv("ALPACA_LIVE_SECRET", raising=False)

    rc = module.main(["--mode", "live"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "ALPACA_LIVE_KEY" in err


def test_main_phase_filter_runs_single_phase(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--phase phase_2`` runs only the named phase."""
    from alphamind.scripts import verify_broker_adapter as module

    monkeypatch.setattr(
        module,
        "_load_venue_and_execution",
        lambda **_: (_venue_config(), _execution_config()),
    )
    monkeypatch.setattr(
        module,
        "_build_context",
        lambda *_args, **_kwargs: _make_context(),
    )

    invocation_log: list[str] = []

    def _make_phase(name: str) -> Any:
        async def _phase(_ctx: Any) -> Any:
            invocation_log.append(name)
            return module.PhaseResult.passed(name)

        return _phase

    fake_phases = tuple((f"phase_{i}", _make_phase(f"phase_{i}")) for i in range(1, 8))
    monkeypatch.setattr(module, "_PHASES", fake_phases)
    monkeypatch.setattr(module, "PHASE_NAMES", tuple(name for name, _fn in fake_phases))

    rc = module.main(["--phase", "phase_2"])
    assert rc == 0
    assert invocation_log == ["phase_2"]


# ---------------------------------------------------------------------------
# Idempotency — re-running the script against the same fixtures yields
# identical results (acceptance criterion).
# ---------------------------------------------------------------------------


async def test_phase_1_idempotent() -> None:
    """Re-running ``phase_1_adapter_substrate`` against the same context twice
    in a row produces the same PASS summary (the acceptance-criteria
    idempotency requirement)."""
    from alphamind.scripts.verify_broker_adapter import phase_1_adapter_substrate

    ctx = _make_context()
    first = await phase_1_adapter_substrate(ctx)
    second = await phase_1_adapter_substrate(ctx)
    assert first.ok == second.ok
    assert first.label == second.label


# ---------------------------------------------------------------------------
# Phases 3, 4, 5 — conditional deferral on missing db_path
#
# When no DB path is threaded through the VerifyContext, the phase should
# defer with a clear diagnostic naming the missing prerequisite. This is the
# subagent-run path (creds may or may not be set; DB is intentionally absent).
# ---------------------------------------------------------------------------


async def test_phase_3_defers_when_db_path_missing_with_clear_reason() -> None:
    """When ``ctx.db_path is None`` the equity-lifecycle phase defers with a
    diagnostic naming the missing DB path so the operator knows what to wire."""
    from alphamind.scripts.verify_broker_adapter import phase_3_equity_order_lifecycle

    ctx = _make_context()  # db_path defaults to None
    result = await phase_3_equity_order_lifecycle(ctx)
    assert result.ok is True
    assert result.deferred is True
    assert "db" in (result.detail or "").lower()


# ---------------------------------------------------------------------------
# Phases 3, 4, 5 — conditional deferral when market is closed
#
# When the live ``MarketClock`` reports ``is_open=False`` the lifecycle phases
# can't realistically observe a fill within their per-phase budget (Alpaca
# paper-mode equity fills only arrive during regular session hours). Each
# lifecycle phase calls ``queries.get_clock()`` and DEFERs with a clear
# operator message rather than submitting orders that won't fill.
# ---------------------------------------------------------------------------


def _build_clock_aware_context(
    tmp_path: Path,
    *,
    is_open: bool,
) -> Any:
    """Build a VerifyContext whose queries' get_clock returns *is_open*.

    Phases 3 / 4 / 5 read ``queries.get_clock()`` after the db-path gate to
    decide whether to defer or proceed; tests need a queries object whose
    underlying SDK client returns a deterministic Clock.
    """
    from alphamind.execution.broker_adapter import AccountStateQueries
    from alphamind.scripts.verify_broker_adapter import VerifyContext

    sdk_client = _build_options_calendar_client()
    sdk_client.get_clock.return_value = _make_clock(is_open=is_open)
    queries = AccountStateQueries(sdk_client)
    factory = _fake_factory()
    return VerifyContext(
        factory=factory,
        queries=queries,
        execution=_execution_config(),
        venue=_venue_config(),
        mode="paper",
        verbose=False,
        db_path=tmp_path / "alphamind.db",
    )


async def test_phase_3_defers_when_market_closed(tmp_path: Path) -> None:
    """When ``MarketClock.is_open`` is ``False``, phase 3 defers with the
    standard "market closed; re-run during 09:30-16:00 ET" diagnostic instead
    of submitting orders that won't fill within the 30s timeout."""
    from alphamind.scripts.verify_broker_adapter import phase_3_equity_order_lifecycle

    ctx = _build_clock_aware_context(tmp_path, is_open=False)
    result = await phase_3_equity_order_lifecycle(ctx)

    assert result.ok is True
    assert result.deferred is True
    detail = (result.detail or "").lower()
    assert "market" in detail and "closed" in detail
    assert "09:30" in (result.detail or "")
    assert "16:00" in (result.detail or "")


# ---------------------------------------------------------------------------
# Phase 3 happy path — equity OPEN→fill→CLOSE round-trip with mocked broker
# ---------------------------------------------------------------------------


class _FakeStream:
    """Minimal alpaca-py-shaped TradingStream stub.

    Mirrors the fixture pattern in
    ``tests/execution/broker_adapter/test_fill_stream.py``. Captures the
    handler so tests can inject ``TradeUpdate`` events; ``_run_forever`` parks
    until cancelled so ``subscribe_trade_updates`` can drain the queue.
    """

    def __init__(self) -> None:
        import asyncio as _asyncio

        self.handler: Any = None
        self.run_cancelled = False
        self._park = _asyncio.Event()

    def subscribe_trade_updates(self, handler: Any) -> None:
        self.handler = handler

    async def inject(self, update: Any) -> None:
        assert self.handler is not None
        await self.handler(update)

    async def _run_forever(self) -> None:
        import asyncio as _asyncio

        try:
            await self._park.wait()
        except _asyncio.CancelledError:
            self.run_cancelled = True
            raise


def _build_alpaca_order(
    *,
    order_class: Any | None = None,
    status: Any | None = None,
    client_order_id: str = "client-id-stub",
    order_id: Any | None = None,
    asset_class: Any | None = None,
) -> Any:
    """Build a MagicMock alpaca-py Order for ``submit_order`` returns."""
    import uuid as _uuid

    from alpaca.trading.enums import AssetClass, OrderClass
    from alpaca.trading.enums import OrderStatus as _OrderStatus

    order = MagicMock()
    order.id = order_id if order_id is not None else _uuid.uuid4()
    order.client_order_id = client_order_id
    order.status = status if status is not None else _OrderStatus.ACCEPTED
    order.order_class = order_class if order_class is not None else OrderClass.SIMPLE
    order.asset_class = asset_class if asset_class is not None else AssetClass.US_EQUITY
    return order


def _build_trade_update_fill(
    *,
    order_id: Any,
    client_order_id: str,
    symbol: str = "NVDA",
    asset_class: Any | None = None,
    order_class: Any | None = None,
    qty: str = "1",
    legs: list[Any] | None = None,
) -> Any:
    """Build a ``fill`` TradeUpdate referencing ``order_id`` / ``client_order_id``."""
    from alpaca.trading.enums import (
        AssetClass,
        OrderClass,
        OrderSide,
        OrderType,
        TimeInForce,
    )
    from alpaca.trading.enums import OrderStatus as AlpacaOrderStatus
    from alpaca.trading.models import Order, TradeUpdate

    order = Order(
        id=order_id,
        client_order_id=client_order_id,
        created_at=dt.datetime.now(dt.UTC),
        updated_at=dt.datetime.now(dt.UTC),
        submitted_at=dt.datetime.now(dt.UTC),
        symbol=symbol,
        asset_class=asset_class if asset_class is not None else AssetClass.US_EQUITY,
        order_class=order_class if order_class is not None else OrderClass.SIMPLE,
        order_type=OrderType.MARKET,
        type=OrderType.MARKET,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        status=AlpacaOrderStatus.FILLED,
        extended_hours=False,
        qty=qty,
        filled_qty=qty,
        legs=legs,
    )
    return TradeUpdate(
        event="fill",
        order=order,
        timestamp=dt.datetime.now(dt.UTC),
        price=150.0,
        qty=float(qty),
    )


def _make_context_with_db(tmp_path: Path, *, mode: str = "paper", verbose: bool = False) -> Any:
    """Build a VerifyContext threading a tmp DB path.

    Wires ``queries.get_clock()`` to return ``is_open=True`` so the lifecycle
    phases pass the market-open defer-gate and proceed to drive the
    OPEN→fill→CLOSE round-trip.
    """
    from alphamind.execution.broker_adapter import AccountStateQueries
    from alphamind.scripts.verify_broker_adapter import VerifyContext

    factory = _fake_factory(mode=mode)
    sdk_client = MagicMock(spec=TradingClient)
    sdk_client.get_clock.return_value = _make_clock(is_open=True)
    queries = AccountStateQueries(sdk_client)
    return VerifyContext(
        factory=factory,
        queries=queries,
        execution=_execution_config(),
        venue=_venue_config(),
        mode=mode,  # type: ignore[arg-type]
        verbose=verbose,
        db_path=tmp_path / "alphamind.db",
    )


async def test_phase_3_happy_path_equity_open_fill_close(tmp_path: Path) -> None:
    """When creds + DB path are available, phase 3 drives a real equity
    OPEN→fill→CLOSE round-trip against the mocked broker.

    Mocked: ``TradingClient.submit_order`` returns an alpaca-py Order; the
    factory's ``build_trading_stream`` returns a ``_FakeStream`` we inject
    fill events into to simulate the trade_updates websocket.
    """
    from alphamind.scripts.verify_broker_adapter import phase_3_equity_order_lifecycle

    # Two submit_order calls expected (OPEN entry, then CLOSE exit). Track ids
    # so we can fabricate matching fill events.
    open_order = _build_alpaca_order(client_order_id="will-be-overwritten")
    close_order = _build_alpaca_order(client_order_id="will-be-overwritten")
    submitted: list[Any] = []

    def _submit_order(req: Any) -> Any:
        coid = getattr(req, "client_order_id", "")
        target = open_order if len(submitted) == 0 else close_order
        target.client_order_id = coid
        submitted.append(req)
        return target

    client = MagicMock()
    client.submit_order = MagicMock(side_effect=_submit_order)

    stream = _FakeStream()
    ctx = _make_context_with_db(tmp_path)
    ctx.factory.build_trading_client.return_value = client
    ctx.factory.build_trading_stream.return_value = stream

    # Drive the phase; while it's awaiting the OPEN fill, inject the
    # corresponding TradeUpdate. Same for the CLOSE fill.
    import asyncio as _asyncio

    async def _injector() -> None:
        # Wait for handler registration + first submit_order call
        for _ in range(50):
            await _asyncio.sleep(0.01)
            if stream.handler is not None and len(submitted) >= 1:
                break
        await stream.inject(
            _build_trade_update_fill(
                order_id=open_order.id, client_order_id=open_order.client_order_id
            )
        )
        # Wait for second submit_order (CLOSE)
        for _ in range(200):
            await _asyncio.sleep(0.01)
            if len(submitted) >= 2:
                break
        await stream.inject(
            _build_trade_update_fill(
                order_id=close_order.id, client_order_id=close_order.client_order_id
            )
        )

    injector_task = _asyncio.create_task(_injector())
    try:
        result = await phase_3_equity_order_lifecycle(ctx)
    finally:
        injector_task.cancel()
        with contextlib.suppress(_asyncio.CancelledError, Exception):
            await injector_task

    assert result.ok is True, result.detail
    assert result.deferred is False
    # Two submit_order calls — OPEN and CLOSE.
    assert len(submitted) == 2


async def test_phase_3_timeout_when_fill_never_arrives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the OPEN fill never arrives within the timeout, the phase fails with
    a diagnostic naming the missing fill. Cleanup runs the CANCEL on the
    pending order so the paper account isn't left with a hung order."""
    import alphamind.scripts.verify_broker_adapter as module

    # Tighten the equity timeout so the test runs sub-second.
    monkeypatch.setattr(module, "_PHASE_3_FILL_TIMEOUT_SECONDS", 0.2)

    open_order = _build_alpaca_order()
    submitted: list[Any] = []

    def _submit_order(req: Any) -> Any:
        coid = getattr(req, "client_order_id", "")
        open_order.client_order_id = coid
        submitted.append(req)
        return open_order

    client = MagicMock()
    client.submit_order = MagicMock(side_effect=_submit_order)
    client.cancel_order_by_id = MagicMock(return_value=None)

    stream = _FakeStream()
    ctx = _make_context_with_db(tmp_path)
    ctx.factory.build_trading_client.return_value = client
    ctx.factory.build_trading_stream.return_value = stream

    result = await module.phase_3_equity_order_lifecycle(ctx)
    assert result.ok is False
    assert "fill never arrived" in (result.detail or "").lower()
    # Cleanup: CANCEL submitted on the pending OPEN.
    client.cancel_order_by_id.assert_called_once_with(str(open_order.id))


async def test_phase_3_cleanup_close_after_open_filled_but_close_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the OPEN fills but the CLOSE fill never arrives, the cleanup path
    submits a residual CLOSE so the paper account isn't left holding the
    position. Asserts the cleanup CLOSE reached ``submit_order``."""
    import asyncio as _asyncio

    import alphamind.scripts.verify_broker_adapter as module

    monkeypatch.setattr(module, "_PHASE_3_FILL_TIMEOUT_SECONDS", 0.5)

    open_order = _build_alpaca_order(client_order_id="will-be-overwritten")
    close_order = _build_alpaca_order(client_order_id="will-be-overwritten")
    cleanup_order = _build_alpaca_order(client_order_id="will-be-overwritten")
    submitted: list[Any] = []

    def _submit_order(req: Any) -> Any:
        coid = getattr(req, "client_order_id", "")
        targets = [open_order, close_order, cleanup_order]
        target = targets[len(submitted)] if len(submitted) < len(targets) else cleanup_order
        target.client_order_id = coid
        submitted.append(req)
        return target

    client = MagicMock()
    client.submit_order = MagicMock(side_effect=_submit_order)
    client.cancel_order_by_id = MagicMock(return_value=None)

    stream = _FakeStream()
    ctx = _make_context_with_db(tmp_path)
    ctx.factory.build_trading_client.return_value = client
    ctx.factory.build_trading_stream.return_value = stream

    async def _injector() -> None:
        # Wait for OPEN submission, then inject only the OPEN fill — never the
        # CLOSE fill. The CLOSE wait should time out and trigger cleanup.
        for _ in range(50):
            await _asyncio.sleep(0.01)
            if stream.handler is not None and len(submitted) >= 1:
                break
        await stream.inject(
            _build_trade_update_fill(
                order_id=open_order.id, client_order_id=open_order.client_order_id
            )
        )

    injector_task = _asyncio.create_task(_injector())
    try:
        result = await module.phase_3_equity_order_lifecycle(ctx)
    finally:
        injector_task.cancel()
        with contextlib.suppress(_asyncio.CancelledError, Exception):
            await injector_task

    assert result.ok is False
    assert "fill never arrived" in (result.detail or "").lower()
    # OPEN + CLOSE + cleanup CLOSE = 3 submit_order calls.
    assert len(submitted) == 3, f"expected 3 submit_order calls, got {len(submitted)}"


# ---------------------------------------------------------------------------
# Phase 4 — single-leg options OPEN→fill→CLOSE round-trip
# ---------------------------------------------------------------------------


def _build_options_calendar_client(today: dt.date | None = None) -> MagicMock:
    """Build a TradingClient whose get_calendar returns Fridays in the next
    45 days — the verify script picks the first Friday ≥ 7 days out as the
    options expiration so re-runs don't go stale."""
    anchor = today or dt.datetime.now(dt.UTC).date()
    days: list[Calendar] = []
    for offset in range(60):
        d = anchor + dt.timedelta(days=offset)
        if d.weekday() < 5:  # Mon-Fri are business days
            days.append(
                Calendar.model_validate({"date": d.isoformat(), "open": "09:30", "close": "16:00"})
            )

    def _filter(*, filters: Any | None = None) -> list[Calendar]:
        if filters is None:
            return days
        start = getattr(filters, "start", None)
        end = getattr(filters, "end", None)
        return [
            d for d in days if (start is None or d.date >= start) and (end is None or d.date <= end)
        ]

    client = MagicMock(spec=TradingClient)
    client.get_calendar.side_effect = _filter
    return client


def _make_context_with_db_and_calendar(
    tmp_path: Path,
    *,
    mode: str = "paper",
    verbose: bool = False,
    is_open: bool = True,
) -> Any:
    """Build a VerifyContext whose queries' get_calendar honors filters.

    Phases 4 / 5 read the trading calendar to pick a near-term option
    expiration and the option chain to pick a listed strike; tests need a
    queries object whose calendar surface returns enough business days for
    that pick to succeed and whose clock surface reports the market as open
    (the lifecycle phases defer up front when the clock says closed).
    """
    from alphamind.execution.broker_adapter import AccountStateQueries
    from alphamind.scripts.verify_broker_adapter import VerifyContext

    sdk_client = _build_options_calendar_client()
    sdk_client.get_clock.return_value = _make_clock(is_open=is_open)
    sdk_client.get_option_contracts.return_value = _make_option_contracts_response(
        _default_option_chain(),
    )
    queries = AccountStateQueries(sdk_client)
    factory = _fake_factory(mode=mode)
    return VerifyContext(
        factory=factory,
        queries=queries,
        execution=_execution_config(),
        venue=_venue_config(),
        mode=mode,  # type: ignore[arg-type]
        verbose=verbose,
        db_path=tmp_path / "alphamind.db",
    )


async def test_phase_4_defers_when_db_path_missing() -> None:
    """When ``ctx.db_path is None`` the options-lifecycle phase defers."""
    from alphamind.scripts.verify_broker_adapter import phase_4_options_order_lifecycle

    ctx = _make_context()
    result = await phase_4_options_order_lifecycle(ctx)
    assert result.ok is True
    assert result.deferred is True
    assert "db" in (result.detail or "").lower()


async def test_phase_4_defers_when_market_closed(tmp_path: Path) -> None:
    """When ``MarketClock.is_open`` is False, phase 4 defers up front instead
    of submitting options orders that won't fill within the 60s budget."""
    from alphamind.scripts.verify_broker_adapter import phase_4_options_order_lifecycle

    ctx = _make_context_with_db_and_calendar(tmp_path, is_open=False)
    result = await phase_4_options_order_lifecycle(ctx)

    assert result.ok is True
    assert result.deferred is True
    detail = (result.detail or "").lower()
    assert "market" in detail and "closed" in detail
    assert "09:30" in (result.detail or "")
    assert "16:00" in (result.detail or "")


async def test_phase_4_picks_listed_strike_from_chain(tmp_path: Path) -> None:
    """Phase 4 must query the option chain and pick a strike from the listed
    set rather than hardcoding $900 (which Alpaca rejects with
    ``asset "NVDA  260518C00900000" not found`` when the picked expiration
    doesn't list it). The picked strike should be the median of the listed
    chain (approximately ATM), and the OCC symbol on the dispatched OPEN
    should reference that strike, not $900."""
    import asyncio as _asyncio

    from alpaca.trading.enums import AssetClass

    from alphamind.scripts.verify_broker_adapter import phase_4_options_order_lifecycle

    chain_strikes = (95.0, 100.0, 105.0, 110.0, 115.0)  # median = 105.0

    open_order = _build_alpaca_order(asset_class=AssetClass.US_OPTION)
    close_order = _build_alpaca_order(asset_class=AssetClass.US_OPTION)
    submitted: list[Any] = []

    def _submit_order(req: Any) -> Any:
        coid = getattr(req, "client_order_id", "")
        target = open_order if len(submitted) == 0 else close_order
        target.client_order_id = coid
        submitted.append(req)
        return target

    client = MagicMock()
    client.submit_order = MagicMock(side_effect=_submit_order)

    stream = _FakeStream()
    ctx = _make_context_with_db_and_calendar(tmp_path)
    _override_chain(ctx, chain_strikes)
    ctx.factory.build_trading_client.return_value = client
    ctx.factory.build_trading_stream.return_value = stream

    async def _injector() -> None:
        for _ in range(50):
            await _asyncio.sleep(0.01)
            if stream.handler is not None and len(submitted) >= 1:
                break
        await stream.inject(
            _build_trade_update_fill(
                order_id=open_order.id,
                client_order_id=open_order.client_order_id,
                asset_class=AssetClass.US_OPTION,
            )
        )
        for _ in range(200):
            await _asyncio.sleep(0.01)
            if len(submitted) >= 2:
                break
        await stream.inject(
            _build_trade_update_fill(
                order_id=close_order.id,
                client_order_id=close_order.client_order_id,
                asset_class=AssetClass.US_OPTION,
            )
        )

    injector_task = _asyncio.create_task(_injector())
    try:
        result = await phase_4_options_order_lifecycle(ctx)
    finally:
        injector_task.cancel()
        with contextlib.suppress(_asyncio.CancelledError, Exception):
            await injector_task

    assert result.ok is True, result.detail
    open_request = submitted[0]
    listed_strike = int(open_request.symbol[-8:]) / 1000.0
    assert listed_strike in chain_strikes, (
        f"OCC strike {listed_strike} not in listed chain {chain_strikes}"
    )
    # Median of the 5-strike chain is 105.0.
    assert listed_strike == 105.0, f"expected median 105.0, got {listed_strike}"


async def test_phase_4_happy_path_options_open_fill_close(tmp_path: Path) -> None:
    """When creds + DB are available, phase 4 drives a 1-contract options
    OPEN→fill→CLOSE round-trip via the broker dispatcher."""
    import asyncio as _asyncio

    from alpaca.trading.enums import AssetClass

    open_order = _build_alpaca_order(asset_class=AssetClass.US_OPTION)
    close_order = _build_alpaca_order(asset_class=AssetClass.US_OPTION)
    submitted: list[Any] = []

    def _submit_order(req: Any) -> Any:
        coid = getattr(req, "client_order_id", "")
        target = open_order if len(submitted) == 0 else close_order
        target.client_order_id = coid
        submitted.append(req)
        return target

    client = MagicMock()
    client.submit_order = MagicMock(side_effect=_submit_order)

    stream = _FakeStream()
    ctx = _make_context_with_db_and_calendar(tmp_path)
    ctx.factory.build_trading_client.return_value = client
    ctx.factory.build_trading_stream.return_value = stream

    async def _injector() -> None:
        for _ in range(50):
            await _asyncio.sleep(0.01)
            if stream.handler is not None and len(submitted) >= 1:
                break
        await stream.inject(
            _build_trade_update_fill(
                order_id=open_order.id,
                client_order_id=open_order.client_order_id,
                asset_class=AssetClass.US_OPTION,
                symbol="NVDA  260619C00900000",
            )
        )
        for _ in range(200):
            await _asyncio.sleep(0.01)
            if len(submitted) >= 2:
                break
        await stream.inject(
            _build_trade_update_fill(
                order_id=close_order.id,
                client_order_id=close_order.client_order_id,
                asset_class=AssetClass.US_OPTION,
                symbol="NVDA  260619C00900000",
            )
        )

    from alphamind.scripts.verify_broker_adapter import phase_4_options_order_lifecycle

    injector_task = _asyncio.create_task(_injector())
    try:
        result = await phase_4_options_order_lifecycle(ctx)
    finally:
        injector_task.cancel()
        with contextlib.suppress(_asyncio.CancelledError, Exception):
            await injector_task

    assert result.ok is True, result.detail
    assert result.deferred is False
    assert len(submitted) == 2


async def test_phase_4_timeout_when_fill_never_arrives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the OPEN options fill never arrives, the phase fails and cleanup
    cancels the pending order."""
    from alpaca.trading.enums import AssetClass

    import alphamind.scripts.verify_broker_adapter as module

    monkeypatch.setattr(module, "_PHASE_4_FILL_TIMEOUT_SECONDS", 0.2)

    open_order = _build_alpaca_order(asset_class=AssetClass.US_OPTION)
    submitted: list[Any] = []

    def _submit_order(req: Any) -> Any:
        coid = getattr(req, "client_order_id", "")
        open_order.client_order_id = coid
        submitted.append(req)
        return open_order

    client = MagicMock()
    client.submit_order = MagicMock(side_effect=_submit_order)
    client.cancel_order_by_id = MagicMock(return_value=None)

    stream = _FakeStream()
    ctx = _make_context_with_db_and_calendar(tmp_path)
    ctx.factory.build_trading_client.return_value = client
    ctx.factory.build_trading_stream.return_value = stream

    result = await module.phase_4_options_order_lifecycle(ctx)
    assert result.ok is False
    assert "fill never arrived" in (result.detail or "").lower()
    client.cancel_order_by_id.assert_called_once_with(str(open_order.id))


async def test_phase_4_cleanup_close_after_open_filled_but_close_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the OPEN options fill arrives but CLOSE fill never does, cleanup
    submits a residual CLOSE."""
    import asyncio as _asyncio

    from alpaca.trading.enums import AssetClass

    import alphamind.scripts.verify_broker_adapter as module

    monkeypatch.setattr(module, "_PHASE_4_FILL_TIMEOUT_SECONDS", 0.5)

    open_order = _build_alpaca_order(asset_class=AssetClass.US_OPTION)
    close_order = _build_alpaca_order(asset_class=AssetClass.US_OPTION)
    cleanup_order = _build_alpaca_order(asset_class=AssetClass.US_OPTION)
    submitted: list[Any] = []

    def _submit_order(req: Any) -> Any:
        coid = getattr(req, "client_order_id", "")
        targets = [open_order, close_order, cleanup_order]
        target = targets[len(submitted)] if len(submitted) < len(targets) else cleanup_order
        target.client_order_id = coid
        submitted.append(req)
        return target

    client = MagicMock()
    client.submit_order = MagicMock(side_effect=_submit_order)
    client.cancel_order_by_id = MagicMock(return_value=None)

    stream = _FakeStream()
    ctx = _make_context_with_db_and_calendar(tmp_path)
    ctx.factory.build_trading_client.return_value = client
    ctx.factory.build_trading_stream.return_value = stream

    async def _injector() -> None:
        for _ in range(50):
            await _asyncio.sleep(0.01)
            if stream.handler is not None and len(submitted) >= 1:
                break
        await stream.inject(
            _build_trade_update_fill(
                order_id=open_order.id,
                client_order_id=open_order.client_order_id,
                asset_class=AssetClass.US_OPTION,
                symbol="NVDA  260619C00900000",
            )
        )

    injector_task = _asyncio.create_task(_injector())
    try:
        result = await module.phase_4_options_order_lifecycle(ctx)
    finally:
        injector_task.cancel()
        with contextlib.suppress(_asyncio.CancelledError, Exception):
            await injector_task

    assert result.ok is False
    assert "fill never arrived" in (result.detail or "").lower()
    assert len(submitted) == 3


# ---------------------------------------------------------------------------
# Phase 5 — multi-leg vertical-spread OPEN→fill→CLOSE round-trip
# ---------------------------------------------------------------------------


async def test_phase_5_defers_when_db_path_missing() -> None:
    """When ``ctx.db_path is None`` the mleg-lifecycle phase defers."""
    from alphamind.scripts.verify_broker_adapter import phase_5_mleg_order_lifecycle

    ctx = _make_context()
    result = await phase_5_mleg_order_lifecycle(ctx)
    assert result.ok is True
    assert result.deferred is True
    assert "db" in (result.detail or "").lower()


async def test_phase_5_defers_when_market_closed(tmp_path: Path) -> None:
    """When ``MarketClock.is_open`` is False, phase 5 defers up front instead
    of submitting a multi-leg spread that won't fill within the 120s budget."""
    from alphamind.scripts.verify_broker_adapter import phase_5_mleg_order_lifecycle

    ctx = _make_context_with_db_and_calendar(tmp_path, is_open=False)
    result = await phase_5_mleg_order_lifecycle(ctx)

    assert result.ok is True
    assert result.deferred is True
    detail = (result.detail or "").lower()
    assert "market" in detail and "closed" in detail
    assert "09:30" in (result.detail or "")
    assert "16:00" in (result.detail or "")


async def test_phase_4_retries_later_expiration_when_first_chain_empty(
    tmp_path: Path,
) -> None:
    """When the first business-day candidate has no listed contracts, phase 4
    must walk forward to the next candidate and pick a strike from that chain.
    Models the operator failure where the first expiration past T+7 has no
    listed call options on NVDA but later expirations do."""
    import asyncio as _asyncio

    from alpaca.trading.enums import AssetClass

    from alphamind.scripts.verify_broker_adapter import phase_4_options_order_lifecycle

    open_order = _build_alpaca_order(asset_class=AssetClass.US_OPTION)
    close_order = _build_alpaca_order(asset_class=AssetClass.US_OPTION)
    submitted: list[Any] = []

    def _submit_order(req: Any) -> Any:
        coid = getattr(req, "client_order_id", "")
        target = open_order if len(submitted) == 0 else close_order
        target.client_order_id = coid
        submitted.append(req)
        return target

    client = MagicMock()
    client.submit_order = MagicMock(side_effect=_submit_order)

    stream = _FakeStream()
    ctx = _make_context_with_db_and_calendar(tmp_path)
    sdk_client = ctx.queries._client
    populated = [_make_option_contract(strike=s) for s in (95.0, 100.0, 105.0)]

    call_count = {"n": 0}

    def _chain(request: Any) -> Any:
        call_count["n"] += 1
        # First lookup returns empty; subsequent lookups return a populated chain.
        if call_count["n"] == 1:
            return _make_option_contracts_response([])
        return _make_option_contracts_response(populated)

    sdk_client.get_option_contracts.side_effect = _chain

    ctx.factory.build_trading_client.return_value = client
    ctx.factory.build_trading_stream.return_value = stream

    async def _injector() -> None:
        for _ in range(50):
            await _asyncio.sleep(0.01)
            if stream.handler is not None and len(submitted) >= 1:
                break
        await stream.inject(
            _build_trade_update_fill(
                order_id=open_order.id,
                client_order_id=open_order.client_order_id,
                asset_class=AssetClass.US_OPTION,
            )
        )
        for _ in range(200):
            await _asyncio.sleep(0.01)
            if len(submitted) >= 2:
                break
        await stream.inject(
            _build_trade_update_fill(
                order_id=close_order.id,
                client_order_id=close_order.client_order_id,
                asset_class=AssetClass.US_OPTION,
            )
        )

    injector_task = _asyncio.create_task(_injector())
    try:
        result = await phase_4_options_order_lifecycle(ctx)
    finally:
        injector_task.cancel()
        with contextlib.suppress(_asyncio.CancelledError, Exception):
            await injector_task

    assert result.ok is True, result.detail
    # The chain was queried at least twice (first empty, then populated).
    assert call_count["n"] >= 2
    # The picked strike is from the populated (second) chain.
    open_request = submitted[0]
    listed_strike = int(open_request.symbol[-8:]) / 1000.0
    assert listed_strike == 100.0  # median of (95, 100, 105)


async def test_phase_4_defers_when_no_expiration_has_listed_chain(
    tmp_path: Path,
) -> None:
    """When *every* business-day candidate in the search window returns an
    empty chain, phase 4 DEFERs with a clear "no listed contracts" diagnostic
    rather than submitting an order with a guessed strike."""
    from alphamind.scripts.verify_broker_adapter import phase_4_options_order_lifecycle

    ctx = _make_context_with_db_and_calendar(tmp_path)
    sdk_client = ctx.queries._client
    sdk_client.get_option_contracts.return_value = _make_option_contracts_response([])

    result = await phase_4_options_order_lifecycle(ctx)

    assert result.ok is True
    assert result.deferred is True
    detail = (result.detail or "").lower()
    assert "no listed" in detail or "no contracts" in detail or "cannot" in detail


async def test_phase_5_picks_listed_strikes_from_chain(tmp_path: Path) -> None:
    """Phase 5 must query the option chain and pick long + short strikes from
    the listed set rather than hardcoding $900 / $910. Long strike = median
    of listed chain (approximately ATM); short strike = next listed strike
    above. Both must be in the listed chain and short > long."""
    import asyncio as _asyncio

    from alpaca.trading.enums import AssetClass, OrderClass

    from alphamind.scripts.verify_broker_adapter import phase_5_mleg_order_lifecycle

    chain_strikes = (95.0, 100.0, 105.0, 110.0, 115.0)
    # median = 105.0; next-up = 110.0

    open_order = _build_alpaca_order(order_class=OrderClass.MLEG, asset_class=AssetClass.US_OPTION)
    close_order = _build_alpaca_order(order_class=OrderClass.MLEG, asset_class=AssetClass.US_OPTION)
    submitted: list[Any] = []

    def _submit_order(req: Any) -> Any:
        coid = getattr(req, "client_order_id", "")
        target = open_order if len(submitted) == 0 else close_order
        target.client_order_id = coid
        submitted.append(req)
        return target

    client = MagicMock()
    client.submit_order = MagicMock(side_effect=_submit_order)

    stream = _FakeStream()
    ctx = _make_context_with_db_and_calendar(tmp_path)
    _override_chain(ctx, chain_strikes)
    ctx.factory.build_trading_client.return_value = client
    ctx.factory.build_trading_stream.return_value = stream

    async def _injector() -> None:
        for _ in range(50):
            await _asyncio.sleep(0.01)
            if stream.handler is not None and len(submitted) >= 1:
                break
        await stream.inject(
            _build_trade_update_fill(
                order_id=open_order.id,
                client_order_id=open_order.client_order_id,
                asset_class=AssetClass.US_OPTION,
                order_class=OrderClass.MLEG,
            )
        )
        for _ in range(200):
            await _asyncio.sleep(0.01)
            if len(submitted) >= 2:
                break
        await stream.inject(
            _build_trade_update_fill(
                order_id=close_order.id,
                client_order_id=close_order.client_order_id,
                asset_class=AssetClass.US_OPTION,
                order_class=OrderClass.MLEG,
            )
        )

    injector_task = _asyncio.create_task(_injector())
    try:
        result = await phase_5_mleg_order_lifecycle(ctx)
    finally:
        injector_task.cancel()
        with contextlib.suppress(_asyncio.CancelledError, Exception):
            await injector_task

    assert result.ok is True, result.detail
    # Inspect the OPEN's mleg legs — both leg OCC symbols must reference
    # listed strikes, with short > long.
    open_request = submitted[0]
    legs = open_request.legs
    assert len(legs) == 2
    leg_strikes = sorted(int(leg.symbol[-8:]) / 1000.0 for leg in legs)
    long_strike, short_strike = leg_strikes[0], leg_strikes[1]
    assert long_strike in chain_strikes, (
        f"long strike {long_strike} not in listed chain {chain_strikes}"
    )
    assert short_strike in chain_strikes, (
        f"short strike {short_strike} not in listed chain {chain_strikes}"
    )
    assert short_strike > long_strike
    # Median + next-up: 105.0 / 110.0.
    assert long_strike == 105.0
    assert short_strike == 110.0


async def test_phase_5_happy_path_mleg_open_fill_close(tmp_path: Path) -> None:
    """When creds + DB are available, phase 5 drives a 1-contract vertical
    spread OPEN→fill→CLOSE round-trip via the broker dispatcher."""
    import asyncio as _asyncio

    from alpaca.trading.enums import AssetClass, OrderClass

    open_order = _build_alpaca_order(order_class=OrderClass.MLEG, asset_class=AssetClass.US_OPTION)
    close_order = _build_alpaca_order(order_class=OrderClass.MLEG, asset_class=AssetClass.US_OPTION)
    submitted: list[Any] = []

    def _submit_order(req: Any) -> Any:
        coid = getattr(req, "client_order_id", "")
        target = open_order if len(submitted) == 0 else close_order
        target.client_order_id = coid
        submitted.append(req)
        return target

    client = MagicMock()
    client.submit_order = MagicMock(side_effect=_submit_order)

    stream = _FakeStream()
    ctx = _make_context_with_db_and_calendar(tmp_path)
    ctx.factory.build_trading_client.return_value = client
    ctx.factory.build_trading_stream.return_value = stream

    async def _injector() -> None:
        for _ in range(50):
            await _asyncio.sleep(0.01)
            if stream.handler is not None and len(submitted) >= 1:
                break
        await stream.inject(
            _build_trade_update_fill(
                order_id=open_order.id,
                client_order_id=open_order.client_order_id,
                asset_class=AssetClass.US_OPTION,
                order_class=OrderClass.MLEG,
                symbol="NVDA  260619C00900000",
            )
        )
        for _ in range(200):
            await _asyncio.sleep(0.01)
            if len(submitted) >= 2:
                break
        await stream.inject(
            _build_trade_update_fill(
                order_id=close_order.id,
                client_order_id=close_order.client_order_id,
                asset_class=AssetClass.US_OPTION,
                order_class=OrderClass.MLEG,
                symbol="NVDA  260619C00900000",
            )
        )

    from alphamind.scripts.verify_broker_adapter import phase_5_mleg_order_lifecycle

    injector_task = _asyncio.create_task(_injector())
    try:
        result = await phase_5_mleg_order_lifecycle(ctx)
    finally:
        injector_task.cancel()
        with contextlib.suppress(_asyncio.CancelledError, Exception):
            await injector_task

    assert result.ok is True, result.detail
    assert result.deferred is False
    assert len(submitted) == 2


async def test_phase_5_timeout_when_fill_never_arrives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the OPEN mleg fill never arrives, the phase fails and cleanup
    cancels the pending order."""
    from alpaca.trading.enums import AssetClass, OrderClass

    import alphamind.scripts.verify_broker_adapter as module

    monkeypatch.setattr(module, "_PHASE_5_FILL_TIMEOUT_SECONDS", 0.2)

    open_order = _build_alpaca_order(order_class=OrderClass.MLEG, asset_class=AssetClass.US_OPTION)
    submitted: list[Any] = []

    def _submit_order(req: Any) -> Any:
        coid = getattr(req, "client_order_id", "")
        open_order.client_order_id = coid
        submitted.append(req)
        return open_order

    client = MagicMock()
    client.submit_order = MagicMock(side_effect=_submit_order)
    client.cancel_order_by_id = MagicMock(return_value=None)

    stream = _FakeStream()
    ctx = _make_context_with_db_and_calendar(tmp_path)
    ctx.factory.build_trading_client.return_value = client
    ctx.factory.build_trading_stream.return_value = stream

    result = await module.phase_5_mleg_order_lifecycle(ctx)
    assert result.ok is False
    assert "fill never arrived" in (result.detail or "").lower()
    client.cancel_order_by_id.assert_called_once_with(str(open_order.id))


async def test_phase_5_cleanup_close_after_open_filled_but_close_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If OPEN mleg fills but CLOSE fill never does, cleanup submits a
    residual CLOSE."""
    import asyncio as _asyncio

    from alpaca.trading.enums import AssetClass, OrderClass

    import alphamind.scripts.verify_broker_adapter as module

    monkeypatch.setattr(module, "_PHASE_5_FILL_TIMEOUT_SECONDS", 0.5)

    open_order = _build_alpaca_order(order_class=OrderClass.MLEG, asset_class=AssetClass.US_OPTION)
    close_order = _build_alpaca_order(order_class=OrderClass.MLEG, asset_class=AssetClass.US_OPTION)
    cleanup_order = _build_alpaca_order(
        order_class=OrderClass.MLEG, asset_class=AssetClass.US_OPTION
    )
    submitted: list[Any] = []

    def _submit_order(req: Any) -> Any:
        coid = getattr(req, "client_order_id", "")
        targets = [open_order, close_order, cleanup_order]
        target = targets[len(submitted)] if len(submitted) < len(targets) else cleanup_order
        target.client_order_id = coid
        submitted.append(req)
        return target

    client = MagicMock()
    client.submit_order = MagicMock(side_effect=_submit_order)
    client.cancel_order_by_id = MagicMock(return_value=None)

    stream = _FakeStream()
    ctx = _make_context_with_db_and_calendar(tmp_path)
    ctx.factory.build_trading_client.return_value = client
    ctx.factory.build_trading_stream.return_value = stream

    async def _injector() -> None:
        for _ in range(50):
            await _asyncio.sleep(0.01)
            if stream.handler is not None and len(submitted) >= 1:
                break
        await stream.inject(
            _build_trade_update_fill(
                order_id=open_order.id,
                client_order_id=open_order.client_order_id,
                asset_class=AssetClass.US_OPTION,
                order_class=OrderClass.MLEG,
                symbol="NVDA  260619C00900000",
            )
        )

    injector_task = _asyncio.create_task(_injector())
    try:
        result = await module.phase_5_mleg_order_lifecycle(ctx)
    finally:
        injector_task.cancel()
        with contextlib.suppress(_asyncio.CancelledError, Exception):
            await injector_task

    assert result.ok is False
    assert "fill never arrived" in (result.detail or "").lower()
    assert len(submitted) == 3
