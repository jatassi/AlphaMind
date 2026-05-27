"""Subprocess-isolated SDK call wrapper.

The Claude Agent SDK exhibits process-level state leakage on Windows: the
first ``claude_agent_sdk.query()`` call in a Python process generally
succeeds, but subsequent calls in the same process can stall after
admission — emitting only a ``SystemMessage`` and never streaming
``AssistantMessage`` content. The stall surfaces as ``stop_reason: null``
with zero output tokens. Even the same-process stall-retry mechanism
sometimes fails (both attempts stall) because the process state itself
has degraded.

The workaround in this module: run each harness invocation in a fresh
Python subprocess. Each subprocess imports the harness module, runs one
``invoke_<agent>`` call, serializes the result to stdout, and exits.
The parent reads stdout, reconstructs the typed result, and returns it
— so the caller's interface is unchanged. Failures are reconstructed
from a typed error payload so the orchestrator's per-failure-class
handling continues to work.

Per-harness subprocess wrappers live next to this module: seven in
total (domain_researcher, qualitative_researcher, analyst, strategist,
portfolio_manager, synthesizer, adaptive_researcher). ALP-650 added
the latter five so every LLM harness's blast radius is bounded to a
subprocess on the same back-to-back-stall failure mode the first two
were added against.

Heavy state objects (``ValidationToolState``, ``MarketInputs`` with a
session-bound ``IvProvider``, ``SubmitEnvelopeState``) cross the
subprocess boundary via base64-encoded pickle inside the JSON
transport. A small set of pickle-time shims replace session-bound
providers with data-only carriers; the worker rehydrates them against
a fresh ``DATABASE_PATH`` session.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import dataclasses
import json
import logging
import os
import pickle
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from alphamind._kernel.progress import NOOP_PROGRESS_EMITTER, ProgressEmitter
from alphamind.analysis._harness_core import (
    ContextOverflowFailure,
    MalformedOutputFailure,
    SDKFailure,
    TimeoutFailure,
    _get_sdk_call_semaphore,
)
from alphamind.analysis._shared import Sector, TokensUsed
from alphamind.analysis.domain_researchers.harness import HarnessSuccess as DRHarnessSuccess
from alphamind.analysis.domain_researchers.models import SectorBrief
from alphamind.analysis.qualitative_research.harness import HarnessSuccess as QRHarnessSuccess
from alphamind.analysis.qualitative_research.models import QualitativeBrief
from alphamind.risk_guardrails.guardrail_evaluation import MarketInputs
from alphamind.risk_guardrails.guardrail_evaluation.iv_sourcing import (
    RealizedVolEntry,
    SqlOptionsIvProvider,
)

# Decision/analysis-layer harness HarnessSuccess types and the heavy state
# Pydantic models are referenced only as type annotations in this module.
# Importing them at module load time creates a circular import: each layer's
# package ``__init__`` re-exports its runner, the runners now import the
# subprocess wrappers from this module, and this module would re-enter the
# runner via the harness import. Deferring them to TYPE_CHECKING breaks the
# cycle without forcing every caller to know the heavyweight package
# structure.
if TYPE_CHECKING:
    from alphamind.analysis.adaptive_research.harness import HarnessSuccess as ARHarnessSuccess
    from alphamind.analysis.synthesizer.harness import HarnessSuccess as SynthHarnessSuccess
    from alphamind.analysis.synthesizer.retrieval import RetrievalStore
    from alphamind.config.models.agents import BaseAgentConfig
    from alphamind.decision.analyst.harness import HarnessSuccess as AnalystHarnessSuccess
    from alphamind.decision.portfolio_manager.harness import HarnessSuccess as PMHarnessSuccess
    from alphamind.decision.portfolio_manager.submit_envelope.types import SubmitEnvelopeState
    from alphamind.decision.strategist.harness import HarnessSuccess as StratHarnessSuccess
    from alphamind.portfolio_state.consumers.portfolio_manager import (
        PortfolioManagerThesisComponentReader,
        PortfolioManagerView,
    )
    from alphamind.portfolio_state.consumers.synthesizer import SynthesizerPortfolioStateReader
    from alphamind.risk_guardrails.state_delivery.validation_tool import ValidationToolState
    from alphamind.state.config import StatePersistenceConfig

__all__ = [
    "invoke_adaptive_researcher_in_subprocess",
    "invoke_analyst_in_subprocess",
    "invoke_domain_researcher_in_subprocess",
    "invoke_portfolio_manager_in_subprocess",
    "invoke_qualitative_researcher_in_subprocess",
    "invoke_strategist_in_subprocess",
    "invoke_synthesizer_in_subprocess",
]

logger = logging.getLogger(__name__)

# The worker module path used by ``-m`` when spawning. Lives next to this
# module so the subprocess can import all of alphamind's harness machinery
# the same way the parent does.
_WORKER_MODULE = "alphamind.analysis._sdk_subprocess_worker"


def _as_of_to_str(as_of: datetime | None) -> str | None:
    """Serialize an optional datetime to ISO 8601 UTC string for the payload."""
    if as_of is None:
        return None
    return as_of.astimezone(UTC).isoformat()


# ---------------------------------------------------------------------------
# Pickle transport for heavy state
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _SqlIvProviderShim:
    """Pickle-friendly stand-in for :class:`SqlOptionsIvProvider`.

    Carries only the ``realized_vol`` mapping (the fallback table); the
    worker reconstructs a real provider against its own fresh
    ``DATABASE_PATH`` ``sessionmaker``. The instance never reaches
    ``lookup_iv`` — it is swapped out during
    :func:`rehydrate_market_inputs` before the harness call.
    """

    realized_vol: Mapping[str, RealizedVolEntry]

    def lookup_iv(self, **_: Any) -> Any:  # pragma: no cover - never invoked
        msg = "_SqlIvProviderShim must be rehydrated before lookup_iv is called"
        raise RuntimeError(msg)


def _prepare_market_inputs_for_pickle(market_inputs: MarketInputs) -> MarketInputs:
    """Swap out a session-bound ``SqlOptionsIvProvider`` before pickling.

    Other implementations (the fixture-only :class:`FixtureIvProvider` used
    in tests and the bootstrap path) pickle as-is.
    """
    if isinstance(market_inputs.iv_provider, SqlOptionsIvProvider):
        shim = _SqlIvProviderShim(realized_vol=market_inputs.iv_provider.realized_vol)
        return dataclasses.replace(market_inputs, iv_provider=shim)
    return market_inputs


def _prepare_validation_state_for_pickle(state: ValidationToolState) -> ValidationToolState:
    """Swap out non-picklable inner fields before pickling ValidationToolState."""
    return state.model_copy(
        update={"library_market": _prepare_market_inputs_for_pickle(state.library_market)}
    )


def _prepare_submit_envelope_state_for_pickle(
    state: SubmitEnvelopeState,
) -> SubmitEnvelopeState:
    """Swap out the nested validation_state's non-picklable inner fields."""
    return dataclasses.replace(
        state,
        validation_state=_prepare_validation_state_for_pickle(state.validation_state),
    )


