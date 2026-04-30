---
status: not_started
completed_date:
commit_id:
---

# 08 — End-to-end verification

## Goal

Land the integration test that exercises every state-delivery surface against a single hand-built `PortfolioStateSnapshot` fixture and confirms the renderers, halt-mode wrappers, emergency wrapper, and validation tool all compose into a coherent, design-conformant header set. The test is the work tree's release gate: every prior story is unit-tested in isolation; story 08 confirms they integrate.

## Reading

- `docs/design/06-risk-guardrails/state-delivery.md` — every header variant, the guardrail validation tool contract, the cross-constraint impact summary, the halt-mode and emergency-invocation header modifications. The full surface is exercised in this story's tests.
- `docs/implementation/02-distillation-layer/replay-harness/09-end-to-end-verification.md` — sibling pattern for an integration story that exercises every prior story's surface; the verification structure mirrors this.
- `docs/implementation/01-data-layer/portfolio-state/09-end-to-end-verification.md` — sibling pattern for an integration story over a hand-built snapshot fixture.
- `02-package-skeleton-and-config.md` through `07-guardrail-validation-tool.md` — every prior story in this work tree.
- `src/alphamind/portfolio_state/repository.py` — `RepositoryFixture`, `StubPortfolioStateRepository` — the test-double pattern this story builds against.
- `src/alphamind/portfolio_state/assembler.py` — `assemble_portfolio_state_snapshot(...)` — the snapshot assembler that takes a fixture-backed repository and produces a `PortfolioStateSnapshot`.

## Depends on

- 02 (package skeleton + config)
- 03 (shared rendering primitives)
- 04a (analyst header renderer)
- 04b (strategist header renderer)
- 04c (PM header renderer)
- 05 (halt-mode wrappers)
- 06 (emergency invocation header)
- 07 (guardrail validation tool)
- **Cross-feature dependencies (load-bearing for this story specifically):**
  - The `rules-and-limits` work tree's stories that produce a populated `RiskBudgetConsumption` and `ActiveRiskParameterSet` from a real snapshot. For story 08, hand-constructed `RiskBudgetConsumption` / `ActiveRiskParameterSet` fixtures are acceptable — the test's job is to verify state-delivery's composition, not to depend on the production guardrail-evaluation pipeline.
  - The `guardrail-evaluation` work tree's `evaluate_proposals` entry point used by the validation tool. Same fixture-substitution rule applies for the validation-tool portion of this test.
  - When BOTH cross-feature work trees are `done`, this story's tests can additionally include a real-snapshot-end-to-end variant that uses production `RiskBudgetConsumption` and `evaluate_proposals`. Until then, the story tests against typed fixtures.

## Scope

In scope, all under `tests/risk_guardrails/state_delivery/test_end_to_end.py`. No production-code changes — story 08 is a verification-only story (matches the replay-harness story 09 pattern that committed binary fixtures + test).

### 1. Master fixture: `_build_full_system_snapshot()`

A module-level fixture builder that returns a `PortfolioStateSnapshot` with:

- 12 open positions across 4 sectors (3 tech, 3 semis, 3 financials, 3 energy):
  - 8 long equity, 2 short equity, 2 long calls.
  - Position weights spanning 0.5% to 4.8% (NORMAL) and one position at 4.5% (WARNING zone) — ensures every zone tag is exercised in the per-position proximity block.
  - Includes one position with no resolved sector (Unclassified group exercise).
- Active risk parameter set with regime `NORMAL`, transition state `STABLE`, parameter_change_flag `False`, all 18 rule entries populated.
- Risk budget consumption with all 18 rule entries; one entry (sector concentration tech) at WARNING zone, one entry (gross exposure) at CRITICAL zone, no entries at BLOCKED zone.
- Drawdown state at `daily_zone=NORMAL`, `cumulative_zone=NORMAL`, `cumulative_tier=None`.
- Cash ledger with $50K total portfolio value, $5K deployable.
- Active theses for 10 of 12 positions (2 orphan-spin-off positions).
- 1 abandoned analyst-originating opening, 1 abandoned strategist-originating action of each type (ADD, ADJUST, CLOSE, CANCEL).
- Recent PM decision log with 3 entries (2 approves, 1 reject).
- 1 engine-originated CLOSE entry in the intra-invocation changelog.
- Thesis quality aggregates with realistic non-zero values.
- Bracket records for 8 positions (long equities); 4 positions without brackets (the 2 short equities, 2 long calls — options brackets handled by continuous monitor per design).

