"""Smoke tests for the five ALP-650 subprocess wrappers.

Each wrapper is exercised in two flavors:

* the success path — :func:`_run_worker` is patched to return a
  base64-pickle payload that the wrapper decodes back into the typed
  ``HarnessSuccess``;
* the failure path — :func:`_run_worker` returns an error envelope and
  the wrapper raises the matching typed ``HarnessFailure``.

The tests deliberately stub ``_run_worker`` rather than spawning a real
subprocess: ALP-650 verification covers the *parent-side glue*
(payload-construction, typed reconstruction, failure protocol). Real
subprocess execution is covered by the operator's ``--debug-e2e``
invocation.

Where the wrapper's input-pickling step would fail on test stand-ins
(``MagicMock`` is unpicklable, ``ValidationToolState`` requires the
full upstream fixture chain), we additionally patch the pickle helpers
to no-ops so the smoke test scopes to the failure-protocol decode.
"""

from __future__ import annotations

import asyncio
import base64
import pickle
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import alphamind.analysis._sdk_subprocess as sp
from alphamind._kernel.progress import NOOP_PROGRESS_EMITTER
from alphamind.analysis._harness_core import (
    ContextOverflowFailure,
    MalformedOutputFailure,
    SDKFailure,
    TimeoutFailure,
)
from alphamind.analysis._shared import TokensUsed

# ---------------------------------------------------------------------------
# Common fakes (module-level so pickle.dumps can locate them by qualified name)
# ---------------------------------------------------------------------------


class _FakeSynthesizerReader:
    """Picklable minimal stand-in for ``SynthesizerPortfolioStateReader``."""

    def get_positions_summary(self) -> tuple[Any, ...]:
        return ()

    def get_active_theses_summary(self) -> tuple[Any, ...]:
        return ()

    def get_exposure_snapshot(self) -> Any:
        return None


def _zero_tokens() -> TokensUsed:
    return TokensUsed(input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_write_tokens=0)


def _success_envelope(harness_success: Any) -> dict[str, Any]:
    return {
        "kind": "success",
        "success_pickle": base64.b64encode(pickle.dumps(harness_success)).decode("ascii"),
    }


def _failure_envelope(
    *,
    error_type: str = "MalformedOutputFailure",
    error_msg: str = "stub",
    agent_name: str = "agent",
    invocation_id: str = "INV-1",
) -> dict[str, Any]:
    return {
        "kind": "failure",
        "error_type": error_type,
        "error_msg": error_msg,
        "agent_name": agent_name,
        "invocation_id": invocation_id,
    }


def _stub_agent_config() -> Any:
    """Bypass ``BaseAgentConfig``'s ``prompt_path_exists`` validator for tests.

    The wrappers only consume ``agent_config.model_dump(mode="json")``;
    the validator's filesystem check is irrelevant to the subprocess-glue
    smoke tests. ``model_construct`` is Pydantic's documented escape
    hatch for tests that need a typed instance without paying the
    validation cost.
    """
    from alphamind.config.models.agents import BaseAgentConfig

    return BaseAgentConfig.model_construct(
        model="claude-sonnet-4-5",
        prompt="stub.md",
        latency_budget_seconds=60,
        context_token_budget=10_000,
        output_token_budget=4_000,
        tools=[],
    )


# ---------------------------------------------------------------------------
# __all__
# ---------------------------------------------------------------------------


def test_all_enumerates_seven_wrappers() -> None:
    """The seven harness wrappers must all be re-exported."""
    expected = {
        "invoke_domain_researcher_in_subprocess",
        "invoke_qualitative_researcher_in_subprocess",
        "invoke_analyst_in_subprocess",
        "invoke_strategist_in_subprocess",
        "invoke_portfolio_manager_in_subprocess",
        "invoke_synthesizer_in_subprocess",
        "invoke_adaptive_researcher_in_subprocess",
    }
    assert expected.issubset(set(sp.__all__))


# ---------------------------------------------------------------------------
# Pickle helpers
# ---------------------------------------------------------------------------


def test_encode_decode_pickle_round_trip() -> None:
    encoded = sp._encode_pickle({"a": 1, "b": (2, 3)})
    decoded = sp._decode_pickle(encoded)
    assert decoded == {"a": 1, "b": (2, 3)}


def test_sql_iv_provider_shim_rebuilds_via_realized_vol() -> None:
    """``_prepare_market_inputs_for_pickle`` swaps ``SqlOptionsIvProvider`` for a shim."""
    from sqlalchemy.orm import sessionmaker

    from alphamind.risk_guardrails.guardrail_evaluation import MarketInputs
    from alphamind.risk_guardrails.guardrail_evaluation.iv_sourcing import (
        RealizedVolEntry,
        SqlOptionsIvProvider,
    )

    realized = {"AAPL": RealizedVolEntry(underlying="AAPL", trailing_30d_realized_vol=0.25)}
    provider = SqlOptionsIvProvider(sync_session_factory=sessionmaker(), realized_vol=realized)
    market_inputs = MarketInputs(
        underlying_prices={"AAPL": 200.0},
        risk_free_rate=0.04,
        iv_provider=provider,
        as_of=datetime.now(UTC),
    )

    prepared = sp._prepare_market_inputs_for_pickle(market_inputs)
    assert isinstance(prepared.iv_provider, sp._SqlIvProviderShim)
    assert prepared.iv_provider.realized_vol == realized

    # Pickle round-trip succeeds against the shim.
    encoded = sp._encode_pickle(prepared)
    decoded = sp._decode_pickle(encoded)
    assert isinstance(decoded.iv_provider, sp._SqlIvProviderShim)


