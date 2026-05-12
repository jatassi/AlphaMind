# 06 — Adaptive-researcher runner

## Goal

Implement `run_adaptive_researcher` — the single function that turns an invocation context into a validated `AdaptiveBrief` plus invocation metadata. Composes the anomaly-stream loaders (story 03a), the input-bundle assembler (story 04b), the harness (story 05), and the tool registry (story 04a) into a clean call surface used by the higher-level pipeline orchestrator. Mirrors `alphamind.analysis.qualitative_research.runner` structurally — public entry point with production defaults wired, and a `_run_adaptive_researcher` private function with a `_Deps` DI bundle for testability.

Per the parent issue's "fail-closed propagation" invariant: any harness failure aborts the adaptive-researcher invocation; the runner does NOT catch and degrade.

## Reading

* `src/alphamind/analysis/qualitative_research/runner.py` — sibling runner to mirror end-to-end. Mirror: `_Deps` frozen dataclass, `_run_*` private DI'd function, public `run_*` function with production defaults, `*Result` return type, the `wall_clock_seconds` accumulation pattern, the `agents_config` registry-lookup discipline.
* `src/alphamind/analysis/adaptive_research/loaders.py` § `assemble_adaptive_anomaly_inputs` — input loader.
* `src/alphamind/analysis/adaptive_research/input_bundle.py` § `assemble_input_bundle` — bundle assembler.
* `src/alphamind/analysis/adaptive_research/harness.py` § `invoke_adaptive_researcher`, `HarnessSuccess` — LLM harness.
* `src/alphamind/analysis/adaptive_research/models.py` § `AdaptiveBrief` — output type.
* `src/alphamind/analysis/_shared.py` § `TokensUsed` — token-accounting record.
* `src/alphamind/config/models/agents.py` § `AgentName.adaptive_researcher`, `BaseAgentConfig` — config registry lookup pattern.

## Depends on

* <issue id="eca19eb3-3d7a-43c6-8ae3-72266ae37aee">ALP-256</issue> (story 03a) — anomaly-stream loaders.
* <issue id="75f81ea7-eecd-45ff-b564-054ea2a09a1a">ALP-262</issue> (story 04b) — input-bundle assembler.
* <issue id="63ac7ee0-1da6-46ee-9a94-ff8e857b1227">ALP-263</issue> (story 05) — tool-using LLM harness.

## Scope

In scope under `src/alphamind/analysis/adaptive_research/runner.py` and `tests/analysis/adaptive_research/test_runner.py`.

### 1\. Result type

```python
class AdaptiveResearcherResult(BaseModel, frozen=True):
    """Runner return type — carries the brief and all invocation metadata."""

    brief: AdaptiveBrief
    input_bundle: InputBundle
    anomaly_inputs: AdaptiveAnomalyInputs
    tokens_used: TokensUsed
    tool_calls_used: int
    wall_clock_seconds: float
    retry_count: int
```

### 2\. Dependency bundle

```python
@dataclass(frozen=True)
class _Deps:
    """Collects the three injectable callables so `_run_adaptive_researcher`
    stays under the linter's argument-count threshold."""

    anomaly_assembler: Callable[..., AdaptiveAnomalyInputs]
    bundle_assembler: Callable[..., InputBundle]
    harness_fn: Callable[..., Coroutine[Any, Any, HarnessSuccess]]
```

### 3\. Private runner

```python
async def _run_adaptive_researcher(  # noqa: PLR0913 — signature dictated by composition seam
    *,
    invocation_id: str,
    as_of: datetime,
    distillation_outputs: DistillationOutputs,
    sector_briefs: tuple[SectorBrief, ...],
    qualitative_brief: QualitativeBrief,
    correlation_regime_brief: CorrelationRegimeBrief,
    universal_regime_label: dict[str, Any],
    universe: frozenset[str],
    agents_config: Mapping[str, BaseAgentConfig],
    deps: _Deps,
    archive_root: Path | None = None,
) -> AdaptiveResearcherResult:
    """Run the adaptive researcher with injected dependencies.

    The testable core. `run_adaptive_researcher` delegates here with production
    callables wrapped in a `_Deps` bundle.

    Mirrors qualitative-research's runner: lookup agent_config, load inputs,
    assemble bundle, invoke harness, return result with composed metadata.

    Per the parent issue's fail-closed invariant: HarnessFailure subclasses
    propagate up unchanged. The runner does NOT catch and degrade.
    """
```

Implementation steps (mirror the qualitative runner step-by-step):

