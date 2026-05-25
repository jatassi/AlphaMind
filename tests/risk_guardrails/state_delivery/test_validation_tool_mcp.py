"""Tests for the validate_guardrail MCP tool wrapper — story 04 (ALP-294)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from types import MappingProxyType
from typing import Any

import pytest

from alphamind._kernel.ids import Symbol
from alphamind._kernel.regime import (
    RegimeLabel,
    RegimeTransitionState,
)
from alphamind.portfolio_state.aggregates.risk_budget import RiskBudgetConsumption
from alphamind.portfolio_state.aggregates.risk_parameters import ActiveRiskParameterSet
from alphamind.risk_guardrails.guardrail_evaluation import (
    ContractType,
    EscalationZones,
    FeatureFlagsView,
    FixtureIvProvider,
    IvQuote,
    IvSurfaceEntry,
    LibraryConfig,
    MarketInputs,
    PortfolioStateSnapshot,
)
from alphamind.risk_guardrails.state_delivery.validation_tool_mcp import (
    build_initial_validation_state,
    build_validate_guardrail_mcp_server,
)

# ---------------------------------------------------------------------------
# Module-level fixture constants — match the guardrail validation tool's tests
# ---------------------------------------------------------------------------

_AS_OF = datetime(2026, 4, 28, 14, 30, tzinfo=UTC)
_EXPIRATION_DATE = date(2026, 5, 28)
_EXPIRATION_DT = datetime(2026, 5, 28, 0, 0, tzinfo=UTC)
_RISK_FREE_RATE = 0.045
_SPOT = 100.0
_IV = 0.30
_PORTFOLIO_VALUE = 100_000.0


def _zones() -> EscalationZones:
    return EscalationZones(warning=70.0, critical=85.0, hard_block=95.0)


def _config(
    *,
    options_enabled: bool = True,
    short_selling_enabled: bool = True,
    active_sectors: tuple[str, ...] = ("tech", "semis", "financials", "energy"),
) -> LibraryConfig:
    effective_limits = {
        "position_max_size_pct": 10.0,
        "sector_concentration_pct": 25.0,
        "net_long_pct": 60.0,
        "net_short_pct": 40.0,
        "gross_exposure_pct": 100.0,
        "options_delta_pct": 30.0,
        "portfolio_theta_pct_per_day": 0.5,
        "portfolio_vega_pct_per_iv_point": 1.0,
        "total_short_pct": 30.0,
        "single_short_max_pct": 5.0,
        "borrow_cost_budget_pct_per_day": 0.05,
        "min_cash_reserve_pct": 10.0,
        "pending_order_capital_pct": 20.0,
    }
    return LibraryConfig(
        effective_limits=MappingProxyType(effective_limits),
        escalation_zones=MappingProxyType({k: _zones() for k in effective_limits}),
        feature_flags=FeatureFlagsView(
            options_enabled=options_enabled,
            short_selling_enabled=short_selling_enabled,
        ),
        active_sectors=active_sectors,
        active_regime="normal",
        active_profile="medium",
        conservative_buffer_pct=10.0,
    )


def _snapshot(
    *,
    sector_exposure_pct: Mapping[str, float] | None = None,
    net_long_pct: float = 30.0,
    net_short_pct: float = 0.0,
    gross_pct: float = 78.0,
    cash_usd: float = 70_000.0,
) -> PortfolioStateSnapshot:
    if sector_exposure_pct is None:
        sector_exposure_pct = {
            "tech": 18.3,
            "semis": 6.0,
            "financials": 3.0,
            "energy": 3.0,
        }
    return PortfolioStateSnapshot(
        portfolio_value_usd=_PORTFOLIO_VALUE,
        cash_usd=cash_usd,
        reserved_for_pending_orders_usd=0.0,
        sector_exposure_pct=MappingProxyType(dict(sector_exposure_pct)),
        net_long_pct=net_long_pct,
        net_short_pct=net_short_pct,
        gross_pct=gross_pct,
        options_delta_pct=0.0,
        portfolio_theta_pct_per_day=0.0,
        portfolio_vega_pct_per_iv_point=0.0,
        total_short_pct=0.0,
        single_short_max_pct=0.0,
        daily_borrow_cost_pct=0.0,
        position_max_size_pct=5.0,
        existing_positions=MappingProxyType({}),
    )


def _atm_provider(underlying: str = "AAPL") -> FixtureIvProvider:
    return FixtureIvProvider(
        surface={
            underlying: IvSurfaceEntry(
                underlying=underlying,
                quotes=(
                    IvQuote(
                        strike=100.0,
                        expiration=_EXPIRATION_DATE,
                        contract_type=ContractType.CALL,
                        implied_volatility=_IV,
                    ),
                    IvQuote(
                        strike=100.0,
                        expiration=_EXPIRATION_DATE,
                        contract_type=ContractType.PUT,
                        implied_volatility=_IV,
                    ),
                ),
            ),
        },
        realized_vol={},
    )


def _market(underlyings: Sequence[str] = ("AAPL", "NVDA", "ABC")) -> MarketInputs:
    return MarketInputs(
        underlying_prices=MappingProxyType({u: _SPOT for u in underlyings}),
        risk_free_rate=_RISK_FREE_RATE,
        iv_provider=_atm_provider("AAPL"),
        as_of=_AS_OF,
    )


def _risk_budget() -> RiskBudgetConsumption:
    return RiskBudgetConsumption(entries=())


def _active_risk_parameters() -> ActiveRiskParameterSet:
    return ActiveRiskParameterSet(
        regime_label=RegimeLabel.NORMAL,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        parameter_change_flag=False,
        entries=(),
        active_overlays=(),
    )


def _sector_resolver(ticker: str) -> str:
    return {"AAPL": "tech", "NVDA": "tech", "ABC": "tech"}.get(ticker, "tech")


def _make_state(
    *,
    config: LibraryConfig | None = None,
    snapshot: PortfolioStateSnapshot | None = None,
    market: MarketInputs | None = None,
) -> Any:
    """Build a ValidationToolState fixture; imported lazily to keep the test
    module's import dependency on validation_tool_mcp explicit."""
    from alphamind.risk_guardrails.state_delivery.validation_tool import (
        ValidationToolState,
    )

    cfg = config or _config()
    return ValidationToolState(
        invocation_id="INV-001",
        starting_snapshot=snapshot or _snapshot(),
        starting_risk_budget=_risk_budget(),
        starting_active_risk_parameters=_active_risk_parameters(),
        profile_feature_flags=cfg.feature_flags,
        library_config=cfg,
        library_market=market or _market(),
        sector_resolver=_sector_resolver,
        accumulated_deltas=(),
    )


