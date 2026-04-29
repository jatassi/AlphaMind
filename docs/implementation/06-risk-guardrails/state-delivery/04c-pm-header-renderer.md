---
status: not_started
completed_date:
commit_id:
---

# 04c — Portfolio manager guardrail state header renderer

## Goal

Land the function that produces the complete PM guardrail state header — the formatted `=== GUARDRAIL STATE ===` text block delivered at the top of the PM's prompt. Composes the shared rendering primitives (story 03), the strategist-shared per-position proximity and per-sector breakdown blocks (originating in story 04b), and seven PM-specific blocks: cross-constraint impact summary, drawdown context (with daily P/L), regime-transition breaches, recent engine-originated actions, active regime overrides, correlation state, dependency risk flag. Produces the verbatim text shape documented in [`state-delivery.md § Portfolio manager guardrail state header`](../../../design/06-risk-guardrails/state-delivery.md#portfolio-manager-guardrail-state-header).

## Reading

- `docs/design/06-risk-guardrails/state-delivery.md` § Portfolio manager guardrail state header — the authoritative format spec; the PM-specific blocks are the seven listed above.
- `docs/design/06-risk-guardrails/state-delivery.md` § Cross-constraint impact summary — pre-computed by the proposal pre-processor; the renderer formats whatever the upstream computation provides.
- `docs/design/06-risk-guardrails/state-delivery.md` § Recent engine-originated actions — prominence cue for activity-log events.
- `docs/design/04-decision-layer/portfolio-manager.md` § Inputs — the PM's input contract.
- `docs/design/04-decision-layer/proposal-pre-processor.md` § §1.A `combined_set_impact` — the upstream typed shape for the cross-constraint impact summary; the renderer takes a typed input that mirrors this schema.
- `docs/design/06-risk-guardrails/regime-adaptation.md` § Override conditions — pre-event tightening, stress overlay; surfaces in the active-regime-overrides block.
- `docs/design/02-distillation-layer/internal.md` § 9b — the dependency-risk-flag input feed (max catalyst-failure exposure, effective independent thesis count, worst shared catalyst).
- `src/alphamind/portfolio_state/consumers/portfolio_manager.py` — `PortfolioManagerView`; same fields as `StrategistView` plus `thesis_quality_aggregates` and `position_modification_trail`.
- `src/alphamind/portfolio_state/records/activity_log.py` — `ActivityLogEntry`, `EventType`; the renderer reads engine-originated action entries from the changelog.
- `02-package-skeleton-and-config.md` — `state_delivery/portfolio_manager.py` is the target module; `StateDeliveryConfig` carries `recent_engine_actions.lookback_invocations`, `correlation_state.min_position_count`, `dependency_risk_flag.min_position_count`.
- `03-shared-rendering-primitives.md` — primitives reused.
- `04b-strategist-header-renderer.md` — sibling renderer; the per-position proximity and per-sector breakdown blocks live there and are imported. The import direction is `pm -> strategist` for shared helpers; both ultimately depend on `primitives.py`.
- `../regime-adaptation/02-package-skeleton-and-types.md` § 2c — canonical declaration of `RegimeTransitionBreach`; imported here, not redeclared (same as 04b).
- `../regime-adaptation/07-regime-transition-breach-detector.md` — the producer of the records this renderer consumes.

## Depends on

