# 07 — Strategist runner

## Goal

Implement the public entry point that composes the strategist's full work — take typed value-object inputs (synthesizer brief text + StrategistView + retrieval store + risk-budget data), build the validation-tool initial state, build the input bundle, invoke the harness, and return the parsed strategist output alongside invocation metadata. Mirrors `alphamind.decision.analyst.runner.run_analyst` shape.

## Reading

* `src/alphamind/decision/analyst/runner.py` — sibling pattern (291 lines). Mirror its structure point-for-point with strategist substitutions (StrategistView in place of AnalystView, defensive_posture in place of watchlist).
* `src/alphamind/decision/strategist/harness.py` — `run_strategist_harness` and `HarnessSuccess` (story 06).
* `src/alphamind/decision/strategist/input_bundle.py` — `assemble_input_bundle_normal`, `assemble_input_bundle_defensive_posture` (story 04).
* `src/alphamind/decision/strategist/models.py` — `StrategistOutput` (story 03).
* `src/alphamind/portfolio_state/consumers/strategist.py` — `StrategistView`.
* `src/alphamind/risk_guardrails/state_delivery/__init__.py` — `build_initial_validation_state`.
* `src/alphamind/risk_guardrails/state_delivery/validation_tool.py` — `ValidationToolState` (typed I/O).
* `src/alphamind/risk_guardrails/breach_behavior/__init__.py` — `HaltState` (passed through to defensive_posture mode).
* `src/alphamind/risk_guardrails/regime_adaptation/__init__.py` — `RegimeTransitionBreach`.
* `src/alphamind/risk_guardrails/state_delivery/config.py` — `StateDeliveryConfig`.
* `src/alphamind/config/__init__.py` — `AgentsConfig` loader (the runner reads the strategist entry by `AgentName.strategist`).
* `tests/decision/analyst/test_runner.py` — sibling test pattern.
* Parent Issue <issue id="213b1ce9-8210-4b63-8d1b-7957cc9466f2">ALP-116</issue> § Pre-resolved decisions (D, G, I) — mode dispatch, sector_resolver plumbing, halt-mode trigger.

## Depends on

* <issue id="d3292de8-e3a4-4081-8b0b-bec65e235046">ALP-307</issue> (Story 06 — Agent SDK harness). Runner invokes `run_strategist_harness`.

## Scope

In scope: `src/alphamind/decision/strategist/runner.py`. Tests at `tests/decision/strategist/test_runner.py`.

### 1\. `StrategistResult` record

```python
class StrategistResult(BaseModel, frozen=True):
    output: StrategistOutput
    validation_result: ValidationResult
    tokens_used: TokensUsed
    metadata: dict[str, Any]
```

### 2\. `load_strategist_agent_config(...)`

Mirror analyst's `load_analyst_agent_config(agents_config_path: Path | None) -> BaseAgentConfig`. Loads `AgentsConfig` and resolves the strategist entry by `AgentName.strategist`. The strategist is non-tool-loop (single-turn JSON-Schema mode), so the loaded entry is a plain `BaseAgentConfig`.

### 3\. `run_strategist(...)` public entry point

```python
async def run_strategist(
    *,
    invocation_id: str,
    timestamp: datetime,
    mode: Literal["normal", "defensive_posture"],
    halt_state: HaltState | None,
    strategist_view: StrategistView,
    synthesizer_brief_text: str,
    retrieval_store: RetrievalStore,
    options_enabled: bool,
    short_selling_enabled: bool,
    active_sectors: tuple[str, ...],
    state_delivery_config: StateDeliveryConfig,
    sector_resolver: Callable[[str], str],
    total_portfolio_value_usd: float,
    available_for_new_positions_usd: float,
    current_price_lookup: Callable[[str], float],
    profile_feature_flags: FeatureFlagsView,
    library_config: LibraryConfig,
    library_market: MarketInputs,
    starting_snapshot: PortfolioStateSnapshot,
    archive_root: Path | None,
    agent_config: BaseAgentConfig | None = None,
    agents_config_path: Path | None = None,
    sector_label_display: dict[str, str] | None = None,
    regime_transition_breaches: tuple[RegimeTransitionBreach, ...] = (),
    sdk_query_fn: Callable[..., AsyncIterable[Any]] | None = None,
    borrow_cost_resolver: Callable[[str], float] | None = None,
) -> StrategistResult:
    """Invoke the strategist and return a StrategistResult."""
```