# ---------------------------------------------------------------------------
# In-process MCP-server handler invocation helper
# ---------------------------------------------------------------------------


async def _invoke_mcp_tool(server: Any, tool_name: str, args: dict[str, Any]) -> tuple[str, bool]:
    """Drive the in-process MCP server's tools/call handler.

    Returns ``(text, is_error)`` from the handler's first content block plus the
    CallToolResult ``isError`` flag.
    """
    from mcp.types import CallToolRequest, CallToolRequestParams

    request = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name=tool_name, arguments=args),
    )
    handler = server.request_handlers[CallToolRequest]
    result = await handler(request)
    content_blocks = result.root.content
    assert content_blocks, "tool returned no content blocks"
    block = content_blocks[0]
    text = block.text
    assert isinstance(text, str)
    return text, bool(result.root.isError)


# ---------------------------------------------------------------------------
# Sample request dicts (raw JSON shape — what the SDK passes to the handler)
# ---------------------------------------------------------------------------


def _equity_open_args(
    *,
    ticker: str = "AAPL",
    direction: str = "long",
    quantity: int = 50,
    dollar_value: float = 5_000.0,
) -> dict[str, Any]:
    return {
        "instrument": {
            "ticker": ticker,
            "asset_type": "equity",
            "direction": direction,
        },
        "size": {"quantity": quantity, "dollar_value": dollar_value},
        "action": "OPEN",
        "reserves_capital": False,
    }


def _option_open_args(
    *,
    ticker: str = "AAPL",
    direction: str = "long",
    quantity: int = 5,
    dollar_value: float = 1_000.0,
    premium_at_risk_usd: float = 1_000.0,
    strike: float = 100.0,
) -> dict[str, Any]:
    return {
        "instrument": {
            "ticker": ticker,
            "asset_type": "options",
            "direction": direction,
            "strike": strike,
            "expiration": _EXPIRATION_DT.isoformat(),
            "contract_type": "call",
        },
        "size": {
            "quantity": quantity,
            "dollar_value": dollar_value,
            "premium_at_risk_usd": premium_at_risk_usd,
        },
        "action": "OPEN",
        "reserves_capital": False,
    }