def _encode_pickle(obj: Any) -> str:
    """Encode a Python object as base64-pickle for JSON transport."""
    return base64.b64encode(pickle.dumps(obj)).decode("ascii")


def _decode_pickle(encoded: str) -> Any:
    """Decode a base64-pickle string produced by :func:`_encode_pickle`."""
    return pickle.loads(base64.b64decode(encoded.encode("ascii")))


def _extract_progress_jsonl_path(progress: ProgressEmitter) -> str | None:
    """Pull the JSONL file path off a ``JsonlProgressEmitter``.

    The worker re-instantiates a fresh emitter against this path so the
    ``agent_request`` / ``agent_response`` events land in the same
    archive ``progress.jsonl`` as if the call had run in-parent.

    Returns ``None`` for the no-op emitter (production / tests).
    """
    path = getattr(progress, "_path", None)
    if isinstance(path, Path):
        return str(path)
    return None


def _raise_failure(error_payload: dict[str, Any]) -> None:
    """Reconstruct and raise the typed harness failure on the parent side."""
    err_type = error_payload["error_type"]
    err_msg = error_payload["error_msg"]
    agent_name = error_payload.get("agent_name") or "unknown"
    invocation_id = error_payload.get("invocation_id") or "unknown"

    if err_type == "MalformedOutputFailure":
        raise MalformedOutputFailure(
            err_msg,
            agent_name=agent_name,
            invocation_id=invocation_id,
            raw_response_initial=error_payload.get("raw_response_initial"),
            raw_response_retry=error_payload.get("raw_response_retry"),
        )
    if err_type == "ContextOverflowFailure":
        raise ContextOverflowFailure(
            err_msg,
            agent_name=agent_name,
            invocation_id=invocation_id,
            raw_response=error_payload.get("raw_response"),
        )
    if err_type == "TimeoutFailure":
        raise TimeoutFailure(
            err_msg,
            agent_name=agent_name,
            invocation_id=invocation_id,
        )
    # SDKFailure or anything unrecognized — surface as SDKFailure so the
    # orchestrator's fail-closed path engages.
    raise SDKFailure(
        f"{err_type}: {err_msg}",
        agent_name=agent_name,
        invocation_id=invocation_id,
    )