- 02 (package skeleton + config)
- 03 (shared rendering primitives)
- 04b (strategist header renderer — the per-position proximity and per-sector breakdown blocks live there)
- **Cross-feature dependency:** the rules-and-limits work tree's populated `RiskBudgetConsumption` and `ActiveRiskParameterSet`, plus the proposal pre-processor work tree's `combined_set_impact` typed shape (or the renderer accepts a hand-shaped input matching the documented schema in story 04c's typed inputs and integration with the real pre-processor lands in a follow-up wiring story).
- **Cross-feature dependency:** the regime-adaptation work tree's `regime_adaptation/02-package-skeleton-and-types.md` ships the canonical `RegimeTransitionBreach` typed record this renderer imports.

## Scope

In scope, all under `src/alphamind/risk_guardrails/state_delivery/portfolio_manager.py`. Tests at `tests/risk_guardrails/state_delivery/test_portfolio_manager.py`.

### 1. Public function

```python
def render_pm_header(
    *,
    pm_view: PortfolioManagerView,
    invocation_id: str,
    timestamp: datetime,
    options_enabled: bool,
    short_selling_enabled: bool,
    active_sectors: tuple[str, ...],
    config: StateDeliveryConfig,
    sector_label_display: dict[str, str] | None = None,
    sector_resolver: Callable[[PositionRecord], str | None],
    total_portfolio_value_usd: float,
    available_for_new_positions_usd: float,
    cross_constraint_impact: CrossConstraintImpact,
    regime_transition_breaches: tuple[RegimeTransitionBreach, ...] = (),
    active_regime_overrides: tuple[RegimeOverride, ...] = (),
    correlation_state: CorrelationState | None = None,
    dependency_risk_flag: DependencyRiskFlag | None = None,
) -> str
```

- `pm_view` — projected from `project_portfolio_manager_view(snapshot)`. Same fields as the strategist view, plus thesis quality aggregates and the per-position modification trail dict.
- `cross_constraint_impact` — typed input mirroring `combined_set_impact` from the pre-processor (see typed shape below). Required.
- `active_regime_overrides` — tuple of `RegimeOverride` records; empty default. When empty, the block renders as `Active regime overrides:` followed by `  None`. (Differs from regime-transition breaches which are silent on empty — the design's PM section lists `Active regime overrides:` followed by `[or "None" if no overrides active]`.)
- `correlation_state` — `CorrelationState | None`. Block omitted entirely when `None` OR when `len(pm_view.positions) < config.correlation_state_min_position_count`.
- `dependency_risk_flag` — `DependencyRiskFlag | None`. Block omitted entirely when `None` OR when `len(pm_view.positions) < config.dependency_risk_flag_min_position_count`.

### 2. Typed inputs

Declared in `state_delivery/portfolio_manager.py` (or a shared `state_delivery/types.py` if 04b promoted shared types — resolve at implementation time):

```python
class CrossConstraintImpactPerRule(BaseModel):
    model_config = ConfigDict(frozen=True)

    rule_id: str
    rule_label: str          # display label used in the rendered row
    current: float
    projected_after: float
    limit: float
    unit: str                # e.g., "% of portfolio (delta-adjusted)"; appended after numeric values

class CrossConstraintImpact(BaseModel):
    model_config = ConfigDict(frozen=True)

    per_rule: tuple[CrossConstraintImpactPerRule, ...]
    flagged_rule_ids: tuple[str, ...]    # subset of per_rule whose projected_after enters WARNING or CRITICAL zone
    available_capital_before_usd: float
    available_capital_after_usd: float

class RegimeOverride(BaseModel):
    model_config = ConfigDict(frozen=True)

    overlay_name: str        # e.g., "pre_event_tightening", "stress_overlay"
    description: str         # human-readable; e.g., "FOMC tightening — max position size -20%, no new positions in final invocation"
    expires_at: datetime | None

class CorrelationState(BaseModel):
    model_config = ConfigDict(frozen=True)

    weighted_avg_correlation: float
    correlation_limit: float
    zone: RiskZone
    highest_pairwise_position_a: str   # e.g., "POS-NVDA-001"
    highest_pairwise_position_b: str   # e.g., "POS-AMD-002"
    highest_pairwise_value: float

class DependencyRiskFlag(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_catalyst_failure_exposure_pct: float
    catalyst_failure_limit_pct: float
    zone: RiskZone
    effective_independent_thesis_count: int
    worst_shared_catalyst_label: str
    worst_shared_catalyst_position_ids: tuple[str, ...]
```

Each value object is computed upstream by a different layer (pre-processor for `cross_constraint_impact`; regime-adaptation overlays runtime for `active_regime_overrides`; the distillation layer's correlation matrix work for `correlation_state`; analysis-layer dependency clustering work for `dependency_risk_flag`). The renderer takes typed inputs from any source; production wiring happens at the PM agent's prompt-assembly stage.

### 3. Cross-constraint impact summary block

```
Cross-constraint impact summary:
  If all pending proposals are approved as-sized:
    Sector tech: 18.3% → 22.1% (within limit)
    Net long:    42.0% → 48.5% (within limit)
    Gross:       78.0% → 84.5% (within limit)
    Capital:     ${available} → ${remaining}
  [Flagged constraints: any rule that would enter WARNING or CRITICAL zone]
```

- Header line + indented `If all pending proposals are approved as-sized:` line + indented per-rule lines + indented `Capital:` line + optional indented `Flagged constraints:` line.
- Per-rule lines: `{rule_label}: {current:.1f}% → {projected_after:.1f}% ({status})` where `status` is `(within limit)` when `projected_after <= limit`, else `(would breach by {projected_after - limit:.1f}%)`.
- Numeric values use one decimal place and the unit is implied by the rule (`%` for percentage rules; the renderer reads `unit` from `CrossConstraintImpactPerRule` and includes the `%` if the unit string contains `%`, omits otherwise — keeps the renderer's units behavior consistent with the headroom blocks).
- Capital line uses dollar formatting: `Capital:     ${available_capital_before_usd:,.0f} → ${available_capital_after_usd:,.0f}`. Capital is always rendered, even if `flagged_rule_ids` is empty — the design's worked example shows it always present.
- Column alignment: rule labels left-padded to the longest label width across the per-rule list; the `:` aligns column-wise.
- Optional flagged-constraints suffix line: when `cross_constraint_impact.flagged_rule_ids` is non-empty, append `    Flagged: {comma-separated rule_labels}`. When empty, omit the line.
- When `cross_constraint_impact.per_rule` is empty (no proposals pending), the block renders as `Cross-constraint impact summary:` followed by `  No pending proposals; no projected impact.`

Helper `_render_cross_constraint_impact_block(impact: CrossConstraintImpact) -> str`.

### 4. Drawdown context block

Differs from the strategist's drawdown-state block — the PM block adds daily P/L:

```
Drawdown context:
  Daily P/L:     {current}% ({zone})
  Daily limit:   {limit}% — headroom: {remaining}%
  Cumulative:    {current}% from HWM ({zone})
  [If in reduced mode: current restrictions and tier]
```

- Daily P/L = `pm_view.portfolio_pnl.daily_total_pnl_usd / total_portfolio_value_usd * 100` (signed; the `PortfolioPnL` record carries `daily_total_pnl_usd` — realized + unrealized — and the percentage is computed at render time against the same `total_portfolio_value_usd` the capital block uses). Sign always shown (`+0.5%` / `-1.2%`).
- Daily limit + headroom from `pm_view.active_risk_parameters.entry_by_rule_id("daily_drawdown_pct").value` (limit) and `pm_view.risk_budget.entry_by_rule_id("daily_drawdown_pct").headroom` (headroom).
- Cumulative = `pm_view.drawdown.current_drawdown_pct from HWM` (no sign — cumulative drawdown is non-negative by definition). Append `(zone tag)` from `pm_view.drawdown.cumulative_zone`.
- When `pm_view.drawdown.cumulative_tier is not None`, append the same restrictions text as story 04b's drawdown-state block (`Cumulative tier: {tier} — {restrictions}`).

Helper `_render_drawdown_context_block(...)`.

### 5. Regime-transition breaches block

Same shape as story 04b's `_render_regime_transition_breaches_block` — re-use that helper from `state_delivery/strategist.py`. Imported as `from alphamind.risk_guardrails.state_delivery.strategist import _render_regime_transition_breaches_block` or promoted to a shared `_helpers.py` if both renderers want a non-private API. For this story, importing the private helper from a sibling module is acceptable (single-process use; no public-API surface concern) but flagged as a refactor target if a third audience needs it.

### 6. Recent engine-originated actions block

```
Recent engine-originated actions (since last invocation):
  {timestamp}: Engine closed POS-XYZ-001 — reason: position-level max loss (-31% of cost)
  {timestamp}: Engine trimmed POS-ABC-002 — reason: single short size limit (3.2% > 3.0%)
  ...
  [or "None" if no engine-originated actions]
```

- Rows from `pm_view.intra_invocation_changelog` filtered to entries with `event_type` in the engine-originated set (`POSITION_CLOSED` and `POSITION_TRIMMED` whose detail's `originating_actor` is the continuous monitor or guardrail enforcement layer; the exact filter logic depends on the activity-log's `originating_actor` or `provenance` field).
- The lookback window is `config.recent_engine_actions_lookback_invocations` (default `1`). The PM view's `intra_invocation_changelog` is already scoped to the current invocation; this story trusts the typed input. If a wider lookback is needed in the future, the projection step extends; the renderer continues to read the typed view.
- Per-row format: timestamp in ISO-8601 with seconds precision and `Z` suffix, `:`, single space, `Engine `, action verb (`closed` / `trimmed` / `cancelled`), single space, position-or-order ID, ` — reason: `, rule label and breach detail.
- Currently the `ActivityLogEntry` detail union may not surface `originating_actor` directly; the renderer reads whatever discriminator the entry's discriminated union exposes. If the activity log's typed shape evolves, the renderer's filter logic adapts; this story uses whatever fields exist at implementation time.
- Empty filtered list produces a `  None` line.

Helper `_render_recent_engine_actions_block(...)`. The filter logic is small enough to inline; if it grows, promote to `state_delivery/_engine_actions.py` as a private helper module.

### 7. Active regime overrides block

```
Active regime overrides:
  [If pre-event tightening active: event name, adjusted limits, expiration]
  [If stress overlay active: trigger, adjusted limits]
  [or "None" if no overrides active]
```

- Header line + one row per `RegimeOverride` entry.
- Per-row format: indented `{description}` (the upstream supplies a human-readable string); when `expires_at` is non-`None`, suffix ` (expires {timestamp})` with ISO-8601 timestamp.
- Empty `active_regime_overrides` produces a `  None` line.

Helper `_render_active_regime_overrides_block(...)`.

### 8. Correlation state block (omitted if < 3 positions)

```
Correlation state:                         [omitted if < 3 concurrent positions]
  Portfolio weighted avg correlation: {value} / {limit} [{zone}]
  Highest pairwise: POS-X ↔ POS-Y = {value}
```

- When `correlation_state is None` OR `len(pm_view.positions) < config.correlation_state_min_position_count`, the entire block is omitted.
- Otherwise: header line + two indented rows.
- Numeric values render with two decimal places (correlations are 0–1; one decimal would lose meaningful precision).

Helper `_render_correlation_state_block(...)`.

### 9. Dependency risk flag block (omitted if < 3 positions)

```
Dependency risk flag:                       [omitted if < 3 concurrent positions]
  Max catalyst-failure exposure: {value}% / {limit}% [{zone}]
  Effective independent thesis count: {n}
  Worst shared catalyst: "{label}" — positions: {POS-A, POS-B, ...}
```

- When `dependency_risk_flag is None` OR `len(pm_view.positions) < config.dependency_risk_flag_min_position_count`, the entire block is omitted.
- Otherwise: header line + three indented rows.
- The `Worst shared catalyst` label renders in double quotes; the position list joins position IDs with `, ` and wraps in braces.

Helper `_render_dependency_risk_flag_block(...)`.

### 10. Composition: full PM header

`render_pm_header` produces:

1. `render_envelope_open(invocation_id, timestamp)`
2. `render_regime_line(pm_view.active_risk_parameters)`
3. *blank*
4. `render_capital_block(...)` — built from `total_portfolio_value_usd`, `available_for_new_positions_usd`, and the per-position max-size entry from `pm_view.active_risk_parameters`.
5. *blank*
6. `render_sector_headroom_block(...)` — same composition as 04a/b.
7. *blank*
8. `render_directional_headroom_block(...)` — same as 04a/b.
9. *blank — only if options block renders*
10. `render_options_headroom_block(...)` — same as 04a/b.
11. *blank*
12. `_render_position_proximity_block(...)` — imported from `state_delivery/strategist.py`.
13. *blank*
14. `_render_sector_breakdown_block(...)` — imported from `state_delivery/strategist.py`.
15. *blank*
16. `_render_cross_constraint_impact_block(cross_constraint_impact)`
17. *blank*
18. The literal block `Guardrail validation tool available:\n  Call validate_guardrail(instrument, direction, size) to check any proposed modification.\n  Tool tracks cumulative impact across multiple checks within this invocation.` — this is a static reminder block per the design's `Guardrail validation tool available:` section. Helper `_render_validation_tool_reminder_block() -> str` returns the verbatim text.
19. *blank*
20. `_render_drawdown_context_block(pm_view)`
21. *blank — only if breaches present*
22. `_render_regime_transition_breaches_block(regime_transition_breaches, regime_label_display=...)` — re-used from 04b; when empty, omitted.
23. *blank*
24. `_render_recent_engine_actions_block(pm_view.intra_invocation_changelog, config)`
25. *blank*
26. `_render_active_regime_overrides_block(active_regime_overrides)`
27. *blank — only if correlation block renders*
28. `_render_correlation_state_block(correlation_state, pm_view.positions, config)` — returns `None` when omitted; `None` skips the slot and its preceding blank.
29. *blank — only if dependency-risk block renders*
30. `_render_dependency_risk_flag_block(dependency_risk_flag, pm_view.positions, config)` — returns `None` when omitted; `None` skips the slot and its preceding blank.
31. *blank — only if hard-blocks block renders*
32. `render_hard_blocks_block(breaching_entries=..., options_enabled=..., short_selling_enabled=..., header_label="Hard blocks (do NOT issue commands violating):")` — note the PM-specific header label per the design's PM section.
33. `render_envelope_close()`

### 11. Tests

Tests at `tests/risk_guardrails/state_delivery/test_portfolio_manager.py`:

- **Happy path — full-system 12 positions, 3 pending proposals (cross-constraint impact non-empty), 1 pre-event overlay active, correlation 0.62 / 0.70 in WARNING zone, dependency-risk-flag at 18% / 25% in NORMAL zone:** rendered output matches a fixture string line-by-line. All blocks present, none omitted.
- **Happy path — micro 3 positions, no overlays, correlation block omitted (< 3 positions threshold not met when `correlation_state_min_position_count: 4`), no engine actions, no abandoned, no breaches:** correlation block omitted entirely; dependency-risk-flag block omitted entirely; recent-engine-actions block shows `  None`; active-regime-overrides block shows `  None`; cross-constraint-impact block shows `No pending proposals` line.
- **Cross-constraint impact `flagged_rule_ids` present:** the `Flagged: ...` suffix line is appended; rules render in input order.
- **Cross-constraint impact empty per_rule:** `No pending proposals; no projected impact.` line emitted.
- **Cross-constraint impact projected_after > limit:** the `(would breach by ...%)` status replaces `(within limit)` for the affected rule.
- **Active regime overlays empty:** `  None` line emitted.
- **Active regime overlays with non-`None` `expires_at`:** the `(expires {timestamp})` suffix is appended.
- **Correlation state below position threshold:** `len(pm_view.positions) < config.correlation_state_min_position_count` → block omitted; no header line, no `(none)` line.
- **Dependency-risk-flag below position threshold:** same shape; block omitted.
- **Recent engine actions — empty:** `  None` line emitted.
- **Recent engine actions — populated:** rows render in chronological order with the documented format; the timestamp uses ISO-8601-with-Z.
- **Drawdown context — daily P/L sign:** positive P/L renders as `+X.X%`; negative as `-X.X%`; zero as `+0.0%`.
- **Drawdown context — cumulative tier present:** restrictions text matches story 04b's helper output.
- **Hard-blocks header label:** PM block uses `Hard blocks (do NOT issue commands violating):` not `Hard blocks (do NOT recommend):`.
- **Determinism:** byte-identical output across repeated calls.
- **Feature-flag closure invariants:** same as 04a/b.
- **Missing required rule:** missing `daily_drawdown_pct`, `cumulative_drawdown_pct`, `position_max_size_pct`, `net_long_pct`, `gross_exposure_pct`, or any expected sector entry raises `ValueError`.
- **Blank-line discipline:** no two consecutive blank lines; no trailing blank; each omitted block does not leave a blank-line residue.

Out of scope:
- The analyst or strategist header (stories 04a, 04b).
- Halt-mode wrapper (story 05).
- Emergency invocation header (story 06).
- The validation tool itself (story 07) — this story renders only the static `Guardrail validation tool available:` reminder block; the tool's I/O contract and cumulative-tracking mechanics live in story 07.
- Computing `cross_constraint_impact`, `correlation_state`, or `dependency_risk_flag` — those are upstream layer responsibilities; this renderer takes typed inputs.
- The PM's full input bundle (synthesizer brief, proposal pre-processor bundle, etc.) — only the guardrail state header is rendered here.

## Notes

The PM header is the most information-dense (400–800 token budget per the design). The composition uses every shared primitive plus three private helpers from this module and two imported from `state_delivery/strategist.py`. Reusing the strategist's per-position proximity and sector breakdown helpers keeps the per-position rendering identical between the two audiences — important because the strategist proposes and the PM evaluates on the same numbers.

Importing private helpers (`_render_position_proximity_block`, `_render_sector_breakdown_block`, `_render_regime_transition_breaches_block`) from `state_delivery/strategist.py` is intentional. These helpers are private because they have no public stable API; any caller-side dependence is in-process and the leading underscore is the conventional warning. If a third audience (or an external caller) needs the same helpers, promote to a `state_delivery/_helpers.py` module or to `primitives.py` at that point — not preemptively per `feedback_simplify_before_building.md`.

Per `feedback_no_inventing_component_names.md`, the typed input names mirror the design's section labels: `CrossConstraintImpact` from `Cross-constraint impact summary`, `RegimeOverride` from `Active regime overrides`, `CorrelationState` from `Correlation state`, `DependencyRiskFlag` from `Dependency risk flag`. The per-rule entry `CrossConstraintImpactPerRule` mirrors the pre-processor's `combined_set_impact.per_rule` schema element.

Per `feedback_per_producer_schema.md`, each value object is a separate Pydantic model rather than a discriminated union — keeps the per-block contract self-contained and matches the upstream-producer-per-block reality (pre-processor produces `combined_set_impact`; regime-adaptation produces overlay state; distillation produces correlation matrix; analysis layer produces dependency clustering). One producer per typed input.

The PM's drawdown context block surfaces daily P/L (signed) — distinct from the strategist's drawdown-state block which surfaces just zone status. The PM is the agent making sizing decisions under drawdown pressure; daily P/L sign drives the `+/-` narrative. The strategist is reasoning about thesis quality; just the zone/limit context suffices.

Per `feedback_avoid_numeric_anchors.md`, the `correlation_state_min_position_count` and `dependency_risk_flag_min_position_count` thresholds (default `3`) are config-tunable infrastructure thresholds, not numeric anchors for LLM reasoning. They control rendering omission, not LLM behavior.

The static `Guardrail validation tool available:` reminder block is a constant string — it tells the PM the tool exists, what to call, and that cumulative tracking is in effect. The actual tool I/O contract lives in story 07. Surfacing the reminder in the header is deliberate: the PM operates in a fresh context and needs to be reminded of every available capability.

Per `feedback_no_decision_trails.md`, the PM-specific hard-blocks header label (`do NOT issue commands violating:` rather than `do NOT recommend:`) is stated authoritatively in the renderer — no comment narrating "the analyst uses different phrasing here". The two phrasings reflect the audiences' authority levels (analyst recommends; PM issues commands) and the design specifies them; the renderer just does the right thing per audience.

Cross-feature dependency callout: The pre-processor work tree owns the `combined_set_impact` typed shape. When that work tree ships, this renderer's `CrossConstraintImpact` typed input may converge against the pre-processor's bundle schema (or stay as a separate typed shape with explicit conversion at the wiring stage). Resolve at wiring-story time, not here.

## Acceptance criteria

- [ ] `src/alphamind/risk_guardrails/state_delivery/portfolio_manager.py` exists with `render_pm_header` exposed (re-exported from `state_delivery/__init__.py`).
- [ ] `CrossConstraintImpact`, `CrossConstraintImpactPerRule`, `RegimeOverride`, `CorrelationState`, `DependencyRiskFlag` frozen Pydantic v2 models exist with the documented fields.
- [ ] The function signature matches the documented keyword-only parameter list.
- [ ] Full-system fixture (12 positions, 3 pending proposals with non-empty cross-constraint impact, 1 active overlay, correlation in WARNING, dependency-risk in NORMAL) renders to a fixture string line-by-line.
- [ ] Micro fixture (3 positions, no overlays, correlation/dependency-risk blocks omitted by threshold, no engine actions, no abandoned, empty cross-constraint impact) renders correctly with the documented `None` lines and omitted blocks.
- [ ] Cross-constraint impact `flagged_rule_ids` present → `Flagged: ...` suffix line appended; absent → no suffix.
- [ ] Cross-constraint impact empty `per_rule` → `No pending proposals; no projected impact.` line emitted.
- [ ] Cross-constraint impact `projected_after > limit` → `(would breach by ...%)` status replaces `(within limit)`.
- [ ] Active regime overlays with non-`None` `expires_at` → `(expires {timestamp})` suffix appended; with `None` → no suffix.
- [ ] Correlation block omitted when `len(pm_view.positions) < config.correlation_state_min_position_count` OR when `correlation_state is None`.
- [ ] Dependency-risk block omitted when `len(pm_view.positions) < config.dependency_risk_flag_min_position_count` OR when `dependency_risk_flag is None`.
- [ ] Drawdown daily P/L sign always rendered (`+0.0%` for zero, `+X.X%` positive, `-X.X%` negative).
- [ ] Hard-blocks block uses the PM-specific header label `Hard blocks (do NOT issue commands violating):`.
- [ ] Static `Guardrail validation tool available:` reminder block renders verbatim from the helper.
- [ ] Per-position proximity and per-sector breakdown blocks are imported from `state_delivery/strategist.py` and produce identical output to the strategist's renderer for the same inputs (regression test asserts byte-equality between the two renderers' position-proximity blocks for a shared fixture).
- [ ] Feature-flag closure invariants raise `ValueError` per 04a/b.
- [ ] Missing required rule entries raise `ValueError` with the rule_id in the message.
- [ ] No two consecutive blank lines; no trailing blank; closing `===` is immediately preceded by a non-blank line.
- [ ] Repeated calls with the same inputs produce byte-identical output.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