# ---------------------------------------------------------------------------
# 1. Factory shape
# ---------------------------------------------------------------------------


def test_factory_returns_server_and_allowed_tools() -> None:
    """build_validate_guardrail_mcp_server returns (mcp_servers, allowed_tools)
    with both tool names under the default server name."""
    state = _make_state()
    mcp_servers, allowed_tools = build_validate_guardrail_mcp_server(state)

    assert "alphamind_decision_validation" in mcp_servers
    server_config = mcp_servers["alphamind_decision_validation"]
    assert server_config["type"] == "sdk"
    assert server_config["name"] == "alphamind_decision_validation"

    assert allowed_tools == [
        "mcp__alphamind_decision_validation__validate_guardrail",
        "mcp__alphamind_decision_validation__validate_guardrail_batch",
    ]


# ---------------------------------------------------------------------------
# 2. OPEN equity request returns JSON-serialised ValidationResult
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_equity_returns_serialised_validation_result() -> None:
    """An OPEN equity request runs through the library and returns a JSON text
    block with overall, per_rule, delta_adjusted_exposure, greeks,
    cumulative_impact_note, failure_guidance, and checked_at fields."""
    import json

    state = _make_state()
    mcp_servers, _ = build_validate_guardrail_mcp_server(state)
    server = mcp_servers["alphamind_decision_validation"]["instance"]

    text, is_error = await _invoke_mcp_tool(server, "validate_guardrail", _equity_open_args())
    assert not is_error

    payload = json.loads(text)
    assert payload["overall"] == "PASS"
    assert isinstance(payload["per_rule"], list)
    assert payload["per_rule"]  # non-empty
    assert "delta_adjusted_exposure" in payload
    assert "greeks" in payload  # may be None for equity
    assert "cumulative_impact_note" in payload
    assert "failure_guidance" in payload
    assert "checked_at" in payload


# ---------------------------------------------------------------------------
# 3. PASS path advances cell — second call sees prior proposal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pass_advances_cell_so_second_call_notes_prior() -> None:
    """After a PASS, subsequent calls should see proposal #2 with cumulative
    impact tracking (the cell was advanced)."""
    import json

    state = _make_state()
    mcp_servers, _ = build_validate_guardrail_mcp_server(state)
    server = mcp_servers["alphamind_decision_validation"]["instance"]

    text1, _ = await _invoke_mcp_tool(server, "validate_guardrail", _equity_open_args())
    p1 = json.loads(text1)
    assert p1["overall"] == "PASS"
    assert "No prior proposals" in p1["cumulative_impact_note"]

    text2, _ = await _invoke_mcp_tool(
        server, "validate_guardrail", _equity_open_args(ticker=Symbol("NVDA"))
    )
    p2 = json.loads(text2)
    assert "Cumulative impact of proposals #1-1" in p2["cumulative_impact_note"]


# ---------------------------------------------------------------------------
# 4. FAIL path preserves cell — failed proposal is not accumulated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fail_does_not_advance_cell() -> None:
    """A FAIL must leave the cell unchanged — the next call still sees itself
    as proposal #2 only if a prior PASS advanced the cell.

    Scenario: first call is a PASS (advances cell to #2-pending). Second call
    fails because options are disabled. Third call should still report itself
    as proposal #2, not #3.
    """
    import json

    # Disable options so the option request reliably fails.
    state = _make_state(config=_config(options_enabled=False))
    mcp_servers, _ = build_validate_guardrail_mcp_server(state)
    server = mcp_servers["alphamind_decision_validation"]["instance"]

    text1, _ = await _invoke_mcp_tool(server, "validate_guardrail", _equity_open_args())
    assert json.loads(text1)["overall"] == "PASS"

    text2, _ = await _invoke_mcp_tool(server, "validate_guardrail", _option_open_args())
    p2 = json.loads(text2)
    assert p2["overall"] == "FAIL"

    text3, _ = await _invoke_mcp_tool(
        server, "validate_guardrail", _equity_open_args(ticker=Symbol("NVDA"))
    )
    p3 = json.loads(text3)
    # The PASS at step #1 advanced cell to next-index=2; the FAIL did NOT
    # advance, so step #3 should be proposal #2, not #3.
    assert p3["proposal_index_in_invocation"] == 2