The fixture is constructed via `RepositoryFixture(...)` then `StubPortfolioStateRepository(fixture)` then `await assemble_portfolio_state_snapshot(repository, ...)` — using the existing portfolio-state assembler infrastructure.

### 2. Master fixture: `_build_typed_inputs()`

Companion helper returning the typed inputs the audience renderers need beyond the snapshot:

- `cross_constraint_impact: CrossConstraintImpact` with 3 per-rule entries, no flagged rules, available_capital_before=$5K, available_capital_after=$3.5K.
- `regime_transition_breaches: tuple[RegimeTransitionBreach, ...] = ()` (no regime transition active in the master fixture).
- `active_regime_overrides: tuple[RegimeOverride, ...] = ()`.
- `correlation_state: CorrelationState | None = None` (12 positions but no correlation computed for the fixture; tests verify the omitted-block path even though position count exceeds threshold).
  - Variant: a `_with_correlation_state()` helper produces a populated `CorrelationState` for tests that exercise the rendered block.
- `dependency_risk_flag: DependencyRiskFlag | None = None`. Same pattern with a `_with_dependency_risk_flag()` helper.

### 3. Test class: `TestNormalHeaderRendering`

Tests exercising the three normal renderers (no halt mode, no emergency):

- `test_analyst_header_full_system` — renders the analyst header against the master fixture; the output's first three lines match the envelope-open + regime-line + blank pattern; the held-positions block has 12 rows; the abandoned-openings block has 1 row; the hard-blocks block is omitted (no BLOCKED entries).
- `test_strategist_header_full_system` — renders the strategist header; per-position proximity block has 12 rows with correct zone tags (one row has `[⚠ WARNING]`); sector breakdown has 4 sector groups + 1 unclassified group; drawdown-state block has no cumulative-tier line; abandoned-actions block has 4 rows.
- `test_pm_header_full_system` — renders the PM header; cross-constraint-impact block has 3 per-rule lines + 1 capital line; recent-engine-actions block has 1 row; correlation-state block is omitted (correlation_state=None); dependency-risk-flag block is omitted; hard-blocks block is omitted.
- `test_three_renderers_share_per_position_proximity_block` — the per-position proximity block in the strategist header equals (line-by-line) the same block in the PM header for the same fixture, confirming the shared helper produces identical output.
- `test_three_renderers_share_sector_breakdown_block` — same shape for the sector breakdown.
- `test_no_renderer_introduces_double_blank_lines` — for each of the three rendered headers, no `\n\n\n` substring exists.
- `test_no_renderer_emits_trailing_blank` — the last character of each rendered header is `=` (the closing line); the second-to-last character is `\n`.

### 4. Test class: `TestHaltModeWrappers`

Tests exercising the halt-mode wrappers:

- `test_analyst_halt_mode` — renders the analyst halt-mode wrapper with a `HaltState(daily_halt_active=True, cumulative_full_halt_active=False, daily_drawdown_pct=2.6, daily_drawdown_limit_pct=2.5)`; the banner is present; the capital block has the documented replacement rows; all other blocks are line-by-line identical to the normal analyst header (after stripping the banner + capital substitution).
- `test_strategist_halt_mode_defensive_posture` — renders the strategist halt-mode wrapper; banner present with `Mode: DEFENSIVE POSTURE` line; all other blocks identical to normal strategist header.
- `test_pm_halt_mode_risk_reduction` — renders the PM halt-mode wrapper with the documented additional inputs (`pending_orders`, `current_price_lookup`); banner has three lines (HALT MODE + Available actions + Blocked actions); pending-orders-review block is inserted; cross-constraint-impact block is replaced with the scoped variant; hard-blocks block has the OPEN/ADD halt-mode action lines appended.
- `test_pm_halt_mode_no_pending_orders` — pending-orders-review block has `  None` line.
- `test_halt_state_validator` — constructing `HaltState(daily_halt_active=False, cumulative_full_halt_active=False, ...)` raises `ValueError`.

### 5. Test class: `TestEmergencyWrapper`

Tests exercising the emergency wrapper:

- `test_emergency_block_appended_to_normal_analyst_header` — `prepend_emergency_block(header=normal_analyst_header, context=context)` inserts the three-line emergency block immediately after envelope-open; the rest of the header is preserved verbatim.
- `test_emergency_block_appended_to_halt_mode_pm_header` — `prepend_emergency_block(header=halt_mode_pm_header, context=context)` produces the documented composition: emergency block before halt-mode banner before normal PM header content.
- `test_emergency_block_for_each_trigger_type` — parametrize over all four `EmergencyTrigger` enum values; the rendered block contains the trigger name and trigger detail correctly.
- `test_emergency_block_minutes_formatting` — `minutes_since_last_invocation=27.4` renders as `27m`.
- `test_emergency_wrapper_rejects_missing_envelope_open` — input string without the envelope-open line raises `ValueError`.

