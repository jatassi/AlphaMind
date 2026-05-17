# 08 — Analyst runner

## Goal

Implement the public entry point that composes the analyst's full work — take typed value-object inputs (synthesizer result + portfolio state + risk-budget data), build the validation-tool initial state, build the input bundle, invoke the harness, and return the parsed analyst output alongside invocation metadata. Mirrors `alphamind.analysis.synthesizer.runner.run_synthesizer` shape.

## Reading

* `src/alphamind/analysis/synthesizer/runner.py` — direct sibling pattern.
* `src/alphamind/analysis/qualitative_research/runner.py` — second sibling; closer in scope (typed-output return).
* `src/alphamind/decision/analyst/{models, harness, input_bundle}.py` — the building blocks.
* `src/alphamind/risk_guardrails/state_delivery/validation_tool_mcp.py` — `build_initial_validation_state`.
* `docs/architecture/llm-integration.md` § Orchestration pattern — runner-vs-harness split.
* `docs/design/cost-and-rate-limit-modeling.md` § Per-trigger budget overrides — the runner accepts a per-trigger-overridden `BaseAgentConfig` so trigger-specific yaml budgets propagate.

## Depends on

* `07 — Agent SDK harness` (ALP-298, this work tree).

## Scope

In scope: `src/alphamind/decision/analyst/runner.py`. Tests at `tests/decision/analyst/test_runner.py`.

### 1\. AnalystResult record

```python
class AnalystResult(BaseModel, frozen=True):
    output: AnalystOutput
    retry_count: int
    tokens_used: TokensUsed
    tool_calls_used: int
    wall_clock_seconds: float
    stop_reason: str | None
```

### 2\. The agent-config loader

```python
def load_analyst_agent_config(
    agents_yaml_path: Path | None = None,
) -> BaseAgentConfig:
    """Read ``config/agents.yaml`` and return the analyst entry."""
```

Mirror synthesizer's `load_synthesizer_agent_config`. Defaults to the in-tree `config/agents.yaml`; tests and the verification script can override via `agents_yaml_path`.

### 3\. Public entry point

```python
async def run_analyst(
    *,
    mode: Literal["normal", "watchlist"],
    synthesizer_text: str,
    retrieval_store: RetrievalStore,
    analyst_view: AnalystView,
    risk_budget: RiskBudgetConsumption,
    active_risk_parameters: ActiveRiskParameterSet,
    profile_feature_flags: FeatureFlagsView,
    library_config: LibraryConfig,
    library_market: MarketInputs,
    sector_resolver: Callable[[str], str],
    portfolio_state_snapshot: PortfolioStateSnapshot,
    active_sectors: frozenset[str],
    invocation_id: str,
    timestamp: datetime,
    state_delivery_config: StateDeliveryConfig,
    options_enabled: bool,
    short_selling_enabled: bool,
    halt_context: HaltModeContext | None = None,
    archive_root: Path | None = None,
    sdk_query_fn: Callable[..., AsyncIterator[Any]] | None = None,
    agent_config: BaseAgentConfig | None = None,
    borrow_cost_resolver: Callable[[str], float] | None = None,
) -> AnalystResult:
    """Invoke the analyst and return an :class:`AnalystResult`.

    Resolves the analyst's :class:`BaseAgentConfig` from ``config/agents.yaml``
    via the existing :class:`AgentsConfig` loader unless an ``agent_config`` is
    supplied — the pipeline composition runner forwards a per-trigger-overridden
    config so ``run_types/<trigger>.yaml`` budget knobs reach the analyst slot.

    Builds the validation-tool initial state, composes the input bundle (mode-
    specific renderer), invokes :func:`invoke_analyst`, and returns the parsed
    output bundled with invocation metadata.

    Any :class:`HarnessFailure` raised by the harness propagates up unchanged.
    """
```

Body:

1. Resolve `agent_config` (passed in or loaded from yaml).
2. Build `initial_validation_state` via `build_initial_validation_state(...)`.
3. Compose user-message via `assemble_input_bundle_normal` or `assemble_input_bundle_halt` based on `mode`. (The halt-mode path requires `halt_context`; raise a clear error if `mode == "watchlist"` and `halt_context is None`.)
4. Call `invoke_analyst(...)` with the prepared inputs and `active_sectors`.
5. Wrap `HarnessSuccess` in `AnalystResult` and return.

### 4\. Public surface

```python
__all__ = ["AnalystResult", "load_analyst_agent_config", "run_analyst"]
```

### Out of scope

* The orchestration of the validation-tool state across multiple agents — analyst, strategist, PM each get fresh state per invocation. Cross-agent cumulative analysis lives in the proposal pre-processor.
* The pipeline composition itself (analysis-layer pipeline composition wiring To-do, ALP-276).

## Acceptance criteria

- [ ] `src/alphamind/decision/analyst/runner.py` exists with `AnalystResult`, `load_analyst_agent_config`, `run_analyst`.
- [ ] `run_analyst` accepts every parameter listed in step 3 with the right types.
- [ ] `mode == "normal"` calls `assemble_input_bundle_normal`; `mode == "watchlist"` calls `assemble_input_bundle_halt` (with `halt_context` required).
- [ ] Failure to load `agents.yaml` (file missing, invalid yaml, no analyst entry) propagates as a clear error from `load_analyst_agent_config`.
- [ ] Any `HarnessFailure` raised by `invoke_analyst` propagates unchanged.
- [ ] `tests/decision/analyst/test_runner.py` covers (using a stub `sdk_query_fn`): clean success in normal mode, clean success in watchlist mode, watchlist-mode-without-halt-context error, harness-failure propagation, agent_config override path (per-trigger budget reaches the harness).
- [ ] All tests pass under `uv run pytest -n auto`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` clean.

## Verification

`uv run pytest tests/decision/analyst/test_runner.py -n auto` passes.