# ---------------------------------------------------------------------------
# 5. ValidationError on input — surfaces as is_error=True with field-path message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_options_request_missing_strike_yields_input_error() -> None:
    """An options request without ``strike`` (cross-field invariant the JSON
    schema cannot express) is rejected by Pydantic; the handler surfaces it
    as is_error=True with a message naming the missing field path."""
    bad_args = {
        "instrument": {
            "ticker": "AAPL",
            "asset_type": "options",
            "direction": "long",
            # strike, expiration, contract_type all missing
        },
        "size": {"quantity": 1, "dollar_value": 100.0, "premium_at_risk_usd": 100.0},
        "action": "OPEN",
    }
    state = _make_state()
    mcp_servers, _ = build_validate_guardrail_mcp_server(state)
    server = mcp_servers["alphamind_decision_validation"]["instance"]

    text, is_error = await _invoke_mcp_tool(server, "validate_guardrail", bad_args)

    assert is_error
    # Message should reference the offending field(s) so the agent can correct.
    assert "OPTIONS asset_type requires" in text or "strike" in text


# ---------------------------------------------------------------------------
# 6. OPEN options when options_enabled — runs through the library; greeks set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_options_returns_greeks_and_runs_library() -> None:
    """An OPEN options request (with options enabled) returns a result with
    populated greeks (delta/gamma/theta/vega) — confirming the library was
    called rather than the feature-flag early-exit fired."""
    import json

    state = _make_state()  # default config has options_enabled=True
    mcp_servers, _ = build_validate_guardrail_mcp_server(state)
    server = mcp_servers["alphamind_decision_validation"]["instance"]

    text, is_error = await _invoke_mcp_tool(server, "validate_guardrail", _option_open_args())
    assert not is_error

    payload = json.loads(text)
    assert payload["greeks"] is not None
    assert {"delta", "gamma", "theta", "vega"} <= set(payload["greeks"].keys())
    # The library produced per_rule output rather than the empty-tuple early-exit.
    assert payload["per_rule"]


# ---------------------------------------------------------------------------
# 7. Disabled-feature options request returns FAIL with intact guidance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_options_disabled_returns_fail_with_feature_disabled_guidance() -> None:
    """When options_enabled=False, an options OPEN returns FAIL with empty
    per_rule and the feature-disabled failure_guidance message intact."""
    import json

    state = _make_state(config=_config(options_enabled=False))
    mcp_servers, _ = build_validate_guardrail_mcp_server(state)
    server = mcp_servers["alphamind_decision_validation"]["instance"]

    text, is_error = await _invoke_mcp_tool(server, "validate_guardrail", _option_open_args())
    assert not is_error  # disabled feature is a domain FAIL, not a tool error

    payload = json.loads(text)
    assert payload["overall"] == "FAIL"
    assert payload["per_rule"] == []
    assert payload["delta_adjusted_exposure"] == 0.0
    assert payload["greeks"] is None
    assert payload["failure_guidance"] == (
        "Options trading is disabled for this portfolio profile."
    )


# ---------------------------------------------------------------------------
# 7b. Resolver-coverage gap → UNAVAILABLE, serialised with reason (ALP-581)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_short_equity_without_resolver_serialises_unavailable() -> None:
    """A short equity OPEN with no ``borrow_cost_resolver`` wired returns a
    serialised ``UNAVAILABLE`` result naming the missing borrow-cost data — not
    a tool error and not a generic FAIL (ALP-581)."""
    import json

    state = _make_state()  # _make_state wires no borrow_cost_resolver
    mcp_servers, _ = build_validate_guardrail_mcp_server(state)
    server = mcp_servers["alphamind_decision_validation"]["instance"]

    text, is_error = await _invoke_mcp_tool(
        server, "validate_guardrail", _equity_open_args(direction="short")
    )
    assert not is_error  # a coverage gap is a domain result, not a tool error

    payload = json.loads(text)
    assert payload["overall"] == "UNAVAILABLE"
    assert payload["unavailable_reason"] == "missing_borrow_cost"
    assert payload["per_rule"] == []
    assert payload["failure_guidance"] is not None
    assert "checked_at" in payload


