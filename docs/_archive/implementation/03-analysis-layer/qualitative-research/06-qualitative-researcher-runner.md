# 06 — Qualitative researcher runner

## Goal

Implement `run_qualitative_researcher` — the single function that turns an invocation context into a validated `QualitativeBrief` plus invocation metadata. Composes the input loaders (story 03d), the news-digest renderer (story 04a), the input bundle assembler (story 05), the harness (story 04b), and the tool registry (story 03e) into a clean call surface used by the higher-level pipeline orchestrator. Async because the harness is async. Dependencies are injected via a private `_run_qualitative_researcher` core; the public function wires production defaults.

## Reading

* `docs/architecture/llm-integration.md` § Pipeline execution flow — the invocation pattern this runner participates in. The runner is invoked once per pipeline invocation, alongside the parallel domain-researcher orchestrator.
* `src/alphamind/analysis/domain_researchers/runner.py` — the canonical analog. Mirror the `_Deps` injection pattern, the `_run_*` private core + `run_*` public-defaults split, the `DomainResearcherResult`-shaped return type, the inline structured logging, and the fail-closed semantics (no try/except around the harness).
* `src/alphamind/analysis/qualitative_research/{loaders,news_digest,input_bundle,harness}.py` — the modules this runner composes.
* `src/alphamind/analysis/tools/__init__.py` — the tool registry the harness consumes.
* `src/alphamind/distillation/orchestrator.py` § `DistillationOutputs.universal_regime_label` — the regime payload the runner reads from a prior pipeline stage and forwards to the bundle assembler.
* `src/alphamind/distillation/sector_assembly.py` § `load_sector_roster` — the runner loads this once and forwards to the digest renderer.

## Depends on

* ALP-249 — the harness the runner invokes.
* ALP-251 — the bundle assembler the runner calls.
* ALP-247 — the tool registry the runner forwards into the harness.