async def _run_worker(payload: dict[str, Any]) -> dict[str, Any]:
    """Spawn the worker subprocess under the parent's SDK-call semaphore.

    The semaphore is acquired before spawning ``claude.exe`` and released
    after the subprocess exits. An upper cap exists so a parallel pipeline
    cannot spawn more ``claude.exe`` instances than the OAuth admit-rate
    and local CPU/memory can sustain. The cap is sized for the widest
    single-phase fan-out (analysis-layer: 3 sectors + qualitative = 4);
    see ``_harness_core._MAX_CONCURRENT_SDK_CALLS`` for the rationale and
    the ALP-702 history.

    Wall-clock guard: the worker is killed and an :class:`SDKFailure` is
    raised if the subprocess exceeds ``latency_budget_seconds`` (from the
    payload's ``agent_config``) plus a fixed slack window for spawn +
    teardown. Without this guard, an SDK stall inside the worker — the
    very failure mode the subprocess transport is meant to bound —
    leaves the parent blocked indefinitely inside ``proc.communicate``,
    holding the semaphore and wedging the whole pipeline.
    """
    timeout_seconds = _timeout_seconds_from_payload(payload)
    async with _get_sdk_call_semaphore():
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            _WORKER_MODULE,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ},  # propagate CLAUDE_CODE_OAUTH_TOKEN, DATABASE_PATH, etc.
        )
        return await _drive_worker(proc, payload, timeout_seconds=timeout_seconds)


# Slack on top of the harness's latency budget: covers Python interpreter
# spawn (~0.5-1s on cold start), the worker's import-time cost (alphamind
# is heavy), and process teardown. The harness's own timeout fires first
# in normal operation; this only engages when the worker is wedged so
# hard the harness's own clock can't trip it.
_WORKER_SPAWN_TEARDOWN_SLACK_SECONDS: float = 30.0


def _timeout_seconds_from_payload(payload: dict[str, Any]) -> float:
    """Pull ``latency_budget_seconds`` out of the payload's ``agent_config``."""
    agent_config = payload.get("agent_config", {})
    budget = agent_config.get("latency_budget_seconds") if isinstance(agent_config, dict) else None
    if not isinstance(budget, int | float) or budget <= 0:
        # Defensive default — if a payload is missing the field (shouldn't happen
        # in production, but keeps the guard from no-op'ing on a malformed call),
        # fall back to the longest production budget so the timeout never fires
        # spuriously.
        budget = 1200.0
    return float(budget) + _WORKER_SPAWN_TEARDOWN_SLACK_SECONDS