@pytest.mark.asyncio
async def test_ticker_outside_market_scope_serialises_unavailable() -> None:
    """An OPEN on a ticker absent from ``library_market.underlying_prices``
    serialises ``UNAVAILABLE`` / ``missing_market_price``."""
    import json

    state = _make_state()  # market covers AAPL/NVDA/ABC
    mcp_servers, _ = build_validate_guardrail_mcp_server(state)
    server = mcp_servers["alphamind_decision_validation"]["instance"]

    text, is_error = await _invoke_mcp_tool(
        server, "validate_guardrail", _equity_open_args(ticker="CSCO")
    )
    assert not is_error

    payload = json.loads(text)
    assert payload["overall"] == "UNAVAILABLE"
    assert payload["unavailable_reason"] == "missing_market_price"


@pytest.mark.asyncio
async def test_unavailable_does_not_advance_cell() -> None:
    """An UNAVAILABLE result, like a FAIL, leaves the cumulative-tracking cell
    unchanged — an unvalidatable proposal is not counted toward headroom."""
    import json

    state = _make_state()
    mcp_servers, _ = build_validate_guardrail_mcp_server(state)
    server = mcp_servers["alphamind_decision_validation"]["instance"]

    text1, _ = await _invoke_mcp_tool(server, "validate_guardrail", _equity_open_args())
    assert json.loads(text1)["overall"] == "PASS"

    text2, _ = await _invoke_mcp_tool(
        server, "validate_guardrail", _equity_open_args(ticker="CSCO")
    )
    assert json.loads(text2)["overall"] == "UNAVAILABLE"

    text3, _ = await _invoke_mcp_tool(
        server, "validate_guardrail", _equity_open_args(ticker=Symbol("NVDA"))
    )
    # The PASS at step #1 advanced the cell to next-index=2; the UNAVAILABLE
    # did NOT advance, so step #3 is proposal #2, not #3.
    assert json.loads(text3)["proposal_index_in_invocation"] == 2


# ---------------------------------------------------------------------------
# 8. build_initial_validation_state helper assembles a fresh state with empty
# accumulated deltas
# ---------------------------------------------------------------------------


def test_build_initial_validation_state_returns_fresh_state() -> None:
    """The runner-facing helper returns a ValidationToolState with the
    upstream inputs wired through and ``accumulated_deltas`` empty."""
    from alphamind.risk_guardrails.state_delivery.validation_tool import (
        ValidationToolState,
    )

    cfg = _config()
    state = build_initial_validation_state(
        invocation_id="INV-XYZ",
        starting_snapshot=_snapshot(),
        starting_risk_budget=_risk_budget(),
        starting_active_risk_parameters=_active_risk_parameters(),
        profile_feature_flags=cfg.feature_flags,
        library_config=cfg,
        library_market=_market(),
        sector_resolver=_sector_resolver,
    )
    assert isinstance(state, ValidationToolState)
    assert state.invocation_id == "INV-XYZ"
    assert state.accumulated_deltas == ()
    assert state.borrow_cost_resolver is None
    assert state.sector_resolver("AAPL") == "tech"


# ---------------------------------------------------------------------------
# 9. Package re-exports the factory + helper from state_delivery/__init__.py
# ---------------------------------------------------------------------------


def test_package_re_exports_factory_and_helper() -> None:
    """The state_delivery package's public surface exposes the factory and
    helper so callers (analyst, strategist, PM, runner) need not reach into
    the validation_tool_mcp submodule."""
    import alphamind.risk_guardrails.state_delivery as pkg

    assert hasattr(pkg, "build_validate_guardrail_mcp_server")
    assert hasattr(pkg, "build_initial_validation_state")
    assert "build_validate_guardrail_mcp_server" in pkg.__all__
    assert "build_initial_validation_state" in pkg.__all__


# ---------------------------------------------------------------------------
# 10. validate_guardrail_batch MCP tool — story 01b (ALP-625)
# ---------------------------------------------------------------------------


def _batch_args(proposals: list[dict[str, Any]]) -> dict[str, Any]:
    """Shape the batch tool's input dict."""
    return {"proposals": proposals}


def test_factory_returns_both_tool_names() -> None:
    """build_validate_guardrail_mcp_server returns an allowed-tool list with
    both the single-call and batch tool names; both close over the same
    state cell."""
    state = _make_state()
    _, allowed_tools = build_validate_guardrail_mcp_server(state)
    assert allowed_tools == [
        "mcp__alphamind_decision_validation__validate_guardrail",
        "mcp__alphamind_decision_validation__validate_guardrail_batch",
    ]