def test_market_inputs_with_fixture_provider_passes_through_unchanged() -> None:
    """Only SqlOptionsIvProvider is swapped; fixture providers pickle as-is and pass through."""
    from alphamind.risk_guardrails.guardrail_evaluation import MarketInputs
    from alphamind.risk_guardrails.guardrail_evaluation.iv_sourcing import FixtureIvProvider

    fixture = FixtureIvProvider(surface={}, realized_vol={})
    market_inputs = MarketInputs(
        underlying_prices={"AAPL": 200.0},
        risk_free_rate=0.04,
        iv_provider=fixture,
        as_of=datetime.now(UTC),
    )
    prepared = sp._prepare_market_inputs_for_pickle(market_inputs)
    assert prepared.iv_provider is fixture


def test_sector_resolver_pickles_with_data() -> None:
    """The refactored ``SectorResolver`` survives pickle + unpickle with its lookup intact."""
    from alphamind.config.assets_views import SectorResolver

    resolver = SectorResolver({"AAPL": "tech", "JPM": "financials"})
    round_trip = pickle.loads(pickle.dumps(resolver))
    assert round_trip("AAPL") == "tech"
    assert round_trip("UNKN") == "UNCLASSIFIED"


def test_borrow_cost_resolver_pickles_with_data() -> None:
    """The refactored ``BorrowCostResolver`` survives pickle + unpickle with its lookup intact."""
    from alphamind.risk_guardrails.borrow_cost import BorrowCostResolver

    resolver = BorrowCostResolver({"AAPL": 15.0})
    round_trip = pickle.loads(pickle.dumps(resolver))
    assert round_trip("AAPL") == 15.0
    assert round_trip("UNKN") is None


# ---------------------------------------------------------------------------
# Synthesizer wrapper
# ---------------------------------------------------------------------------


def test_synthesizer_wrapper_success_path() -> None:
    """Wrapper serializes its inputs and pickle-round-trips HarnessSuccess from worker."""
    from alphamind.analysis.synthesizer.harness import HarnessSuccess

    success = HarnessSuccess(
        response_text="stub prose",
        tokens_used=_zero_tokens(),
        tool_calls_used=0,
        wall_clock_seconds=0.1,
        stop_reason="end_turn",
    )
    captured: dict[str, Any] = {}

    async def fake_run_worker(payload: dict[str, Any]) -> dict[str, Any]:
        captured.update(payload)
        return _success_envelope(success)

    with patch.object(sp, "_run_worker", fake_run_worker):
        result = asyncio.run(
            sp.invoke_synthesizer_in_subprocess(
                agent_config=_stub_agent_config(),
                user_message="hello",
                invocation_id="INV-1",
                portfolio_reader=_FakeSynthesizerReader(),
                progress=NOOP_PROGRESS_EMITTER,
            )
        )

    assert result.response_text == "stub prose"
    assert captured["agent"] == "synthesizer"
    assert captured["invocation_id"] == "INV-1"
    assert "portfolio_reader_pickle" in captured


def test_synthesizer_wrapper_failure_path() -> None:
    """A ``TimeoutFailure`` envelope is reconstructed and raised on the parent side."""

    async def fake_run_worker(_: dict[str, Any]) -> dict[str, Any]:
        return _failure_envelope(error_type="TimeoutFailure", error_msg="exceeded")

    with patch.object(sp, "_run_worker", fake_run_worker), pytest.raises(TimeoutFailure):
        asyncio.run(
            sp.invoke_synthesizer_in_subprocess(
                agent_config=_stub_agent_config(),
                user_message="hello",
                invocation_id="INV-1",
                portfolio_reader=_FakeSynthesizerReader(),
            )
        )


# ---------------------------------------------------------------------------
# Failure-only smoke tests for the heavy-state wrappers
#
# Building real ValidationToolState / PortfolioManagerView etc. requires
# deep upstream fixture chains; the smoke coverage here scopes to the
# failure-protocol glue (``_raise_failure``). The wrapper's input
# pickling is short-circuited by patching the pickle helpers, so we can
# pass ``MagicMock`` inputs without exercising the live serialization.
# Success-path round-trip for these heavy wrappers is covered by the
# operator's ``--debug-e2e`` invocation (per the issue's "Manual"
# verification step).
# ---------------------------------------------------------------------------


