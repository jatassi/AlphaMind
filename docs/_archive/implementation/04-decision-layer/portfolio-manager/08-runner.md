# 08 — Runner

## Goal

Author the public `run_portfolio_manager(...)` entry point that composes initial states, dispatches the input bundle by mode, invokes the harness, and returns a `PMResult` bundling the parsed `PMCompletionRecord` + invocation metadata + the engine-stub's submission log. Mirrors strategist's runner ([ALP-308](<https://linear.app/alphamind-jatassi/issue/ALP-308>)) extended with the additional state-cell construction (submit_envelope) and thesis-component-reader threading.

## Reading

* `src/alphamind/decision/strategist/runner.py` — sibling pattern; closest analogue. The runner this story's structure mirrors most closely.
* `src/alphamind/decision/analyst/runner.py` — sibling pattern (also relevant for the agents.yaml loader, mode-dispatch via `_assemble_user_message`, and `AgentName.portfolio_manager` resolution).
* `src/alphamind/decision/portfolio_manager/harness.py` (story 07 / [ALP-329](<https://linear.app/alphamind-jatassi/issue/ALP-329>)) — `invoke_pm`, `HarnessSuccess`, exception types.
* `src/alphamind/decision/portfolio_manager/input_bundle.py` (story 04 / [ALP-324](<https://linear.app/alphamind-jatassi/issue/ALP-324>)) — input bundle assemblers.
* `src/alphamind/risk_guardrails/state_delivery/validation_tool_mcp.py` — `build_initial_validation_state(...)` factory the runner uses.
* `src/alphamind/execution/oms/submit_envelope_mcp.py` (story 06c / [ALP-328](<https://linear.app/alphamind-jatassi/issue/ALP-328>)) — `build_initial_submit_envelope_state(...)` factory the runner uses.
* `src/alphamind/portfolio_state/consumers/portfolio_manager.py` — `PortfolioManagerView`, `SnapshotBackedThesisComponentReader` (the runner constructs this from the snapshot).
* `src/alphamind/config/models/agents.py` — `AgentsConfig`, `AgentName.portfolio_manager`, `BaseAgentConfig`.
* Parent issue [ALP-117](<https://linear.app/alphamind-jatassi/issue/ALP-117>) — Pre-resolved decisions (J), (M) for sector resolver and mode dispatch.

## Depends on

* [ALP-329](<https://linear.app/alphamind-jatassi/issue/ALP-329>) (this work tree, story 07) — provides `invoke_pm` and `HarnessSuccess`.

## Scope

Code at `src/alphamind/decision/portfolio_manager/runner.py`. Tests at `tests/decision/portfolio_manager/test_runner.py`.

### 1\. Constants

Mirror analyst's runner:

* `_REPO_ROOT = Path(__file__).resolve().parents[4]`.
* `_AGENTS_YAML = _REPO_ROOT / "config" / "agents.yaml"`.
* `PM_TOOL_NAMES: tuple[str, ...] = (...)` — the four wire-form tool names: `mcp__alphamind_state_delivery_validation__validate_guardrail`, `mcp__alphamind_synthesizer_retrieval__retrieve_brief`, `mcp__alphamind_portfolio_state_thesis_components__get_thesis_components`, `mcp__alphamind_execution_oms_submit__submit_envelope` (or whatever wire-form names the four MCP factories actually emit).

### 2\. `PMResult` Pydantic model

Mirror strategist's `StrategistResult`:

```python
class PMResult(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)
    output: PMCompletionRecord
    submission_log: tuple[SubmissionLogEntry, ...]
    retry_count: int
    tokens_used: TokensUsed
    tool_calls_used: int
    wall_clock_seconds: float
    stop_reason: str | None
```

### 3\. `load_pm_agent_config(agents_yaml_path=None) -> BaseAgentConfig`

Mirror analyst's `load_analyst_agent_config`. Reads `config/agents.yaml`, validates via `AgentsConfig.model_validate`, returns `cfg.agents[AgentName.portfolio_manager]`.

### 4\. `run_portfolio_manager(...)` async function

Author the function with this signature:

```python
async def run_portfolio_manager(
    *,
    mode: Literal["normal", "halt"],
    pre_processor_bundle: ProposalPreProcessorBundle,
    synthesizer_text: str,
    retrieval_store: RetrievalStore,
    pm_view: PortfolioManagerView,
    thesis_component_reader: PortfolioManagerThesisComponentReader,
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
    total_portfolio_value_usd: float,
    available_for_new_positions_usd: float,
    cross_constraint_impact: CrossConstraintImpact,
    halt_state: HaltState | None = None,
    pending_orders: tuple[OrderRecord, ...] = (),
    current_price_lookup: Callable[[str], float] | None = None,
    sector_label_display: dict[str, str] | None = None,
    regime_transition_breaches: tuple[RegimeTransitionBreach, ...] = (),
    active_regime_overrides: tuple[RegimeOverride, ...] = (),
    correlation_state: CorrelationState | None = None,
    dependency_risk_flag: DependencyRiskFlag | None = None,
    sector_resolver_position: Callable[[PositionRecord], str | None] | None = None,
    archive_root: Path | None = None,
    sdk_query_fn: Callable[..., AsyncIterator[Any]] | None = None,
    agent_config: BaseAgentConfig | None = None,
    borrow_cost_resolver: Callable[[str], float] | None = None,
) -> PMResult: ...
```

Behavior:

1. Validate mode-conditional inputs. `mode == "halt"` requires `halt_state is not None`, `pending_orders` (may be empty tuple), and `current_price_lookup is not None`. Mirror analyst's runner-side guard.
2. Resolve `agent_config` — `agent_config or load_pm_agent_config()`.
3. Construct two state cells:
   * `initial_validation_state = build_initial_validation_state(invocation_id=invocation_id, starting_snapshot=portfolio_state_snapshot, starting_risk_budget=risk_budget, starting_active_risk_parameters=active_risk_parameters, profile_feature_flags=profile_feature_flags, library_config=library_config, library_market=library_market, sector_resolver=sector_resolver, borrow_cost_resolver=borrow_cost_resolver)`.
   * `initial_submit_envelope_state = build_initial_submit_envelope_state(invocation_id=invocation_id, starting_validation_state=initial_validation_state)` — note this is the SAME validation-state cell; the engine-stub re-uses it for cumulative-impact tracking unified across both tools.
4. Dispatch input bundle by mode. Construct `tool_names = PM_TOOL_NAMES`. Pull out `_assemble_user_message(...)` helper that branches on mode and calls `assemble_input_bundle_normal` or `assemble_input_bundle_halt` with the appropriate parameter set. Mirror analyst's `_assemble_user_message`.
5. Construct `halt_mode = (mode == "halt")` for the harness's `halt_mode` parameter.
6. Invoke `harness_result = await invoke_pm(agent_config=resolved_config, user_message=user_message, invocation_id=invocation_id, initial_validation_state=initial_validation_state, initial_submit_envelope_state=initial_submit_envelope_state, retrieval_store=retrieval_store, thesis_component_reader=thesis_component_reader, pre_processor_bundle=pre_processor_bundle, pm_view=pm_view, active_sectors=active_sectors, halt_mode=halt_mode, sector_resolver=sector_resolver, library_config=library_config, library_market=library_market, archive_root=archive_root, sdk_query_fn=sdk_query_fn)`.
7. Per the parent's "fail-closed" invariant: any `HarnessFailure` propagates up unchanged — the runner does NOT catch and degrade.
8. Construct + return `PMResult` from `harness_result`'s fields.

### 5\. Tests

Tests at `tests/decision/portfolio_manager/test_runner.py`:

* `test_runner_loads_agent_config` — given no override, the runner loads the `portfolio_manager` slot from `config/agents.yaml`.
* `test_runner_dispatches_normal_mode_to_normal_assembler` — given `mode="normal"`, the assembled user message contains the normal-mode `=== GUARDRAIL STATE ===` block (no halt banner).
* `test_runner_dispatches_halt_mode_to_halt_assembler` — given `mode="halt"` + `halt_state`, the user message contains the halt banner.
* `test_runner_raises_on_halt_mode_without_halt_state` — `mode="halt"` + `halt_state=None` raises `ValueError`.
* `test_runner_raises_on_halt_mode_without_current_price_lookup` — `mode="halt"` + `current_price_lookup=None` raises `ValueError`.
* `test_runner_constructs_state_cells_per_invocation` — call the runner twice with stub harness; assert the two harness calls received distinct state-cell instances.
* `test_runner_propagates_harness_failure` — stub harness raises `MalformedOutputFailure`; runner does NOT catch.
* `test_runner_returns_pmresult_with_submission_log` — happy path; assert `PMResult.submission_log` is non-empty when the stub harness simulates submissions.

The tests stub the harness via `sdk_query_fn` injection.

### 6\. Update `__init__.py`

Re-export `run_portfolio_manager`, `PMResult`, `PM_TOOL_NAMES`, `load_pm_agent_config` from `alphamind.decision.portfolio_manager`. Mirror analyst's `__init__.py` exports.

### Out of scope

* The verify script (story 09) — calls `run_portfolio_manager` against the real SDK.
* Live pipeline composition wiring — that lives in [ALP-310](<https://linear.app/alphamind-jatassi/issue/ALP-310>). The runner accepts the projected `pm_view` directly.

## Acceptance criteria

- [ ] `src/alphamind/decision/portfolio_manager/runner.py` exists exporting `run_portfolio_manager`, `PMResult`, `PM_TOOL_NAMES`, `load_pm_agent_config`.
- [ ] `__init__.py` re-exports the public surface.
- [ ] The runner constructs the validation + submit_envelope state cells fresh per invocation; the cells share the same `ValidationToolState` for unified cumulative tracking.
- [ ] Mode dispatch is keyword-only; `halt` requires `halt_state` + `current_price_lookup` + `pending_orders`.
- [ ] All eight tests pass.
- [ ] `uv run pytest tests/decision/portfolio_manager/test_runner.py -n auto` passes.
- [ ] `uv run ruff check .` and `uv run ruff format .` pass.
- [ ] `uv run mypy` passes without new errors.

## Verification

Run `uv run pytest tests/decision/portfolio_manager/test_runner.py -n auto` — all eight tests pass. The runner is invoked from story 09's verify script.