@pytest.mark.asyncio
async def test_batch_empty_proposals_returns_pass_and_does_not_advance_cell() -> None:
    """An empty-batch call returns aggregate PASS and an empty per_proposal
    list; the cell is not advanced — a subsequent single-call still sees
    itself as proposal #1."""
    import json

    state = _make_state()
    mcp_servers, _ = build_validate_guardrail_mcp_server(state)
    server = mcp_servers["alphamind_decision_validation"]["instance"]

    text, is_error = await _invoke_mcp_tool(
        server, "validate_guardrail_batch", _batch_args([])
    )
    assert not is_error
    payload = json.loads(text)
    assert payload["overall"] == "PASS"
    assert payload["per_proposal"] == []
    assert "cumulative_impact_note" in payload
    assert "checked_at" in payload

    text2, _ = await _invoke_mcp_tool(server, "validate_guardrail", _equity_open_args())
    p2 = json.loads(text2)
    assert p2["proposal_index_in_invocation"] == 1


@pytest.mark.asyncio
async def test_batch_all_pass_advances_cell_one_delta_per_proposal() -> None:
    """When all per-proposal entries PASS, the wrapper advances the cell once
    per proposal — a subsequent single-call sees its index as N+1 where N
    equals the batch size."""
    import json

    # Per-sector sector resolver + extra ticker in the market so two equity
    # OPENs in distinct sectors both pass without breaching sector concentration.
    def per_sector(ticker: str) -> str:
        return {"AAPL": "tech", "JPM": "financials"}.get(ticker, "tech")

    cfg = _config()
    state = _make_state(
        config=cfg,
        market=MarketInputs(
            underlying_prices=MappingProxyType(
                {u: _SPOT for u in ("AAPL", "NVDA", "ABC", "JPM")}
            ),
            risk_free_rate=_RISK_FREE_RATE,
            iv_provider=_atm_provider("AAPL"),
            as_of=_AS_OF,
        ),
    )
    # Override sector_resolver via a fresh state build.
    from alphamind.risk_guardrails.state_delivery.validation_tool import (
        ValidationToolState,
    )

    state = ValidationToolState(
        invocation_id=state.invocation_id,
        starting_snapshot=state.starting_snapshot,
        starting_risk_budget=state.starting_risk_budget,
        starting_active_risk_parameters=state.starting_active_risk_parameters,
        profile_feature_flags=state.profile_feature_flags,
        library_config=state.library_config,
        library_market=state.library_market,
        sector_resolver=per_sector,
        accumulated_deltas=(),
    )

    mcp_servers, _ = build_validate_guardrail_mcp_server(state)
    server = mcp_servers["alphamind_decision_validation"]["instance"]

    text, is_error = await _invoke_mcp_tool(
        server,
        "validate_guardrail_batch",
        _batch_args(
            [
                _equity_open_args(ticker="AAPL"),
                _equity_open_args(ticker="JPM"),
            ]
        ),
    )
    assert not is_error
    payload = json.loads(text)
    assert payload["overall"] == "PASS"
    assert len(payload["per_proposal"]) == 2

    # A subsequent single-call should report itself as proposal #3
    # (two batch proposals advanced the cell).
    text2, _ = await _invoke_mcp_tool(server, "validate_guardrail", _equity_open_args(ticker="NVDA"))
    p2 = json.loads(text2)
    assert p2["proposal_index_in_invocation"] == 3


@pytest.mark.asyncio
async def test_batch_aggregate_fail_does_not_advance_cell() -> None:
    """A batch where the aggregate is FAIL leaves the cell unchanged — no
    partial advancement of the PASSed prefix."""
    import json

    # First proposal PASSes; second FAILs because options are disabled. The
    # batch wrapper must NOT advance the cell by the first proposal.
    state = _make_state(config=_config(options_enabled=False))
    mcp_servers, _ = build_validate_guardrail_mcp_server(state)
    server = mcp_servers["alphamind_decision_validation"]["instance"]

    text, _ = await _invoke_mcp_tool(
        server,
        "validate_guardrail_batch",
        _batch_args(
            [
                _equity_open_args(ticker="AAPL"),
                _option_open_args(ticker="AAPL"),
            ]
        ),
    )
    payload = json.loads(text)
    assert payload["overall"] == "FAIL"

    # Subsequent single-call should see itself as proposal #1 — the batch
    # FAIL did NOT advance the cell.
    text2, _ = await _invoke_mcp_tool(server, "validate_guardrail", _equity_open_args(ticker="NVDA"))
    p2 = json.loads(text2)
    assert p2["proposal_index_in_invocation"] == 1