async def _drive_worker(
    proc: asyncio.subprocess.Process,
    payload: dict[str, Any],
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Stream the payload into *proc*, collect the result, raise on protocol violation.

    A wall-clock guard wraps ``proc.communicate``: if the worker exceeds
    ``timeout_seconds`` we kill it and surface an :class:`SDKFailure` so
    the orchestrator's fail-closed handler engages instead of the parent
    blocking forever on a wedged subprocess.
    """
    assert proc.stdin is not None
    assert proc.stdout is not None
    assert proc.stderr is not None
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(json.dumps(payload).encode("utf-8")),
            timeout=timeout_seconds,
        )
    except TimeoutError as exc:
        # Best-effort kill — the harness's own internal timeout should have
        # fired well before this; reaching this branch means the worker is
        # stuck so hard its own clock didn't trip it (the back-to-back-stall
        # failure mode this whole module exists to bound).
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        raise SDKFailure(
            f"SDK subprocess exceeded {timeout_seconds:.1f}s wall-clock budget; killed.",
            agent_name=payload.get("agent", "unknown"),
            invocation_id=payload.get("invocation_id", "unknown"),
        ) from exc
    returncode = proc.returncode

    stderr_text = stderr_bytes.decode("utf-8", errors="replace")
    if stderr_text:
        # Forward worker stderr to the parent's logger so a crash leaves
        # a trail — the worker's stderr is normally the only signal of an
        # import-time crash or unhandled exception.
        logger.warning("[sdk-subprocess stderr] %s", stderr_text.rstrip())

    if returncode != 0 and not stdout_bytes.strip():
        raise SDKFailure(
            f"SDK subprocess exited {returncode} with no result payload. "
            f"stderr: {stderr_text[:1000]}",
            agent_name=payload.get("agent", "unknown"),
            invocation_id=payload.get("invocation_id", "unknown"),
        )

    # The worker writes a single JSON line as the LAST stdout line; earlier
    # lines (if any) are noise. Read from the end.
    output_lines = [line for line in stdout_bytes.decode("utf-8").splitlines() if line.strip()]
    if not output_lines:
        raise SDKFailure(
            f"SDK subprocess produced no stdout. stderr: {stderr_text[:1000]}",
            agent_name=payload.get("agent", "unknown"),
            invocation_id=payload.get("invocation_id", "unknown"),
        )

    try:
        parsed: dict[str, Any] = json.loads(output_lines[-1])
    except json.JSONDecodeError as exc:
        raise SDKFailure(
            f"SDK subprocess emitted malformed JSON: {exc}. Last line: {output_lines[-1][:500]!r}",
            agent_name=payload.get("agent", "unknown"),
            invocation_id=payload.get("invocation_id", "unknown"),
        ) from exc
    return parsed


async def invoke_domain_researcher_in_subprocess(  # noqa: PLR0913 — signature parity with ``invoke_domain_researcher``
    *,
    agent_config: BaseAgentConfig,
    sector: Sector,
    user_message: str,
    invocation_id: str,
    as_of: datetime | None = None,
    archive_root: Path | None = None,
    sdk_query_fn: Any = None,  # noqa: ARG001 — signature parity; subprocess uses its own SDK call
    progress: ProgressEmitter = NOOP_PROGRESS_EMITTER,
    phase: str = "domain_researchers",
) -> DRHarnessSuccess:
    """Drop-in subprocess-isolated replacement for ``invoke_domain_researcher``.

    The SDK call runs in a fresh Python subprocess (see module docstring
    for why); the parent reconstructs the typed result on completion.
    """
    payload = {
        "agent": "domain_researcher",
        "agent_config": agent_config.model_dump(mode="json"),
        "sector": sector.value,
        "user_message": user_message,
        "invocation_id": invocation_id,
        "as_of": _as_of_to_str(as_of),
        "archive_root": str(archive_root) if archive_root is not None else None,
        "progress_jsonl_path": _extract_progress_jsonl_path(progress),
        "phase": phase,
    }
    result = await _run_worker(payload)
    if result["kind"] == "failure":
        _raise_failure(result)

    return DRHarnessSuccess(
        brief=SectorBrief.model_validate(result["brief"]),
        raw_response=result["raw_response"],
        retry_count=result["retry_count"],
        tokens_used=TokensUsed.model_validate(result["tokens_used"]),
        wall_clock_seconds=result["wall_clock_seconds"],
    )


async def invoke_qualitative_researcher_in_subprocess(  # noqa: PLR0913 — signature parity with ``invoke_qualitative_researcher``
    *,
    agent_config: BaseAgentConfig,
    user_message: str,
    invocation_id: str,
    universe: frozenset[str],
    as_of: datetime | None = None,
    archive_root: Path | None = None,
    sdk_query_fn: Any = None,  # noqa: ARG001 — signature parity; subprocess uses its own SDK call
    progress: ProgressEmitter = NOOP_PROGRESS_EMITTER,
    phase: str = "qualitative",
) -> QRHarnessSuccess:
    """Drop-in subprocess-isolated replacement for ``invoke_qualitative_researcher``.

    Note: the in-parent ``invoke_qualitative_researcher`` takes a
    ``session`` argument so it can wire the analysis MCP server's
    ``news_search`` / ``prediction_markets`` / ``earnings_commentary``
    tools against the live DB connection. The subprocess opens its own
    session from ``DATABASE_PATH`` and rebuilds the MCP server there
    — the in-parent ``session`` argument is intentionally not exposed
    here because closures over an open session can't cross the
    subprocess boundary.
    """
    payload = {
        "agent": "qualitative_researcher",
        "agent_config": agent_config.model_dump(mode="json"),
        "user_message": user_message,
        "invocation_id": invocation_id,
        "universe": sorted(universe),
        "as_of": _as_of_to_str(as_of),
        "archive_root": str(archive_root) if archive_root is not None else None,
        "progress_jsonl_path": _extract_progress_jsonl_path(progress),
        "phase": phase,
    }
    result = await _run_worker(payload)
    if result["kind"] == "failure":
        _raise_failure(result)

    return QRHarnessSuccess(
        brief=QualitativeBrief.model_validate(result["brief"]),
        raw_response=result["raw_response"],
        retry_count=result["retry_count"],
        tokens_used=TokensUsed.model_validate(result["tokens_used"]),
        tool_calls_used=result["tool_calls_used"],
        wall_clock_seconds=result["wall_clock_seconds"],
    )


# ---------------------------------------------------------------------------
# Synthesizer wrapper — added per ALP-650
# ---------------------------------------------------------------------------


async def invoke_synthesizer_in_subprocess(  # noqa: PLR0913 — signature parity with ``invoke_synthesizer``
    *,
    agent_config: BaseAgentConfig,
    user_message: str,
    invocation_id: str,
    portfolio_reader: SynthesizerPortfolioStateReader,
    as_of: datetime | None = None,
    archive_root: Path | None = None,
    sdk_query_fn: Any = None,
    progress: ProgressEmitter = NOOP_PROGRESS_EMITTER,
    phase: str = "synthesizer",
) -> SynthHarnessSuccess:
    """Drop-in subprocess-isolated replacement for ``invoke_synthesizer``.

    The ``portfolio_reader`` is pickled across the boundary; the production
    implementation (``SnapshotBackedSynthesizerReader``) carries only a
    projected :class:`SynthesizerView` (data-only frozen dataclass) so it
    pickles cleanly without further preprocessing.

    When ``sdk_query_fn`` is supplied, the wrapper bypasses the subprocess
    transport entirely and routes to the in-process harness. This restores
    the SDK-substitution test seam — runner tests that inject a stub SDK
    (and that often build inputs containing local-function closures the
    pickle transport cannot carry) continue to work unchanged.
    """
    if sdk_query_fn is not None:
        from alphamind.analysis.synthesizer.harness import invoke_synthesizer

        return await invoke_synthesizer(
            agent_config=agent_config,
            user_message=user_message,
            invocation_id=invocation_id,
            portfolio_reader=portfolio_reader,
            as_of=as_of,
            archive_root=archive_root,
            sdk_query_fn=sdk_query_fn,
            progress=progress,
            phase=phase,
        )

    payload = {
        "agent": "synthesizer",
        "agent_config": agent_config.model_dump(mode="json"),
        "user_message": user_message,
        "invocation_id": invocation_id,
        "portfolio_reader_pickle": _encode_pickle(portfolio_reader),
        "as_of": _as_of_to_str(as_of),
        "archive_root": str(archive_root) if archive_root is not None else None,
        "progress_jsonl_path": _extract_progress_jsonl_path(progress),
        "phase": phase,
    }
    result = await _run_worker(payload)
    if result["kind"] == "failure":
        _raise_failure(result)
    success: SynthHarnessSuccess = _decode_pickle(result["success_pickle"])
    return success


# ---------------------------------------------------------------------------
# Adaptive researcher wrapper — added per ALP-650
# ---------------------------------------------------------------------------


async def invoke_adaptive_researcher_in_subprocess(  # noqa: PLR0913 — signature parity with ``invoke_adaptive_researcher``
    *,
    agent_config: BaseAgentConfig,
    user_message: str,
    invocation_id: str,
    universe: frozenset[str],
    sector_briefs: tuple[SectorBrief, ...],
    qualitative_brief: QualitativeBrief,
    correlation_regime_brief: Any,  # CorrelationRegimeBrief — typed Pydantic at the worker boundary
    as_of: datetime | None = None,
    archive_root: Path | None = None,
    sdk_query_fn: Any = None,  # noqa: ARG001 — signature parity; subprocess uses its own SDK call
    progress: ProgressEmitter = NOOP_PROGRESS_EMITTER,
    phase: str = "adaptive",
) -> ARHarnessSuccess:
    """Drop-in subprocess-isolated replacement for ``invoke_adaptive_researcher``.

    The worker opens its own ``DATABASE_PATH`` session (mirroring the
    qualitative-researcher wrapper) so the harness's MCP-server wiring
    works against the same DB the parent uses.
    """
    payload = {
        "agent": "adaptive_researcher",
        "agent_config": agent_config.model_dump(mode="json"),
        "user_message": user_message,
        "invocation_id": invocation_id,
        "universe": sorted(universe),
        "sector_briefs_pickle": _encode_pickle(sector_briefs),
        "qualitative_brief_pickle": _encode_pickle(qualitative_brief),
        "correlation_regime_brief_pickle": _encode_pickle(correlation_regime_brief),
        "as_of": _as_of_to_str(as_of),
        "archive_root": str(archive_root) if archive_root is not None else None,
        "progress_jsonl_path": _extract_progress_jsonl_path(progress),
        "phase": phase,
    }
    result = await _run_worker(payload)
    if result["kind"] == "failure":
        _raise_failure(result)
    success: ARHarnessSuccess = _decode_pickle(result["success_pickle"])
    return success


# ---------------------------------------------------------------------------
# Analyst wrapper — added per ALP-650
# ---------------------------------------------------------------------------


async def invoke_analyst_in_subprocess(  # noqa: PLR0913 — signature parity with ``invoke_analyst``
    *,
    agent_config: BaseAgentConfig,
    user_message: str,
    invocation_id: str,
    initial_validation_state: ValidationToolState,
    retrieval_store: RetrievalStore,
    active_sectors: frozenset[str],
    as_of: datetime | None = None,
    archive_root: Path | None = None,
    sdk_query_fn: Any = None,
    progress: ProgressEmitter = NOOP_PROGRESS_EMITTER,
    phase: str = "analyst",
) -> AnalystHarnessSuccess:
    """Drop-in subprocess-isolated replacement for ``invoke_analyst``.

    ``initial_validation_state.library_market.iv_provider`` is replaced
    with :class:`_SqlIvProviderShim` before pickling when it is the
    session-bound ``SqlOptionsIvProvider``; the worker rebuilds a fresh
    provider against its own ``DATABASE_PATH`` session.

    When ``sdk_query_fn`` is supplied, the wrapper bypasses the subprocess
    transport entirely and routes to the in-process harness. This restores
    the SDK-substitution test seam — runner tests that inject a stub SDK
    (and that often build state containing local-function resolvers the
    pickle transport cannot carry) continue to work unchanged.
    """
    if sdk_query_fn is not None:
        from alphamind.decision.analyst.harness import invoke_analyst

        return await invoke_analyst(
            agent_config=agent_config,
            user_message=user_message,
            invocation_id=invocation_id,
            initial_validation_state=initial_validation_state,
            retrieval_store=retrieval_store,
            active_sectors=active_sectors,
            as_of=as_of,
            archive_root=archive_root,
            sdk_query_fn=sdk_query_fn,
            progress=progress,
            phase=phase,
        )

    payload = {
        "agent": "analyst",
        "agent_config": agent_config.model_dump(mode="json"),
        "user_message": user_message,
        "invocation_id": invocation_id,
        "initial_validation_state_pickle": _encode_pickle(
            _prepare_validation_state_for_pickle(initial_validation_state)
        ),
        "retrieval_store_pickle": _encode_pickle(retrieval_store),
        "active_sectors": sorted(active_sectors),
        "as_of": _as_of_to_str(as_of),
        "archive_root": str(archive_root) if archive_root is not None else None,
        "progress_jsonl_path": _extract_progress_jsonl_path(progress),
        "phase": phase,
    }
    result = await _run_worker(payload)
    if result["kind"] == "failure":
        _raise_failure(result)
    success: AnalystHarnessSuccess = _decode_pickle(result["success_pickle"])
    return success


# ---------------------------------------------------------------------------
# Strategist wrapper — added per ALP-650
# ---------------------------------------------------------------------------


async def invoke_strategist_in_subprocess(  # noqa: PLR0913 — signature parity with ``run_strategist_harness``
    *,
    user_message: str,
    system_prompt: str,
    invocation_id: str,
    agent_config: BaseAgentConfig,
    validation_state: ValidationToolState,
    retrieval_store: RetrievalStore,
    active_sectors: frozenset[str],
    as_of: datetime | None = None,
    archive_root: Path | None = None,
    sdk_query_fn: Any = None,
    progress: ProgressEmitter = NOOP_PROGRESS_EMITTER,
    phase: str = "strategist",
) -> StratHarnessSuccess:
    """Drop-in subprocess-isolated replacement for ``run_strategist_harness``.

    Same ``ValidationToolState`` pickle shim treatment as the analyst
    wrapper. When ``sdk_query_fn`` is supplied, the wrapper bypasses the
    subprocess transport and routes to the in-process harness so the
    test seam is preserved.
    """
    if sdk_query_fn is not None:
        from alphamind.decision.strategist.harness import run_strategist_harness

        return await run_strategist_harness(
            user_message=user_message,
            system_prompt=system_prompt,
            invocation_id=invocation_id,
            agent_config=agent_config,
            validation_state=validation_state,
            retrieval_store=retrieval_store,
            active_sectors=active_sectors,
            as_of=as_of,
            archive_root=archive_root,
            sdk_query_fn=sdk_query_fn,
            progress=progress,
            phase=phase,
        )

    payload = {
        "agent": "strategist",
        "agent_config": agent_config.model_dump(mode="json"),
        "user_message": user_message,
        "system_prompt": system_prompt,
        "invocation_id": invocation_id,
        "validation_state_pickle": _encode_pickle(
            _prepare_validation_state_for_pickle(validation_state)
        ),
        "retrieval_store_pickle": _encode_pickle(retrieval_store),
        "active_sectors": sorted(active_sectors),
        "as_of": _as_of_to_str(as_of),
        "archive_root": str(archive_root) if archive_root is not None else None,
        "progress_jsonl_path": _extract_progress_jsonl_path(progress),
        "phase": phase,
    }
    result = await _run_worker(payload)
    if result["kind"] == "failure":
        _raise_failure(result)
    success: StratHarnessSuccess = _decode_pickle(result["success_pickle"])
    return success


# ---------------------------------------------------------------------------
# Portfolio manager wrapper — added per ALP-650
# ---------------------------------------------------------------------------


async def invoke_portfolio_manager_in_subprocess(  # noqa: PLR0913 — signature parity with ``invoke_pm``
    *,
    agent_config: BaseAgentConfig,
    user_message: str,
    invocation_id: str,
    initial_validation_state: ValidationToolState,
    initial_submit_envelope_state: SubmitEnvelopeState,
    retrieval_store: RetrievalStore,
    thesis_component_reader: PortfolioManagerThesisComponentReader,
    pre_processor_bundle: Any,  # ProposalPreProcessorBundle — Pydantic, picklable
    pm_view: PortfolioManagerView,
    active_sectors: frozenset[str],
    halt_mode: bool,
    sector_resolver: Any,  # SectorResolver instance (callable class) — picklable
    library_config: Any,  # LibraryConfig — frozen dataclass, picklable
    library_market: MarketInputs,
    state_persistence_config: StatePersistenceConfig,
    as_of: datetime | None = None,
    archive_root: Path | None = None,
    sdk_query_fn: Any = None,
    broker_dispatch: Any = None,
    venue_config: Any = None,  # VenueConfig — Pydantic, picklable
    execution_mode: Any = None,  # ExecutionMode — StrEnum, picklable
    execution_config: Any = None,  # ExecutionConfig — Pydantic, picklable
    progress: ProgressEmitter = NOOP_PROGRESS_EMITTER,
    phase: str = "pm",
) -> PMHarnessSuccess:
    """Drop-in subprocess-isolated replacement for ``invoke_pm``.

    ALP-711 — production broker routing is wired by passing the picklable
    triple ``(venue_config, execution_mode, execution_config)`` across the
    subprocess boundary. The live ``TradingClient`` is not picklable, so
    the worker reconstructs it from the triple on its own side via
    :func:`alphamind.decision.portfolio_manager.submit_envelope.server.build_broker_routing_kwargs`.
    Production callers (the scheduler orchestrator) pass the triple;
    debug-e2e / non-prod callers leave all three at ``None`` so the
    submit_envelope wrapper's broker-routing gate stays False and the
    log-only path persists (synthetic ``alp-{order_id}`` placeholders).

    A non-``None`` ``broker_dispatch`` is still rejected when routed
    through the subprocess: a live callable cannot pickle. Tests that
    need to inject a fake ``broker_dispatch`` must supply ``sdk_query_fn``
    so the in-process branch below takes them — the in-process harness
    accepts ``broker_dispatch`` directly.

    When ``sdk_query_fn`` is supplied, the wrapper bypasses the subprocess
    transport and routes to the in-process harness so the SDK-substitution
    test seam is preserved.
    """
    if broker_dispatch is not None and sdk_query_fn is None:
        msg = (
            "invoke_portfolio_manager_in_subprocess does not support a non-None "
            "broker_dispatch across the subprocess boundary; supply sdk_query_fn "
            "to take the in-process branch (which forwards broker_dispatch to "
            "invoke_pm directly), or pass the (venue_config, execution_mode, "
            "execution_config) triple so the worker reconstructs the dispatch "
            "internally."
        )
        raise NotImplementedError(msg)

    if sdk_query_fn is not None:
        from alphamind.decision.portfolio_manager.harness import invoke_pm

        return await invoke_pm(
            agent_config=agent_config,
            user_message=user_message,
            invocation_id=invocation_id,
            initial_validation_state=initial_validation_state,
            initial_submit_envelope_state=initial_submit_envelope_state,
            retrieval_store=retrieval_store,
            thesis_component_reader=thesis_component_reader,
            pre_processor_bundle=pre_processor_bundle,
            pm_view=pm_view,
            active_sectors=active_sectors,
            halt_mode=halt_mode,
            sector_resolver=sector_resolver,
            library_config=library_config,
            library_market=library_market,
            state_persistence_config=state_persistence_config,
            as_of=as_of,
            archive_root=archive_root,
            sdk_query_fn=sdk_query_fn,
            broker_dispatch=broker_dispatch,
            venue_config=venue_config,
            execution_mode=execution_mode,
            execution_config=execution_config,
            progress=progress,
            phase=phase,
        )

    payload = {
        "agent": "portfolio_manager",
        "agent_config": agent_config.model_dump(mode="json"),
        "user_message": user_message,
        "invocation_id": invocation_id,
        "initial_validation_state_pickle": _encode_pickle(
            _prepare_validation_state_for_pickle(initial_validation_state)
        ),
        "initial_submit_envelope_state_pickle": _encode_pickle(
            _prepare_submit_envelope_state_for_pickle(initial_submit_envelope_state)
        ),
        "retrieval_store_pickle": _encode_pickle(retrieval_store),
        "thesis_component_reader_pickle": _encode_pickle(thesis_component_reader),
        "pre_processor_bundle_pickle": _encode_pickle(pre_processor_bundle),
        "pm_view_pickle": _encode_pickle(pm_view),
        "active_sectors": sorted(active_sectors),
        "halt_mode": halt_mode,
        "sector_resolver_pickle": _encode_pickle(sector_resolver),
        "library_config_pickle": _encode_pickle(library_config),
        "library_market_pickle": _encode_pickle(_prepare_market_inputs_for_pickle(library_market)),
        "state_persistence_config": state_persistence_config.model_dump(mode="json"),
        "as_of": _as_of_to_str(as_of),
        "archive_root": str(archive_root) if archive_root is not None else None,
        "progress_jsonl_path": _extract_progress_jsonl_path(progress),
        "phase": phase,
        # ALP-711 — picklable broker-routing inputs the worker uses to
        # reconstruct the live ``TradingClient`` on its own side. All three
        # round-trip via Pydantic JSON; ``None`` legs mean "no broker
        # routing" (debug-e2e / log-only path).
        "venue_config": (
            venue_config.model_dump(mode="json") if venue_config is not None else None
        ),
        "execution_mode": execution_mode.value if execution_mode is not None else None,
        "execution_config": (
            execution_config.model_dump(mode="json") if execution_config is not None else None
        ),
    }
    result = await _run_worker(payload)
    if result["kind"] == "failure":
        _raise_failure(result)
    success: PMHarnessSuccess = _decode_pickle(result["success_pickle"])
    return success
