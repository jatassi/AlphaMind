# 10 — Synthesizer runner

## Goal

Implement the public entry point that composes the synthesizer's full work — take the upstream brief inputs (typed value objects from each upstream agent), build the retrieval store, build the synthesizer's input bundle, invoke the harness, and return the synthesis text alongside the retrieval store the decision-layer agents will consume.

## Reading

* `docs/architecture/llm-integration.md` § Orchestration pattern — the `run_synthesizer(...)` entry point this story implements.
* `docs/design/03-analysis-layer/synthesizer.md` § System prompt — the prompt file this runner loads via the harness.
* `docs/design/03-analysis-layer/synthesizer.md` § Inputs — the six brief sources this runner composes.
* `src/alphamind/config/models/agents.py` — `BaseAgentConfig`, `AgentName.synthesizer` the runner resolves from `agents.yaml`.
* `src/alphamind/analysis/qualitative_research/runner.py` — sibling runner pattern reference (config resolution, harness invocation, value-object return).
* `src/alphamind/portfolio_state/consumers/synthesizer.py` — `SynthesizerPortfolioStateReader` the runner accepts as injection.
* [ALP-200](https://linear.app/alphamind-jatassi/issue/ALP-200) (story 02) — `agents.yaml` entry verified.
* [ALP-201](https://linear.app/alphamind-jatassi/issue/ALP-201) (story 03) — `BriefBundle`, `BriefSource`, `ReferencePrefix`.
* [ALP-203](https://linear.app/alphamind-jatassi/issue/ALP-203) (story 05a) — `RetrievalStore`, `assemble_retrieval_store`, four adapters.
* [ALP-205](https://linear.app/alphamind-jatassi/issue/ALP-205) (story 06a) — `build_retrieve_brief_mcp_server`. The runner returns the `RetrievalStore` so downstream decision-layer runners can call this factory; the runner itself does not invoke `retrieve_brief`.
* [ALP-206](https://linear.app/alphamind-jatassi/issue/ALP-206) (story 06b) — `build_portfolio_state_mcp_server`. The runner does not call this directly — the harness (08) does — but the runner accepts the `SynthesizerPortfolioStateReader` parameter that the harness ultimately uses.
* [ALP-207](https://linear.app/alphamind-jatassi/issue/ALP-207) (story 07) — `assemble_input_bundle`.
* [ALP-208](https://linear.app/alphamind-jatassi/issue/ALP-208) (story 08) — `invoke_synthesizer`, `HarnessSuccess`.
* [ALP-209](https://linear.app/alphamind-jatassi/issue/ALP-209) (story 09) — system prompt verified; the runner's harness call loads it.

## Depends on

Stories 02, 03, 05a, 06a, 06b, 07, 08, 09. The runner is the integration point of the work tree.

## Scope

Module path is `src/alphamind/analysis/synthesizer/runner.py`. The module defines a `SynthesizerResult` value object and the public `run_synthesizer` async entry point.

#### SynthesizerResult value object

Frozen Pydantic model carrying six fields. `synthesis_text: str` is the LLM's prose output. `retrieval_store: RetrievalStore` is the populated store the decision-layer agents will query. `tokens_used: TokensUsed` and `tool_calls_used: int` come from the harness response. `wall_clock_seconds: float` and `stop_reason: str | None` also come from the harness response.

#### run_synthesizer entry point

Async signature with keyword-only parameters.

```
async def run_synthesizer(
    *,
    regime_label: str,
    sector_briefs: tuple[SectorBrief, ...],
    correlation_regime_brief: CorrelationRegimeBrief,
    qualitative_brief: QualitativeBrief,
    adaptive_brief: AdaptiveBrief | None,
    portfolio_reader: SynthesizerPortfolioStateReader,
    invocation_id: str,
    now_utc: datetime,
    archive_root: Path | None = None,
    sdk_query_fn: Callable | None = None,
) -> SynthesizerResult
```

The body proceeds as follows.

1. Resolve `BaseAgentConfig` for `AgentName.synthesizer` via the existing config loader.
2. Construct a `BriefBundle` per upstream input via the four adapters from story 05a. Sector briefs receive a freshness param — the runner supplies `now_utc` or a per-brief freshness if the upstream emits one.
3. Assemble the `RetrievalStore` via `assemble_retrieval_store(bundles)`.
4. Compose the input-bundle text via `assemble_input_bundle(regime_label=..., brief_bundles=..., portfolio_tool_names=("get_positions_summary", "get_active_theses_summary", "get_exposure_snapshot"), now_utc=...)`.
5. Invoke `invoke_synthesizer(agent_config=..., user_message=..., invocation_id=..., portfolio_reader=..., archive_root=..., sdk_query_fn=...)`.
6. Return `SynthesizerResult` with `synthesis_text=harness_result.response_text`, plus the `retrieval_store`, plus the harness's tokens/tool_calls/wall_clock/stop_reason fields.

The `adaptive_brief: AdaptiveBrief | None` parameter — adaptive research is the only optional input (some invocations have no anomaly to investigate). The four sector briefs and correlation/regime brief are mandatory.

#### Tests

Test module path is `tests/analysis/synthesizer/test_runner.py`. Test cases listed below.

* `test_runner_resolves_agent_config` — verify the runner reads the synthesizer entry from `agents.yaml`.
* `test_runner_builds_retrieval_store_from_inputs` — fixture upstream briefs; verify the store has expected ref-ID set.
* `test_runner_omits_optional_adaptive_brief` — `adaptive_brief=None` produces a store with no `AR-N` entries and an input bundle with no `AR` section.
* `test_runner_returns_result_on_happy_path` — stubbed harness returns text; runner returns populated `SynthesizerResult`.
* `test_runner_propagates_harness_failures` — `HarnessFailure` raised in the harness propagates unchanged (fail-closed).

## Out of scope

* The end-to-end live-SDK verification (story 11).
* Production wiring of `SnapshotBackedSynthesizerReader` (caller's responsibility — typically the analysis-layer pipeline composition step listed under `project-tracker.md` § Substantial § Analysis-layer pipeline composition wiring).
* Alternate model selection per cap-pressure response (deferred to operational tooling).

## Acceptance criteria

- [ ] `run_synthesizer(...)` returns a populated `SynthesizerResult` on happy path.
- [ ] Optional `adaptive_brief=None` is supported (other inputs mandatory).
- [ ] Harness failures propagate fail-closed.
- [ ] `agents.yaml` resolution uses the existing config loader (no parallel implementation).
- [ ] All four adapters are called with appropriate freshness inputs.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.