Composition:

1. **Resolve** `agent_config`. If `agent_config is None`, call `load_strategist_agent_config(agents_config_path)`.
2. **Read system prompt.** Resolve `agent_config.prompt` to an absolute path; read the prompt verbatim.
3. **Build initial validation state.** Call `build_initial_validation_state(invocation_id=invocation_id, starting_snapshot=starting_snapshot, starting_risk_budget=strategist_view.risk_budget, starting_active_risk_parameters=strategist_view.active_risk_parameters, profile_feature_flags=profile_feature_flags, library_config=library_config, library_market=library_market, sector_resolver=sector_resolver, borrow_cost_resolver=borrow_cost_resolver)`.
4. **Assemble input bundle.** Dispatch on `mode`:
   * `"normal"` → `assemble_input_bundle_normal(...)`.
   * `"defensive_posture"` → require `halt_state is not None`; call `assemble_input_bundle_defensive_posture(halt_state=halt_state, ...)`. If `halt_state is None`, raise `ValueError`.
5. **Invoke harness.** Call `run_strategist_harness(user_message=user_message, system_prompt=system_prompt, invocation_id=invocation_id, agent_config=agent_config, validation_state=initial_state, retrieval_store=retrieval_store, active_sectors=frozenset(active_sectors), archive_root=archive_root, sdk_query_fn=sdk_query_fn or default_sdk_query)`.
6. **Sanity-check output.** Confirm `harness_success.output.mode == mode` (the runner sets the mode authoritatively per parent decision (I); if the LLM emits a different mode, that's a parse/validate failure the harness should have caught — but defensive double-check).
7. **Return** `StrategistResult` with `output`, `validation_result`, `tokens_used`, `metadata`.

### 4\. Failure propagation

The runner does NOT catch `HarnessFailure` — it propagates to the caller (per fail-closed contract). The runner does NOT log; logging is the harness's job via the diagnostic archive.

### Out of scope

* Live SDK e2e verification + fixture emission — story 08.
* Production pipeline composition (synthesizer→strategist) — lives in the analysis-layer pipeline composition wiring To-do (<issue id="adfd5e93-3b9a-471b-aa2a-fbf85f68960a">ALP-276</issue>).

## Acceptance criteria

- [ ] `src/alphamind/decision/strategist/runner.py` exports `run_strategist`, `StrategistResult`, `load_strategist_agent_config`.
- [ ] Runner accepts `mode: Literal["normal", "defensive_posture"]` and dispatches to the correct input-bundle assembler.
- [ ] Runner raises `ValueError` if `mode == "defensive_posture"` and `halt_state is None`.
- [ ] Runner builds the initial `ValidationToolState` via `build_initial_validation_state` with all required fields.
- [ ] Runner reads the system prompt from the resolved `agent_config.prompt` path.
- [ ] Runner returns `StrategistResult` with output, validation_result, tokens_used, metadata.
- [ ] `HarnessFailure` subclasses propagate uncaught.
- [ ] `tests/decision/strategist/test_runner.py` covers: happy path normal mode (with stub `sdk_query_fn`); happy path defensive_posture mode; ValueError when defensive_posture without halt_state; agent_config injection (skip yaml load); HarnessFailure propagation.
- [ ] `uv run pytest tests/decision/strategist/test_runner.py -n auto` passes.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` clean.
- [ ] No magic numbers — all numeric values come from `agent_config` or its dependents.

## Verification

```bash
uv run pytest tests/decision/strategist/test_runner.py -n auto
```

Inspect runner shape for parity with `src/alphamind/decision/analyst/runner.py` — same composition pattern, same parameter ordering, same propagation discipline.