def _patch_pickle_helpers_to_noop() -> Any:
    """Skip the wrapper's input pickling so tests can pass MagicMock inputs.

    The wrapper still calls ``_run_worker`` (patched separately to return
    a failure envelope), so the failure-protocol path is what we exercise.
    """
    return patch.multiple(
        sp,
        _encode_pickle=lambda _obj: "stub-base64",
        _prepare_validation_state_for_pickle=lambda obj: obj,
        _prepare_submit_envelope_state_for_pickle=lambda obj: obj,
        _prepare_market_inputs_for_pickle=lambda obj: obj,
    )


def test_analyst_wrapper_raises_on_context_overflow_failure() -> None:
    async def fake_run_worker(_: dict[str, Any]) -> dict[str, Any]:
        return _failure_envelope(error_type="ContextOverflowFailure")

    with (
        _patch_pickle_helpers_to_noop(),
        patch.object(sp, "_run_worker", fake_run_worker),
        pytest.raises(ContextOverflowFailure),
    ):
        asyncio.run(
            sp.invoke_analyst_in_subprocess(
                agent_config=_stub_agent_config(),
                user_message="bundle",
                invocation_id="INV-1",
                initial_validation_state=MagicMock(),
                retrieval_store=MagicMock(),
                active_sectors=frozenset({"tech"}),
            )
        )


def test_strategist_wrapper_raises_on_malformed_output_failure() -> None:
    async def fake_run_worker(_: dict[str, Any]) -> dict[str, Any]:
        return _failure_envelope(error_type="MalformedOutputFailure")

    with (
        _patch_pickle_helpers_to_noop(),
        patch.object(sp, "_run_worker", fake_run_worker),
        pytest.raises(MalformedOutputFailure),
    ):
        asyncio.run(
            sp.invoke_strategist_in_subprocess(
                user_message="bundle",
                system_prompt="sp",
                invocation_id="INV-1",
                agent_config=_stub_agent_config(),
                validation_state=MagicMock(),
                retrieval_store=MagicMock(),
                active_sectors=frozenset({"tech"}),
            )
        )


def test_pm_wrapper_raises_on_sdk_failure() -> None:
    async def fake_run_worker(_: dict[str, Any]) -> dict[str, Any]:
        return _failure_envelope(error_type="SDKFailure")

    with (
        _patch_pickle_helpers_to_noop(),
        patch.object(sp, "_run_worker", fake_run_worker),
        pytest.raises(SDKFailure),
    ):
        asyncio.run(
            sp.invoke_portfolio_manager_in_subprocess(
                agent_config=_stub_agent_config(),
                user_message="bundle",
                invocation_id="INV-1",
                initial_validation_state=MagicMock(),
                initial_submit_envelope_state=MagicMock(),
                retrieval_store=MagicMock(),
                thesis_component_reader=MagicMock(),
                pre_processor_bundle=MagicMock(),
                pm_view=MagicMock(),
                active_sectors=frozenset({"tech"}),
                halt_mode=False,
                sector_resolver=MagicMock(),
                library_config=MagicMock(),
                library_market=MagicMock(),
            )
        )


# ---------------------------------------------------------------------------
# Adaptive researcher wrapper
# ---------------------------------------------------------------------------


def test_adaptive_researcher_wrapper_failure_path() -> None:
    """A ``SDKFailure`` envelope reconstructs and raises on the parent side."""

    async def fake_run_worker(_: dict[str, Any]) -> dict[str, Any]:
        return _failure_envelope(error_type="SDKFailure")

    with (
        _patch_pickle_helpers_to_noop(),
        patch.object(sp, "_run_worker", fake_run_worker),
        pytest.raises(SDKFailure),
    ):
        asyncio.run(
            sp.invoke_adaptive_researcher_in_subprocess(
                agent_config=_stub_agent_config(),
                user_message="input bundle",
                invocation_id="INV-AR-1",
                universe=frozenset({"AAPL", "JPM"}),
                sector_briefs=(),
                qualitative_brief=MagicMock(),
                correlation_regime_brief=MagicMock(),
            )
        )


def test_adaptive_researcher_wrapper_payload_carries_universe_sorted() -> None:
    """Adaptive wrapper marshals ``universe`` as a sorted list for deterministic shape."""
    captured: dict[str, Any] = {}

    async def fake_run_worker(payload: dict[str, Any]) -> dict[str, Any]:
        captured.update(payload)
        return _failure_envelope()

    with (
        _patch_pickle_helpers_to_noop(),
        patch.object(sp, "_run_worker", fake_run_worker),
        pytest.raises(MalformedOutputFailure),
    ):
        asyncio.run(
            sp.invoke_adaptive_researcher_in_subprocess(
                agent_config=_stub_agent_config(),
                user_message="bundle",
                invocation_id="INV-AR-1",
                universe=frozenset({"JPM", "AAPL"}),
                sector_briefs=(),
                qualitative_brief=MagicMock(),
                correlation_regime_brief=MagicMock(),
            )
        )

    assert captured["agent"] == "adaptive_researcher"
    assert captured["universe"] == ["AAPL", "JPM"]