1. `wall_start = time.monotonic()`
2. `agent_name = AgentName.adaptive_researcher.value`; lookup `agent_config = agents_config[agent_name]`; raise `ValueError` on missing.
3. `anomaly_inputs = deps.anomaly_assembler(distillation_outputs=distillation_outputs, sector_briefs=sector_briefs, as_of=as_of)`
4. `bundle = deps.bundle_assembler(invocation_id=invocation_id, as_of=as_of, regime_label=universal_regime_label, anomaly_inputs=anomaly_inputs)`
5. `harness_result = await deps.harness_fn(agent_config=agent_config, user_message=bundle.bundle_text, invocation_id=invocation_id, universe=universe, sector_briefs=sector_briefs, qualitative_brief=qualitative_brief, correlation_regime_brief=correlation_regime_brief, archive_root=archive_root)`
6. `wall_elapsed = time.monotonic() - wall_start`
7. Construct and return `AdaptiveResearcherResult`.

Logging at INFO level for the three input-shape milestones (mirror qualitative runner's `logger.info` lines): "anomaly inputs assembled (%d distillation, %d sector)", "input bundle assembled (length=%d)", "harness invoked (retry_count=%d, tokens=%s, tool_calls=%d)".

### 4\. Public entry point

```python
async def run_adaptive_researcher(
    invocation_id: str,
    as_of: datetime,
    *,
    session: Session,
    distillation_outputs: DistillationOutputs,
    sector_briefs: tuple[SectorBrief, ...],
    qualitative_brief: QualitativeBrief,
    correlation_regime_brief: CorrelationRegimeBrief,
    universal_regime_label: dict[str, Any],
    universe: frozenset[str],
    agents_config: Mapping[str, BaseAgentConfig],
    archive_root: Path | None = None,
) -> AdaptiveResearcherResult:
    """Invoke the adaptive researcher and return a validated result.

    Wires production defaults: anomaly_assembler is assemble_adaptive_anomaly_inputs;
    bundle_assembler is assemble_input_bundle; harness_fn is invoke_adaptive_researcher
    bound to session and the upstream briefs.
    """
```

Build `_Deps` in-line, calling the production functions; delegate to `_run_adaptive_researcher`.

### 5\. Tests

`tests/analysis/adaptive_research/test_runner.py` covers:

* Happy path — stubbed `_Deps` returns a `HarnessSuccess`; runner produces an `AdaptiveResearcherResult` with composed metadata (`tokens_used`, `tool_calls_used`, `wall_clock_seconds > 0`, `retry_count`).
* Missing agent config — `agents_config` without an `adaptive_researcher` entry raises `ValueError("Agent name 'adaptive_researcher' is not in agents_config — config drift detected")`.
* Harness failure propagation — stubbed `harness_fn` raising `MalformedOutputFailure` propagates up unchanged; runner does NOT catch.
* `wall_clock_seconds` is non-negative and approximately matches the elapsed time (loose bound — within 10× a stubbed sub-second harness).
* Async correctness — `await run_adaptive_researcher(...)` returns the typed result; the test runs under pytest-asyncio's default mode.

### Out of scope

* End-to-end integration with live SDK + populated DB — story 07.
* Pipeline-level wiring (composing this runner with the upstream `run_external_distillation`, `run_qualitative_researcher`, and three `run_<sector>_researcher` calls) — that's the execution-layer composition story tracked under "Substantial" in `docs/project-tracker.md`.
* Editing the harness or any input-stage module — stories 05, 04b, 03a.

## Acceptance criteria

- [ ] `from alphamind.analysis.adaptive_research.runner import AdaptiveResearcherResult, run_adaptive_researcher` resolves cleanly.
- [ ] `run_adaptive_researcher` is `async def` and accepts the documented argument set.
- [ ] Happy-path call with a stubbed harness that returns a `HarnessSuccess` produces an `AdaptiveResearcherResult` whose `brief`, `input_bundle`, `anomaly_inputs`, `tokens_used`, `tool_calls_used`, `wall_clock_seconds`, and `retry_count` fields are all populated correctly.
- [ ] Missing `adaptive_researcher` key in `agents_config` raises `ValueError` with a message naming the missing agent.
- [ ] A stubbed harness that raises `MalformedOutputFailure` causes `run_adaptive_researcher` to propagate the exception (no catch, no degraded result).
- [ ] The production wiring of `run_adaptive_researcher` calls `assemble_adaptive_anomaly_inputs`, `assemble_input_bundle`, and `invoke_adaptive_researcher` (verifiable by inspecting source imports).
- [ ] `tests/analysis/adaptive_research/test_runner.py` covers each scenario above and passes under `uv run pytest tests/analysis/adaptive_research/test_runner.py -n auto`.
- [ ] `uv run ruff check . && uv run ruff format --check . && uv run mypy` clean.

## Verification

Run `uv run pytest tests/analysis/adaptive_research/test_runner.py -n auto`. Construct one fixture invocation with stubbed loaders + harness and assert the returned `AdaptiveResearcherResult.brief` is the brief the stubbed harness returned (no mutation by the runner). Inspect `git diff main..HEAD -- src/alphamind/analysis/adaptive_research/` and confirm only `runner.py` and its test file were touched by this story.