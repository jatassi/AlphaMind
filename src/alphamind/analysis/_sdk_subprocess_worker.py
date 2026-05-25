"""SDK subprocess worker — entry point for one isolated harness invocation.

Spawned by :mod:`alphamind.analysis._sdk_subprocess` via
``python -m alphamind.analysis._sdk_subprocess_worker``. Reads a JSON
payload from stdin, dispatches to the matching ``invoke_<agent>``,
and writes a single JSON line of result (or typed-error metadata) to
stdout before exiting.

The subprocess boundary is the workaround for the SDK's process-level
state leakage that causes intermittent ``stop_reason: null`` /
zero-token stalls on the second-or-later SDK call in a Python process.
Each invocation gets a fresh process and therefore a fresh SDK module /
CLI subprocess context.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alphamind._kernel.progress import NOOP_PROGRESS_EMITTER, ProgressEmitter
from alphamind.analysis._harness_core import (
    ContextOverflowFailure,
    MalformedOutputFailure,
    SDKFailure,
    TimeoutFailure,
)
from alphamind.analysis._sdk_subprocess import (
    _decode_pickle,
    _encode_pickle,
    _SqlIvProviderShim,
)
from alphamind.analysis._shared import Sector
from alphamind.analysis.adaptive_research.harness import invoke_adaptive_researcher
from alphamind.analysis.domain_researchers.harness import invoke_domain_researcher
from alphamind.analysis.qualitative_research.harness import invoke_qualitative_researcher
from alphamind.analysis.synthesizer.harness import invoke_synthesizer
from alphamind.config.models.agents import BaseAgentConfig
from alphamind.decision.analyst.harness import invoke_analyst
from alphamind.decision.portfolio_manager.harness import invoke_pm
from alphamind.decision.strategist.harness import run_strategist_harness
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.risk_guardrails.guardrail_evaluation import MarketInputs
from alphamind.risk_guardrails.guardrail_evaluation.iv_sourcing import SqlOptionsIvProvider
from alphamind.risk_guardrails.state_delivery.validation_tool import ValidationToolState


class _JsonlAppender:
    """Worker-local JSONL progress emitter.

    Implements :class:`ProgressEmitter` Protocol via append-only writes to
    the same ``progress.jsonl`` the parent's
    :class:`alphamind.scheduler.debug_e2e.jsonl_emitter.JsonlProgressEmitter`
    writes — matching its on-disk format so a single archive file
    interleaves parent ``phase_*`` events with worker ``agent_request`` /
    ``agent_response`` events.

    Inlined here rather than imported from ``scheduler.debug_e2e`` to
    keep the worker module within the ``analysis`` layer of the
    composition-root-layering import contract — the subprocess entry
    sits at the same architectural level as the harness it dispatches.
    """

    def __init__(self, *, path: Path) -> None:
        self._path = path

    def phase_start(self, phase: str) -> None:
        self._write({"event": "phase_start", "phase": phase})

    def phase_done(self, phase: str, **fields: Any) -> None:
        self._write({"event": "phase_done", "phase": phase, **fields})

    def agent_request(self, **fields: Any) -> None:
        self._write({"event": "agent_request", **fields})

    def agent_response(self, **fields: Any) -> None:
        self._write({"event": "agent_response", **fields})

    def _write(self, payload: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload["timestamp"] = datetime.now(UTC).isoformat()
        with self._path.open("a", encoding="utf-8", newline="") as f:
            f.write(json.dumps(payload, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())


def _resolve_progress(progress_jsonl_path: str | None) -> ProgressEmitter:
    if progress_jsonl_path is None:
        return NOOP_PROGRESS_EMITTER
    return _JsonlAppender(path=Path(progress_jsonl_path))


async def _run_domain_researcher(payload: dict[str, Any]) -> dict[str, Any]:
    """Invoke ``invoke_domain_researcher`` in this fresh process and serialize the result."""
    # Parent marshals the resolved (per-trigger-override-applied)
    # ``agent_config`` as a dict; reconstruct here so the worker uses the
    # same Pydantic instance the parent would have.
    sector = Sector(payload["sector"])
    agent_config = BaseAgentConfig.model_validate(payload["agent_config"])
    progress = _resolve_progress(payload.get("progress_jsonl_path"))
    archive_root_str = payload.get("archive_root")
    archive_root = Path(archive_root_str) if archive_root_str else None

    try:
        result = await invoke_domain_researcher(
            agent_config=agent_config,
            sector=sector,
            user_message=payload["user_message"],
            invocation_id=payload["invocation_id"],
            archive_root=archive_root,
            progress=progress,
            phase=payload.get("phase", "domain_researchers"),
        )
    except MalformedOutputFailure as exc:
        return {
            "kind": "failure",
            "error_type": "MalformedOutputFailure",
            "error_msg": str(exc),
            "agent_name": exc.agent_name,
            "invocation_id": exc.invocation_id,
            "raw_response_initial": exc.raw_response_initial,
            "raw_response_retry": exc.raw_response_retry,
        }
    except ContextOverflowFailure as exc:
        return {
            "kind": "failure",
            "error_type": "ContextOverflowFailure",
            "error_msg": str(exc),
            "agent_name": exc.agent_name,
            "invocation_id": exc.invocation_id,
            "raw_response": exc.raw_response,
        }
    except TimeoutFailure as exc:
        return {
            "kind": "failure",
            "error_type": "TimeoutFailure",
            "error_msg": str(exc),
            "agent_name": exc.agent_name,
            "invocation_id": exc.invocation_id,
        }
    except SDKFailure as exc:
        return {
            "kind": "failure",
            "error_type": "SDKFailure",
            "error_msg": str(exc),
            "agent_name": exc.agent_name,
            "invocation_id": exc.invocation_id,
        }

    return {
        "kind": "success",
        "brief": result.brief.model_dump(mode="json"),
        "raw_response": result.raw_response,
        "retry_count": result.retry_count,
        "tokens_used": result.tokens_used.model_dump(mode="json"),
        "wall_clock_seconds": result.wall_clock_seconds,
    }


def _failure_payload(exc: Exception) -> dict[str, Any]:
    """Translate a known :class:`HarnessFailure` subclass into the wire dict."""
    if isinstance(exc, MalformedOutputFailure):
        return {
            "kind": "failure",
            "error_type": "MalformedOutputFailure",
            "error_msg": str(exc),
            "agent_name": exc.agent_name,
            "invocation_id": exc.invocation_id,
            "raw_response_initial": exc.raw_response_initial,
            "raw_response_retry": exc.raw_response_retry,
        }
    if isinstance(exc, ContextOverflowFailure):
        return {
            "kind": "failure",
            "error_type": "ContextOverflowFailure",
            "error_msg": str(exc),
            "agent_name": exc.agent_name,
            "invocation_id": exc.invocation_id,
            "raw_response": exc.raw_response,
        }
    if isinstance(exc, TimeoutFailure):
        return {
            "kind": "failure",
            "error_type": "TimeoutFailure",
            "error_msg": str(exc),
            "agent_name": exc.agent_name,
            "invocation_id": exc.invocation_id,
        }
    if isinstance(exc, SDKFailure):
        return {
            "kind": "failure",
            "error_type": "SDKFailure",
            "error_msg": str(exc),
            "agent_name": exc.agent_name,
            "invocation_id": exc.invocation_id,
        }
    # Unknown exception — surface as SDKFailure so the parent's fail-closed path engages.
    return {
        "kind": "failure",
        "error_type": "SDKFailure",
        "error_msg": f"{type(exc).__name__}: {exc}",
        "agent_name": getattr(exc, "agent_name", "unknown"),
        "invocation_id": getattr(exc, "invocation_id", "unknown"),
    }


async def _run_qualitative_researcher(payload: dict[str, Any]) -> dict[str, Any]:
    """Invoke ``invoke_qualitative_researcher`` in this fresh process and serialize the result.

    Opens a fresh SQLAlchemy session against ``DATABASE_PATH`` so the
    in-process MCP server (``alphamind_qualitative``) wires the
    ``news_search`` / ``prediction_markets`` / ``earnings_commentary``
    tools against the same DB the parent uses. Session is committed
    and closed before returning.
    """
    agent_config = BaseAgentConfig.model_validate(payload["agent_config"])
    progress = _resolve_progress(payload.get("progress_jsonl_path"))
    archive_root_str = payload.get("archive_root")
    archive_root = Path(archive_root_str) if archive_root_str else None
    universe = frozenset(payload["universe"])

    engine = make_engine()
    session_factory = make_session_factory(engine)
    session = session_factory()
    try:
        try:
            result = await invoke_qualitative_researcher(
                agent_config=agent_config,
                user_message=payload["user_message"],
                invocation_id=payload["invocation_id"],
                session=session,
                universe=universe,
                archive_root=archive_root,
                progress=progress,
                phase=payload.get("phase", "qualitative"),
            )
        except (MalformedOutputFailure, ContextOverflowFailure, TimeoutFailure, SDKFailure) as exc:
            return _failure_payload(exc)

        return {
            "kind": "success",
            "brief": result.brief.model_dump(mode="json"),
            "raw_response": result.raw_response,
            "retry_count": result.retry_count,
            "tokens_used": result.tokens_used.model_dump(mode="json"),
            "tool_calls_used": result.tool_calls_used,
            "wall_clock_seconds": result.wall_clock_seconds,
        }
    finally:
        session.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# ALP-650 helpers + dispatchers
# ---------------------------------------------------------------------------


def _rehydrate_market_inputs(
    market_inputs: MarketInputs, *, sync_session_factory: Any
) -> MarketInputs:
    """Replace a :class:`_SqlIvProviderShim` with a real provider against *sync_session_factory*."""
    if isinstance(market_inputs.iv_provider, _SqlIvProviderShim):
        provider = SqlOptionsIvProvider(
            sync_session_factory=sync_session_factory,
            realized_vol=market_inputs.iv_provider.realized_vol,
        )
        return dataclasses.replace(market_inputs, iv_provider=provider)
    return market_inputs


def _rehydrate_validation_state(
    state: ValidationToolState, *, sync_session_factory: Any
) -> ValidationToolState:
    """Replace the embedded shim provider with a fresh SqlOptionsIvProvider."""
    return state.model_copy(
        update={
            "library_market": _rehydrate_market_inputs(
                state.library_market, sync_session_factory=sync_session_factory
            ),
        },
    )


def _success_payload(harness_success: Any) -> dict[str, Any]:
    """Wire-format envelope for a successful worker invocation."""
    return {"kind": "success", "success_pickle": _encode_pickle(harness_success)}


async def _run_synthesizer(payload: dict[str, Any]) -> dict[str, Any]:
    agent_config = BaseAgentConfig.model_validate(payload["agent_config"])
    progress = _resolve_progress(payload.get("progress_jsonl_path"))
    archive_root_str = payload.get("archive_root")
    archive_root = Path(archive_root_str) if archive_root_str else None
    portfolio_reader = _decode_pickle(payload["portfolio_reader_pickle"])

    try:
        result = await invoke_synthesizer(
            agent_config=agent_config,
            user_message=payload["user_message"],
            invocation_id=payload["invocation_id"],
            portfolio_reader=portfolio_reader,
            archive_root=archive_root,
            progress=progress,
            phase=payload.get("phase", "synthesizer"),
        )
    except (MalformedOutputFailure, ContextOverflowFailure, TimeoutFailure, SDKFailure) as exc:
        return _failure_payload(exc)

    return _success_payload(result)


async def _run_adaptive_researcher(payload: dict[str, Any]) -> dict[str, Any]:
    agent_config = BaseAgentConfig.model_validate(payload["agent_config"])
    progress = _resolve_progress(payload.get("progress_jsonl_path"))
    archive_root_str = payload.get("archive_root")
    archive_root = Path(archive_root_str) if archive_root_str else None
    universe = frozenset(payload["universe"])
    sector_briefs = _decode_pickle(payload["sector_briefs_pickle"])
    qualitative_brief = _decode_pickle(payload["qualitative_brief_pickle"])
    correlation_regime_brief = _decode_pickle(payload["correlation_regime_brief_pickle"])

    engine = make_engine()
    session_factory = make_session_factory(engine)
    session = session_factory()
    try:
        try:
            result = await invoke_adaptive_researcher(
                agent_config=agent_config,
                user_message=payload["user_message"],
                invocation_id=payload["invocation_id"],
                session=session,
                universe=universe,
                sector_briefs=sector_briefs,
                qualitative_brief=qualitative_brief,
                correlation_regime_brief=correlation_regime_brief,
                archive_root=archive_root,
                progress=progress,
                phase=payload.get("phase", "adaptive"),
            )
        except (MalformedOutputFailure, ContextOverflowFailure, TimeoutFailure, SDKFailure) as exc:
            return _failure_payload(exc)
        return _success_payload(result)
    finally:
        session.close()
        engine.dispose()


async def _run_analyst(payload: dict[str, Any]) -> dict[str, Any]:
    agent_config = BaseAgentConfig.model_validate(payload["agent_config"])
    progress = _resolve_progress(payload.get("progress_jsonl_path"))
    archive_root_str = payload.get("archive_root")
    archive_root = Path(archive_root_str) if archive_root_str else None
    retrieval_store = _decode_pickle(payload["retrieval_store_pickle"])
    active_sectors = frozenset(payload["active_sectors"])

    engine = make_engine()
    sync_session_factory = make_session_factory(engine)
    try:
        validation_state = _rehydrate_validation_state(
            _decode_pickle(payload["initial_validation_state_pickle"]),
            sync_session_factory=sync_session_factory,
        )
        try:
            result = await invoke_analyst(
                agent_config=agent_config,
                user_message=payload["user_message"],
                invocation_id=payload["invocation_id"],
                initial_validation_state=validation_state,
                retrieval_store=retrieval_store,
                active_sectors=active_sectors,
                archive_root=archive_root,
                progress=progress,
                phase=payload.get("phase", "analyst"),
            )
        except (MalformedOutputFailure, ContextOverflowFailure, TimeoutFailure, SDKFailure) as exc:
            return _failure_payload(exc)
        return _success_payload(result)
    finally:
        engine.dispose()


async def _run_strategist(payload: dict[str, Any]) -> dict[str, Any]:
    agent_config = BaseAgentConfig.model_validate(payload["agent_config"])
    progress = _resolve_progress(payload.get("progress_jsonl_path"))
    archive_root_str = payload.get("archive_root")
    archive_root = Path(archive_root_str) if archive_root_str else None
    retrieval_store = _decode_pickle(payload["retrieval_store_pickle"])
    active_sectors = frozenset(payload["active_sectors"])

    engine = make_engine()
    sync_session_factory = make_session_factory(engine)
    try:
        validation_state = _rehydrate_validation_state(
            _decode_pickle(payload["validation_state_pickle"]),
            sync_session_factory=sync_session_factory,
        )
        try:
            result = await run_strategist_harness(
                user_message=payload["user_message"],
                system_prompt=payload["system_prompt"],
                invocation_id=payload["invocation_id"],
                agent_config=agent_config,
                validation_state=validation_state,
                retrieval_store=retrieval_store,
                active_sectors=active_sectors,
                archive_root=archive_root,
                progress=progress,
                phase=payload.get("phase", "strategist"),
            )
        except (MalformedOutputFailure, ContextOverflowFailure, TimeoutFailure, SDKFailure) as exc:
            return _failure_payload(exc)
        return _success_payload(result)
    finally:
        engine.dispose()


async def _run_portfolio_manager(payload: dict[str, Any]) -> dict[str, Any]:
    agent_config = BaseAgentConfig.model_validate(payload["agent_config"])
    progress = _resolve_progress(payload.get("progress_jsonl_path"))
    archive_root_str = payload.get("archive_root")
    archive_root = Path(archive_root_str) if archive_root_str else None
    retrieval_store = _decode_pickle(payload["retrieval_store_pickle"])
    thesis_component_reader = _decode_pickle(payload["thesis_component_reader_pickle"])
    pre_processor_bundle = _decode_pickle(payload["pre_processor_bundle_pickle"])
    pm_view = _decode_pickle(payload["pm_view_pickle"])
    active_sectors = frozenset(payload["active_sectors"])
    halt_mode = bool(payload["halt_mode"])
    sector_resolver = _decode_pickle(payload["sector_resolver_pickle"])
    library_config = _decode_pickle(payload["library_config_pickle"])

    engine = make_engine()
    sync_session_factory = make_session_factory(engine)
    try:
        validation_state = _rehydrate_validation_state(
            _decode_pickle(payload["initial_validation_state_pickle"]),
            sync_session_factory=sync_session_factory,
        )
        submit_envelope_state = _decode_pickle(payload["initial_submit_envelope_state_pickle"])
        submit_envelope_state = dataclasses.replace(
            submit_envelope_state,
            validation_state=_rehydrate_validation_state(
                submit_envelope_state.validation_state,
                sync_session_factory=sync_session_factory,
            ),
        )
        library_market = _rehydrate_market_inputs(
            _decode_pickle(payload["library_market_pickle"]),
            sync_session_factory=sync_session_factory,
        )

        try:
            result = await invoke_pm(
                agent_config=agent_config,
                user_message=payload["user_message"],
                invocation_id=payload["invocation_id"],
                initial_validation_state=validation_state,
                initial_submit_envelope_state=submit_envelope_state,
                retrieval_store=retrieval_store,
                thesis_component_reader=thesis_component_reader,
                pre_processor_bundle=pre_processor_bundle,
                pm_view=pm_view,
                active_sectors=active_sectors,
                halt_mode=halt_mode,
                sector_resolver=sector_resolver,
                library_config=library_config,
                library_market=library_market,
                archive_root=archive_root,
                broker_dispatch=None,
                progress=progress,
                phase=payload.get("phase", "pm"),
            )
        except (MalformedOutputFailure, ContextOverflowFailure, TimeoutFailure, SDKFailure) as exc:
            return _failure_payload(exc)
        return _success_payload(result)
    finally:
        engine.dispose()


async def main() -> int:
    payload = json.loads(sys.stdin.read())
    agent = payload["agent"]
    if agent == "domain_researcher":
        result = await _run_domain_researcher(payload)
    elif agent == "qualitative_researcher":
        result = await _run_qualitative_researcher(payload)
    elif agent == "synthesizer":
        result = await _run_synthesizer(payload)
    elif agent == "adaptive_researcher":
        result = await _run_adaptive_researcher(payload)
    elif agent == "analyst":
        result = await _run_analyst(payload)
    elif agent == "strategist":
        result = await _run_strategist(payload)
    elif agent == "portfolio_manager":
        result = await _run_portfolio_manager(payload)
    else:
        result = {
            "kind": "failure",
            "error_type": "SDKFailure",
            "error_msg": f"Unsupported agent kind in worker: {agent!r}",
            "agent_name": payload.get("agent") or "unknown",
            "invocation_id": payload.get("invocation_id") or "unknown",
        }

    sys.stdout.write(json.dumps(result))
    sys.stdout.flush()
    return 0 if result["kind"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
