# 04 — Input bundle assembler (normal + halt)

## Goal

Author the pure function that composes the PM's user-message text from the four input shapes (PM guardrail header, tool-reminder block, pre-processor bundle, synthesizer brief, portfolio state). Two functions: `assemble_input_bundle_normal(...)` and `assemble_input_bundle_halt(...)`. The runner (story 08) dispatches based on a caller-supplied mode parameter — never inferred from breach state. Mirrors strategist story 04 ([ALP-304](<https://linear.app/alphamind-jatassi/issue/ALP-304>)) extended for the PM's pre-processor-bundle rendering.

## Reading

* `docs/design/04-decision-layer/portfolio-manager.md` § Inputs — names the four input components in source-order.
* `docs/design/06-risk-guardrails/state-delivery.md` § Portfolio manager guardrail state header — names the rich PM-specific block (cross-constraint impact summary, recent engine-originated actions, regime overrides, correlation state, dependency-risk flag).
* `docs/design/06-risk-guardrails/state-delivery.md` § Halt-mode header modifications § PM — risk reduction mode — names the halt-mode-specific block (banner, pending orders review, restricted-actions reminder).
* `docs/design/04-decision-layer/proposal-pre-processor.md` and `docs/design/04-decision-layer/proposal-pre-processor-bundle-schema.md` — bundle shape this story renders into the user message.
* `prompts/decision/pm.md` — `<inputs>` section names the source-order; the assembler matches.
* `src/alphamind/risk_guardrails/state_delivery/portfolio_manager.py` — `render_pm_header` (normal mode) + auxiliary types (`CrossConstraintImpact`, `CrossConstraintImpactPerRule`, `RegimeOverride`, `CorrelationState`, `DependencyRiskFlag`). Story 04 calls these renderers.
* `src/alphamind/risk_guardrails/state_delivery/halt_mode.py` — `render_pm_header_halt_mode` (risk-reduction mode); accepts `pending_orders` + `current_price_lookup`.
* `src/alphamind/portfolio_state/consumers/portfolio_manager.py` — `PortfolioManagerView` + `SnapshotBackedThesisComponentReader`. The bundle assembler reads `pm_view` for the portfolio-state section.
* `src/alphamind/decision/proposal_pre_processor/__init__.py` — `ProposalPreProcessorBundle` and the wrapped-record models. The bundle is rendered into the user message.
* `src/alphamind/decision/strategist/input_bundle.py` — sibling pattern to mirror (the structured `=== … ===` section markers, the activity-log rendering of `§5a/§5b/§5c`, the per-position rendering of P/L / distance-to-target).
* `src/alphamind/decision/analyst/input_bundle.py` — sibling pattern; simpler since analyst doesn't read pre-processor bundle.
* Parent issue [ALP-117](<https://linear.app/alphamind-jatassi/issue/ALP-117>) — Pre-resolved decisions (M), (N) for mode dispatch and activity-log rendering.

## Depends on

None — Wave 1 dispatch.

## Scope

Code at `src/alphamind/decision/portfolio_manager/input_bundle.py`. Tests at `tests/decision/portfolio_manager/test_input_bundle.py`.

### 1\. `assemble_input_bundle_normal(...)` — normal mode composition

Author a pure function with this signature shape (mirror analyst's `assemble_input_bundle_normal` keyword-only signature):

```
def assemble_input_bundle_normal(
    *,
    pm_view: PortfolioManagerView,
    pre_processor_bundle: ProposalPreProcessorBundle,
    synthesizer_brief_text: str,
    invocation_id: str,
    timestamp: datetime,
    options_enabled: bool,
    short_selling_enabled: bool,
    active_sectors: tuple[str, ...],
    state_delivery_config: StateDeliveryConfig,
    sector_resolver: Callable[[PositionRecord], str | None],
    total_portfolio_value_usd: float,
    available_for_new_positions_usd: float,
    cross_constraint_impact: CrossConstraintImpact,
    tool_names: tuple[str, ...],
    sector_label_display: dict[str, str] | None = None,
    regime_transition_breaches: tuple[RegimeTransitionBreach, ...] = (),
    active_regime_overrides: tuple[RegimeOverride, ...] = (),
    correlation_state: CorrelationState | None = None,
    dependency_risk_flag: DependencyRiskFlag | None = None,
) -> str: ...
```

The composed user-message text is composed from these blocks in this order, separated by blank lines:

* `=== GUARDRAIL STATE === ` block — produced by `render_pm_header(...)` with all the PM-specific arguments threaded through.
* `=== AVAILABLE TOOLS ===` block — pure-text reminder listing the four tool names with one-line descriptions. Mirror strategist's tool-reminder block; extend with `submit_envelope` and `get_thesis_components`. Tool names come from the `tool_names` parameter (the runner will pass `(\"mcp__alphamind_state_delivery_validation__validate_guardrail\", \"mcp__alphamind_synthesizer_retrieval__retrieve_brief\", \"mcp__alphamind_portfolio_state_thesis_components__get_thesis_components\", \"mcp__alphamind_execution_oms_submit__submit_envelope\")` or whatever wire-form names the harness assigns).
* `=== PROPOSAL PRE-PROCESSOR BUNDLE ===` block — rendered representation of the `pre_processor_bundle` parameter. Use `bundle.model_dump_json(indent=2)` (Pydantic's serializer preserving the bundle byte-for-byte, since the wrapped records hold byte-preserved analyst/strategist records). The block has a header, the JSON content, and a closing marker.
* `=== SYNTHESIZER BRIEF ===` block — `synthesizer_brief_text` verbatim, surrounded by header/closing markers.
* `=== PORTFOLIO STATE ===` block — composed from `pm_view`. Render in this order:
  * Per-position records (one per `pm_view.positions[i]`) with: position_id, sector, asset_type, direction, current quantity, current P/L (absolute + % since open), distance to target, distance to stop, position age in hours, full thesis summary at the **summary level** (not component-level — components are loaded via `get_thesis_components` tool), inline §5c position-modification-trail entries from `pm_view.position_modification_trail.get(position_id, ())`.
  * `=== ACTIVITY LOG (intra-invocation) ===` rendering `pm_view.intra_invocation_changelog` — one line per entry with timestamp + event_type + position_id/order_id + brief detail summary.
  * `=== ACTIVITY LOG (recent PM decisions) ===` rendering `pm_view.recent_pm_decision_log` — same shape.
  * `=== RECENT THESIS RESOLUTIONS ===` rendering `pm_view.recent_thesis_resolutions` — one line per resolution with position_id + resolution_category + timestamp.
  * `=== ABANDONED OPENINGS / ACTIONS ===` rendering `pm_view.abandoned_openings` and `pm_view.abandoned_actions` — informational only; the analyst and strategist own re-evaluation of those.
  * `=== THESIS QUALITY AGGREGATE ===` rendering `pm_view.thesis_quality_aggregates` (a small summary block).

Helpers (private to this module): `_render_per_position_record(position, bracket, modification_trail)`, `_render_activity_log_block(header, entries)`, `_format_pnl_pct(...)`, `_format_distance_pct(...)`, `_format_position_age(...)`. Mirror strategist's helpers' shape and naming.

### 2\. `assemble_input_bundle_halt(...)` — halt-mode composition

Author a parallel function:

```
def assemble_input_bundle_halt(
    *,
    halt_state: HaltState,
    pm_view: PortfolioManagerView,
    pre_processor_bundle: ProposalPreProcessorBundle,
    synthesizer_brief_text: str,
    invocation_id: str,
    timestamp: datetime,
    pending_orders: tuple[OrderRecord, ...],
    current_price_lookup: Callable[[str], float],
    options_enabled: bool,
    short_selling_enabled: bool,
    active_sectors: tuple[str, ...],
    state_delivery_config: StateDeliveryConfig,
    sector_resolver: Callable[[PositionRecord], str | None],
    total_portfolio_value_usd: float,
    available_for_new_positions_usd: float,
    cross_constraint_impact: CrossConstraintImpact,
    tool_names: tuple[str, ...],
    sector_label_display: dict[str, str] | None = None,
    regime_transition_breaches: tuple[RegimeTransitionBreach, ...] = (),
    active_regime_overrides: tuple[RegimeOverride, ...] = (),
    correlation_state: CorrelationState | None = None,
    dependency_risk_flag: DependencyRiskFlag | None = None,
) -> str: ...
```

Same composition order, but:

* The header block is `render_pm_header_halt_mode(...)` — which itself prepends the halt banner, inserts the pending-orders review block, and annotates hard blocks with `OPEN: BLOCKED (halt mode)` + `ADD: BLOCKED (halt mode)` lines.
* The tool-reminder block names the same four tools; halt-mode availability does not subtract any tool. (Per parent decision (M), the strategist's halt mode keeps `validate_guardrail` available — same logic applies here for `submit_envelope` for CLOSE / ADJUST / CANCEL.)
* The pre-processor bundle render is unchanged — pre-processor halt-mode bundles already carry `analyst_section.mode = "watchlist"` + `strategist_section.mode = "defensive_posture"`.
* Other blocks unchanged.

### 3\. Public surface and tests

The module exports `assemble_input_bundle_normal` and `assemble_input_bundle_halt` only. No re-export from `__init__.py` necessary (callers import from `alphamind.decision.portfolio_manager.input_bundle`).

Tests under `tests/decision/portfolio_manager/test_input_bundle.py`:

* `test_normal_bundle_contains_all_six_section_markers` — assembled text contains `=== GUARDRAIL STATE ===`, `=== AVAILABLE TOOLS ===`, `=== PROPOSAL PRE-PROCESSOR BUNDLE ===`, `=== SYNTHESIZER BRIEF ===`, `=== PORTFOLIO STATE ===`, `=== ACTIVITY LOG (intra-invocation) ===`, `=== ACTIVITY LOG (recent PM decisions) ===`.
* `test_halt_bundle_contains_halt_banner` — assembled text contains the `** HALT MODE ACTIVE **` banner from `render_pm_header_halt_mode`.
* `test_pre_processor_bundle_serialized_byte_for_byte` — the rendered pre-processor bundle JSON, when re-parsed, equals the input bundle byte-for-byte (round-trip through `model_dump_json(indent=2)` / `model_validate_json`).
* `test_synthesizer_brief_verbatim` — the synthesizer brief text appears in the bundle exactly as passed in.
* `test_position_record_renders_pnl_and_distances` — a known position with known P/L produces the expected formatted string slice.
* `test_activity_log_blocks_match_view_entries` — the activity-log blocks contain one line per `intra_invocation_changelog` and `recent_pm_decision_log` entry.
* `test_position_modification_trail_inline` — a position with two modification-trail entries renders both inline within that position's record.
* `test_pure_no_io` — the function is pure: no logging, no clock reads, no file I/O, no UUID generation. Construct a fixture, call twice with identical inputs, assert byte-equal output.

Use the in-memory fixture builders from `src/alphamind/scripts/verify_strategist.py` (the `_StrategistView` builders) as references for constructing realistic `PortfolioManagerView` test fixtures; or build PM-specific minimal fixtures inline.

### Out of scope

* The harness (story 07).
* The runner (story 08).
* MCP wrappers (stories 05, 06c).
* The validator (story 06b) — the input bundle does not validate envelopes; it only renders inputs.
* Computing the `cross_constraint_impact` parameter — the caller (verify script or pipeline composition) computes from `pre_processor_bundle.aggregate_observations.combined_set_impact.per_rule` and threads it into the renderer. This story accepts it as a parameter.

## Acceptance criteria

- [ ] `src/alphamind/decision/portfolio_manager/input_bundle.py` exists exporting `assemble_input_bundle_normal` and `assemble_input_bundle_halt`.
- [ ] Both functions are pure (no logging, no clock reads, no I/O, deterministic).
- [ ] Both functions accept the keyword-only signatures named in scope sections 1 and 2.
- [ ] The `assemble_input_bundle_normal` user message contains all seven section markers in correct order.
- [ ] The `assemble_input_bundle_halt` user message contains the halt banner from `render_pm_header_halt_mode`.
- [ ] The pre-processor bundle is rendered byte-for-byte via `model_dump_json(indent=2)`.
- [ ] The synthesizer brief text appears verbatim.
- [ ] Per-position records render P/L (absolute + %), distance-to-target, distance-to-stop, position age, full thesis summary, and inline position_modification_trail entries.
- [ ] All three activity-log surfaces (§5a, §5b, §5c) are rendered per parent decision (N).
- [ ] `tests/decision/portfolio_manager/test_input_bundle.py` covers each acceptance bullet with at least one assertion.
- [ ] `uv run pytest tests/decision/portfolio_manager/ -n auto` passes.
- [ ] `uv run ruff check .` and `uv run ruff format .` pass.
- [ ] `uv run mypy` passes without new errors.

## Verification

Run `uv run pytest tests/decision/portfolio_manager/test_input_bundle.py -n auto` — all assertions pass. Construct a minimal `PortfolioManagerView` + `ProposalPreProcessorBundle` in a Python REPL via the runner's signature; call `assemble_input_bundle_normal(...)`; confirm the assembled text reads cleanly and follows the source-order in `prompts/decision/pm.md`'s `<inputs>` section.