(Indirectly depends on stories 03d, 04a via the bundle assembler — that transitive dependency is captured in 05's `blockedBy`.)

## Scope

In scope under `src/alphamind/analysis/qualitative_research/runner.py` and `tests/analysis/qualitative_research/test_runner.py`.

### 1\. Result type

```python
class QualitativeResearcherResult(BaseModel, frozen=True):
    brief: QualitativeBrief
    input_bundle: InputBundle
    news_digest: NewsDigest
    tokens_used: TokensUsed
    tool_calls_used: int
    wall_clock_seconds: float
    retry_count: int
```

### 2\. Dependency bundle

```python
@dataclass(frozen=True)
class _Deps:
    inputs_loader: Callable[..., QualitativeInputs]
    digest_renderer: Callable[..., NewsDigest]
    bundle_assembler: Callable[..., InputBundle]
    harness_fn: Callable[..., Coroutine[Any, Any, HarnessSuccess]]
```

### 3\. Private core

```python
async def _run_qualitative_researcher(
    *,
    invocation_id: str,
    as_of: datetime,
    last_invocation_time: datetime,
    universal_regime_label: dict[str, Any],
    sector_roster: Mapping[Sector, frozenset[str]],
    universe: frozenset[str],
    agents_config: Mapping[str, BaseAgentConfig],
    deps: _Deps,
    archive_root: Path | None = None,
) -> QualitativeResearcherResult: ...
```

Behavior:

1. Look up the agent config: `agent_config = agents_config[AgentName.qualitative_researcher.value]`. Raise `ValueError` if missing — config drift detected.
2. Call `deps.inputs_loader(as_of=as_of)` to obtain `QualitativeInputs`.
3. Call `deps.digest_renderer(invocation_id=invocation_id, as_of=as_of, last_invocation_time=last_invocation_time, sector_roster=sector_roster)` to obtain `NewsDigest`.
4. Call `deps.bundle_assembler(invocation_id=invocation_id, as_of=as_of, regime_label=universal_regime_label, digest=news_digest, inputs=inputs)` to obtain `InputBundle`.
5. Call `deps.harness_fn(agent_config=agent_config, user_message=bundle.bundle_text, invocation_id=invocation_id, universe=universe, archive_root=archive_root)` to obtain `HarnessSuccess`.
6. Assemble `QualitativeResearcherResult` from the harness result + bundle + digest + computed wall-clock.

`HarnessFailure` propagates up unchanged — the runner does not catch and degrade. The pipeline-level orchestrator handles fail-closed semantics.

Inline `logger.info` lines after each step name what just landed (`"qualitative inputs loaded (%d sentiment, %d events)"`, `"news digest rendered (%d entries, %d collected)"`, `"input bundle assembled (length=%d)"`, `"harness invoked (retry_count=%d, tokens=%s, tool_calls=%d)"`).

### 4\. Public entry point

```python
async def run_qualitative_researcher(
    invocation_id: str,
    as_of: datetime,
    last_invocation_time: datetime,
    *,
    session: Session,
    universal_regime_label: dict[str, Any],
    universe: frozenset[str],
    agents_config: Mapping[str, BaseAgentConfig],
    archive_root: Path | None = None,
) -> QualitativeResearcherResult: ...
```

Wires production defaults: `inputs_loader` is `load_qualitative_inputs` bound to `session`, `digest_renderer` is `render_news_digest` bound to `session`, `bundle_assembler` is `assemble_input_bundle`, `harness_fn` is `invoke_qualitative_researcher` bound to `session` and `universe`. Loads `sector_roster` from the distillation module once. Delegates to `_run_qualitative_researcher`.

### 5\. Tests

Tests at `tests/analysis/qualitative_research/test_runner.py`:

* Happy path — inject stubs for all four `_Deps` callables. Assert the runner returns a `QualitativeResearcherResult` whose `brief`, `input_bundle`, and `news_digest` are the stub's outputs. Wall-clock is positive.
* Harness failure propagates — stub `harness_fn` raising `MalformedOutputFailure`. Assert the runner re-raises the same exception unchanged (no try/except swallow).
* Config drift — `agents_config` missing the `qualitative_researcher` key. Assert `ValueError` with a message naming the missing key.
* Inputs loader returns empty inputs (empty universe, no events, no theses) — runner still completes (the bundle assembler handles empty gracefully).

No test invokes the real harness or the real Anthropic SDK.

### Out of scope

* Pipeline-level orchestration with the parallel domain-researcher orchestrator — that is the next composition layer (out of scope for this work tree's parent issue).
* Failure aggregation across multiple agents — fail-closed at the runner level; pipeline orchestrator handles cross-agent.
* Caching the rendered digest across invocations — every invocation renders fresh.

## Acceptance criteria

- [ ] `from alphamind.analysis.qualitative_research.runner import run_qualitative_researcher, QualitativeResearcherResult` resolves.
- [ ] Happy-path test returns a `QualitativeResearcherResult` carrying the stub's `QualitativeBrief`, `InputBundle`, and `NewsDigest`.
- [ ] Harness failure (`MalformedOutputFailure`, `ContextOverflowFailure`, `SDKFailure`, `TimeoutFailure`) propagates unchanged.
- [ ] Missing `qualitative_researcher` key in `agents_config` raises `ValueError`.
- [ ] `wall_clock_seconds` is non-negative and roughly equals real elapsed time in the happy-path stub.
- [ ] No try/except wrapping the harness call (per fail-closed propagation).
- [ ] All tests pass under `uv run pytest tests/analysis/qualitative_research/test_runner.py -n auto`.
- [ ] `uv run mypy src/alphamind/analysis/qualitative_research/runner.py` is clean.

## Verification

Run `uv run pytest tests/analysis/qualitative_research/test_runner.py -n auto`. Run `uv run ruff check . && uv run mypy` clean. Inspect the runner module to confirm it has no try/except around `deps.harness_fn(...)` and that `agents_config[...]` raises `KeyError` (or the explicit `ValueError`) on missing config rather than being defaulted.