### 6. Test class: `TestValidationTool`

Tests exercising the validation tool against a stubbed `evaluate_proposals` (the cross-feature gate from the guardrail-evaluation work tree may not be `done` at story-08 implementation time):

- `test_validation_tool_pass_path` — a single proposal validates as PASS; `proposal_index_in_invocation=1`; `cumulative_impact_note` reads "No prior proposals affect headroom calculations."; `failure_guidance is None`.
- `test_validation_tool_fail_path` — a proposal triggering a sector-concentration FAIL returns `overall="FAIL"`, `failure_guidance` containing `"Reduce size by"` and the rule label; `greeks is None` for an equity proposal.
- `test_validation_tool_cumulative_tracking` — three sequential proposals that individually pass but cumulatively breach: first PASS, second PASS, third FAIL; `accumulated_deltas` correctly grows.
- `test_validation_tool_feature_flag_early_exit` — option OPEN on a profile with `options_enabled=False` returns FAIL with no per-rule entries; the stubbed `evaluate_proposals` was not called.
- `test_validation_tool_state_immutability` — `state.with_accepted_proposal(delta)` returns a new state; the original state's `accumulated_deltas` is unchanged.
- `test_validation_tool_options_greeks_populated` — option OPEN returns a populated `Greeks` (from `guardrail_evaluation`); the stub returns deterministic greek values.

### 7. Test class: `TestComposition`

Cross-cutting tests confirming the surfaces compose correctly:

- `test_emergency_plus_halt_mode_plus_pm_header` — the full three-stage composition (`render_pm_header_halt_mode` → `prepend_emergency_block`) matches a fixture string.
- `test_validation_tool_state_starts_from_snapshot_fixture` — `ValidationToolState` constructed from the master fixture's snapshot has correctly-populated `starting_snapshot`, `starting_risk_budget`, `starting_active_risk_parameters`; `accumulated_deltas` is empty; profile_feature_flags reflects the master fixture's profile.
- `test_three_audience_headers_use_same_snapshot` — rendering all three headers against the same `PortfolioStateSnapshot` produces three coherent outputs whose envelope-open lines, regime lines, capital blocks, and headroom blocks are line-by-line identical (the shared primitives' contract).
- `test_renderer_outputs_are_pure_strings` — each renderer's output is a `str`; concatenating them via `\n\n` produces a single string with no embedded byte-order marks or non-UTF-8 characters.
- `test_full_invocation_simulation` — simulates a full agent invocation:
  1. Build snapshot from master fixture.
  2. Build `ValidationToolState`.
  3. Call `validate_guardrail(...)` with three proposals (two accepted, one rejected); observe cumulative-impact note text changes.
  4. Render analyst header with the snapshot's analyst view.
  5. Render strategist header with the snapshot's strategist view.
  6. Render PM header with the snapshot's PM view + the typed inputs.
  7. Confirm every header passes the no-double-blank-lines and no-trailing-blank invariants.

### 8. Test infrastructure

- All tests use `pytest-asyncio` (or `anyio` per the existing test infrastructure) for the async snapshot-assembler call.
- Helper `_assert_lines_equal(actual: str, expected: str)` compares strings line-by-line and produces diff-friendly failure output. Used wherever a fixture-string comparison is needed.
- A small set of canonical fixture strings (analyst normal header, strategist normal header, PM normal header) committed under `tests/risk_guardrails/state_delivery/fixtures/` as `.txt` files; the helper reads them and compares against rendered output. This isolates the format spec into reviewable text files.
  - When the design's format changes, the fixture files are updated in the same commit as the renderer changes — keeping the spec ↔ test ↔ renderer triple in sync.
- Fixtures use UTF-8 encoding without BOM; the WARNING/CRITICAL emoji codepoints render natively in fixture files.

Out of scope:
- Performance benchmarks (rendering throughput, token-count measurement) — the design's token budgets are operator-tunable infrastructure; benchmarks are deferred to a feedback-loop story that measures real invocation token usage.
- Renderer behavior under malformed snapshots (e.g., a snapshot missing required fields) — the typed Pydantic models enforce structural invariants at construction; the renderer's `ValueError` paths are exercised in per-story unit tests.
- Production wiring of the validation tool into the analyst/strategist/PM agent's tool registration — that lives in each agent's runtime story, not here.
- A real-snapshot integration test using production `RiskBudgetConsumption` populator and `evaluate_proposals` — gated on the cross-feature work trees landing; promotable from this story's deferred-test list once both gates clear.
- A property-based test (Hypothesis-style) generating arbitrary snapshots — the deterministic fixture-based tests cover the documented contract; property-based testing is a follow-up if regressions emerge.

## Notes

The integration test deliberately uses a single master fixture rather than one fixture per audience. Sharing the snapshot across all three audience headers exercises the design's "snapshot consistency" property: every header renders the same numbers in the same way. A regression in any shared primitive (regime line, capital block, sector headroom block, directional headroom block, options headroom block, hard-blocks block) is caught by all three audiences' tests.

Per `feedback_simplify_before_building.md`, the test infrastructure is small: one master fixture, one helper for line-by-line string comparison, one set of `.txt` fixture files. No test-only DSL, no parametrized test framework beyond pytest's built-in `parametrize` decorator, no fixture-generation tooling.

Per `feedback_no_inventing_component_names.md`, the test class names mirror the design's section labels (`TestNormalHeaderRendering`, `TestHaltModeWrappers`, `TestEmergencyWrapper`, `TestValidationTool`, `TestComposition`). The fixture file names mirror the renderer names (`analyst_normal.txt`, `strategist_normal.txt`, `pm_normal.txt`, `pm_halt_mode_with_emergency.txt`).

Per `feedback_avoid_numeric_anchors.md`, the master fixture's specific numeric values (12 positions, 4 sectors, $50K portfolio, etc.) are illustrative test inputs. The tests assert structural properties (block presence, row count, zone-tag presence) rather than specific dollar/percentage values. The `.txt` fixture files anchor the format spec; the tests confirm the renderer matches the format.

Per `feedback_subagent_must_commit.md`, this story's implementation involves a substantial test file plus committed `.txt` fixture files. The implementing subagent must commit the test file AND the fixture files in a single commit (or sequential commits within the same branch); fixture files are required at verification time and the orchestrator's spot-check step verifies they exist on the merged branch.

Cross-feature dependency callout: the test stubs `evaluate_proposals` from the guardrail-evaluation library. When the library's stories are `done`, this story can be re-dispatched (or an addendum story files) to replace the stub with the real call and confirm end-to-end behavior against the production library. Recording this as a follow-up at orchestration time, not as a v1 acceptance criterion.

The committed fixture files (`tests/risk_guardrails/state_delivery/fixtures/*.txt`) are the format spec's tangible artifact in the repository. A reviewer can `cat` the fixture and confirm it matches the design doc's worked example — making the design ↔ implementation pairing easy to audit.

## Acceptance criteria

- [ ] `tests/risk_guardrails/state_delivery/test_end_to_end.py` exists with the documented test classes (`TestNormalHeaderRendering`, `TestHaltModeWrappers`, `TestEmergencyWrapper`, `TestValidationTool`, `TestComposition`).
- [ ] `tests/risk_guardrails/state_delivery/fixtures/` exists with at least three committed fixture files: `analyst_normal.txt`, `strategist_normal.txt`, `pm_normal.txt`. Optionally: `analyst_halt_mode.txt`, `strategist_halt_mode.txt`, `pm_halt_mode_with_emergency.txt` for halt-mode + emergency composition variants.
- [ ] All test classes exercise their documented test cases; every test passes under `uv run pytest -n auto`.
- [ ] Master fixture builder `_build_full_system_snapshot()` produces a snapshot with 12 positions, 4 sectors + 1 unclassified position, the documented WARNING / CRITICAL zone variety, no BLOCKED entries, normal drawdown, populated abandoned blocks.
- [ ] `test_three_renderers_share_per_position_proximity_block` passes — confirming the shared helper between strategist and PM renderers produces identical output.
- [ ] `test_no_renderer_introduces_double_blank_lines` passes for each of the three normal headers, each of the three halt-mode wrappers, and the emergency-wrapper-on-halt-mode composition.
- [ ] `test_full_invocation_simulation` runs the full sequence (validate three proposals, render three headers) and every assertion passes.
- [ ] All fixture files are UTF-8 encoded without BOM.
- [ ] No production-code changes outside `src/alphamind/risk_guardrails/state_delivery/` — the story is verification-only.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
