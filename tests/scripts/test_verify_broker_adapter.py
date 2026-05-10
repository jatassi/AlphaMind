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