@pytest.mark.asyncio
async def test_batch_aggregate_unavailable_does_not_advance_cell() -> None:
    """A batch containing an UNAVAILABLE per-proposal entry produces aggregate
    UNAVAILABLE; the cell is not advanced; the per-proposal entries surface the
    gap on the right requests."""
    import json

    state = _make_state()  # market: AAPL/NVDA/ABC
    mcp_servers, _ = build_validate_guardrail_mcp_server(state)
    server = mcp_servers["alphamind_decision_validation"]["instance"]

    text, _ = await _invoke_mcp_tool(
        server,
        "validate_guardrail_batch",
        _batch_args(
            [
                _equity_open_args(ticker="AAPL"),
                _equity_open_args(ticker="CSCO"),  # outside market -> UNAVAILABLE
            ]
        ),
    )
    payload = json.loads(text)
    assert payload["overall"] == "UNAVAILABLE"
    overalls = [p["overall"] for p in payload["per_proposal"]]
    assert overalls == ["PASS", "UNAVAILABLE"]
    assert payload["per_proposal"][1]["unavailable_reason"] == "missing_market_price"

    text2, _ = await _invoke_mcp_tool(server, "validate_guardrail", _equity_open_args(ticker="NVDA"))
    p2 = json.loads(text2)
    assert p2["proposal_index_in_invocation"] == 1


@pytest.mark.asyncio
async def test_batch_shares_state_cell_with_single_call() -> None:
    """Both tools close over the same state cell — a prior single-call PASS
    is visible in the batch tool's per-proposal cumulative_impact_note."""
    import json

    def per_sector(ticker: str) -> str:
        return {"AAPL": "tech", "JPM": "financials"}.get(ticker, "tech")

    from alphamind.risk_guardrails.state_delivery.validation_tool import (
        ValidationToolState,
    )

    base = _make_state(
        market=MarketInputs(
            underlying_prices=MappingProxyType(
                {u: _SPOT for u in ("AAPL", "NVDA", "ABC", "JPM")}
            ),
            risk_free_rate=_RISK_FREE_RATE,
            iv_provider=_atm_provider("AAPL"),
            as_of=_AS_OF,
        ),
    )
    state = ValidationToolState(
        invocation_id=base.invocation_id,
        starting_snapshot=base.starting_snapshot,
        starting_risk_budget=base.starting_risk_budget,
        starting_active_risk_parameters=base.starting_active_risk_parameters,
        profile_feature_flags=base.profile_feature_flags,
        library_config=base.library_config,
        library_market=base.library_market,
        sector_resolver=per_sector,
        accumulated_deltas=(),
    )

    mcp_servers, _ = build_validate_guardrail_mcp_server(state)
    server = mcp_servers["alphamind_decision_validation"]["instance"]

    # First, a single-call PASS advances the cell to next-index=2.
    text1, _ = await _invoke_mcp_tool(server, "validate_guardrail", _equity_open_args(ticker="AAPL"))
    assert json.loads(text1)["overall"] == "PASS"

    # The batch tool's per-proposal entries should reflect the prior call —
    # the first batch proposal is proposal #2.
    text2, _ = await _invoke_mcp_tool(
        server,
        "validate_guardrail_batch",
        _batch_args([_equity_open_args(ticker="JPM")]),
    )
    payload = json.loads(text2)
    assert payload["per_proposal"][0]["proposal_index_in_invocation"] == 2


@pytest.mark.asyncio
async def test_batch_bad_input_returns_is_error_with_field_paths() -> None:
    """An invalid per-proposal entry (e.g. options missing strike) is rejected
    by Pydantic; the handler surfaces it as is_error=True."""
    state = _make_state()
    mcp_servers, _ = build_validate_guardrail_mcp_server(state)
    server = mcp_servers["alphamind_decision_validation"]["instance"]

    bad_proposal = {
        "instrument": {
            "ticker": "AAPL",
            "asset_type": "options",
            "direction": "long",
            # missing strike/expiration/contract_type
        },
        "size": {"quantity": 1, "dollar_value": 100.0, "premium_at_risk_usd": 100.0},
        "action": "OPEN",
    }
    text, is_error = await _invoke_mcp_tool(
        server, "validate_guardrail_batch", _batch_args([bad_proposal])
    )
    assert is_error
    assert "OPTIONS asset_type requires" in text or "strike" in text
