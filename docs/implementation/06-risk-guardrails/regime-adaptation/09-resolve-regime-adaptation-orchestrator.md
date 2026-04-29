---
status: done
completed_date: 2026-04-29
commit_id: 3b8fde7
---

# 09 — Resolve regime adaptation orchestrator

## Goal

Land the top-level `resolve_regime_adaptation` function — the regime-adaptation feature's single entry point. Reads the upstream inputs (distillation regime block, held positions, event calendar, distillation composite alerts, prior persisted state, loaded config, current invocation timestamp), composes the per-story primitives into the per-invocation outputs, and returns a `RegimeAdaptationOutput` carrying the runtime dimensions for `compose_config`, the resolved `ActiveRiskParameterSet`, the regime-transition breach records, the regime-skip emergency flag passthrough, the new state to persist, and the audit log entries. Persistence and config-resolver invocation are the *caller's* responsibility (the pipeline runtime); this orchestrator is pure-with-DB-read.

## Reading

- `docs/design/06-risk-guardrails/regime-adaptation.md` — the full feature spec; this story's orchestrator implements the design end-to-end
- `docs/design/06-risk-guardrails/regime-adaptation.md` § Transition logging — the orchestrator emits audit log entries for every transition
- `docs/design/configuration-management.md` § Composition model — the orchestrator's `RegimeAdaptationOutput` feeds `compose_config(...)` via the runtime dimensions
- `02-package-skeleton-and-types.md` — `RegimeAdaptationOutput`, `RegimeAdaptationState`, `RegimeAdaptationAuditEntry`
- `03-distillation-to-guardrail-regime-mapping.md` — `map_distillation_to_guardrail_regime`
- `04a-regime-adaptation-state-persistence.md` — `select_most_recent_state` and `insert_state` (the orchestrator reads via the first; the *caller* writes via the second)
- `04b-transition-state-machine.md` — `compute_next_transition`
- `04c-loosening-interpolation.md` — `resolve_active_multipliers`
- `05-event-calendar-loader.md` — `load_event_calendar`, `select_events_within_window`, `warn_on_stale_calendar`
- `06a-pre-event-overlay-activator.md` — `evaluate_pre_event_overlay`
- `06b-stress-overlay-activator.md` — `evaluate_stress_overlay`, `fetch_composite_alert_state`
- `07-regime-transition-breach-detector.md` — `detect_regime_transition_breaches`
- `08-active-risk-parameter-set-assembler.md` — `assemble_active_risk_parameter_set`

## Depends on

- 02 (package skeleton + types)
- 03 (regime mapping)
- 04a (persistence — only the *read*; the *write* happens in the caller)
- 04b (transition state machine)
- 04c (interpolation)
- 05 (event calendar loader)
- 06a (pre-event overlay activator)
- 06b (stress overlay activator)
- 07 (regime-transition breach detector)
- 08 (active risk parameter set assembler)

## Scope

In scope, all under `src/alphamind/risk_guardrails/regime_adaptation/orchestrator.py`. Tests at `tests/risk_guardrails/regime_adaptation/test_orchestrator.py`.

### 1. Public function

```python
def resolve_regime_adaptation(
    *,
    invocation_id: str,
    now_utc: datetime,
    distillation_regime_label: DistillationRegimeLabel,
    distillation_vix_level: float,
    distillation_regime_skip_emergency: bool,
    held_positions: tuple[Position, ...],
    risk_budget: RiskBudgetConsumption,
    prior_parameter_set: ActiveRiskParameterSet | None,
    event_calendar: EventCalendar,
    composite_alert_state: CompositeAlertState,
    loaded_config: LoadedConfig,
    rule_metadata: Mapping[str, RuleMetadata],
    session: Session,
) -> RegimeAdaptationOutput
```

The `session` argument is the only DB-read seam — used to read the prior `RegimeAdaptationState` row via `select_most_recent_state`. Held positions, risk budget, prior parameter set, event calendar, composite alert state, and loaded config are *passed in* by the pipeline runtime; the orchestrator does not fetch them from the database itself. This keeps the orchestrator's I/O surface narrow and testable.

### 2. Orchestration steps

In execution order:

1. **Read prior regime adaptation state.** `prior_state = select_most_recent_state(session)`. May be `None` on bootstrap.

2. **Map distillation regime to guardrail regime.** Build `vix_thresholds = VixBoundaryThresholds.from_regime_classification(loaded_config.distillation.regime_classification)`. Call `new_regime = map_distillation_to_guardrail_regime(distillation_label=..., vix_level=..., vix_thresholds=...)`.

3. **Compute the next transition decision.** `next_transition = compute_next_transition(new_regime=new_regime, invocation_id=invocation_id, prior_state=prior_state)`.

4. **Resolve active overlays.**
   - `pre_event_decision = evaluate_pre_event_overlay(now_utc=..., event_calendar=..., scheduler_config=loaded_config.scheduler, pre_event_overlay=loaded_config.overlays[Overlay.pre_event])`
   - `stress_decision = evaluate_stress_overlay(funding_stress_alert_active=composite_alert_state.funding_stress_alert_active, ..., stress_overlay=loaded_config.overlays[Overlay.stress])`
   - `active_overlays = tuple(d.overlay for d in (pre_event_decision, stress_decision) if d.is_active)` — typed as `tuple[Overlay, ...]`. Sort/conversion to the portfolio-state shape happens later via the canonical `overlays_to_strings` helper (story 02 § 2m); the orchestrator passes the enum-typed tuple to `assemble_active_risk_parameter_set` (story 08), which calls `overlays_to_strings` internally. For the persisted `RegimeAdaptationState.active_overlays` (which is also typed `tuple[Overlay, ...]`), the orchestrator stores the enum-typed tuple sorted alphabetically by `Overlay.value` via `tuple(sorted((d.overlay for d in ... if d.is_active), key=lambda o: o.value))` — keeps the persisted field deterministic without crossing the enum→string boundary.

5. **Resolve interpolated multipliers.**
   - `active_regime_multipliers = loaded_config.regimes[next_transition.active_regime].multipliers`
   - `transition_origin_multipliers = loaded_config.regimes[next_transition.transition_origin_regime].multipliers if next_transition.transition_origin_regime is not None else None`
   - `interpolated_multipliers = resolve_active_multipliers(transition_state=next_transition.transition_state, transition_invocations_remaining=next_transition.transition_invocations_remaining, active_regime_multipliers=active_regime_multipliers, transition_origin_multipliers=transition_origin_multipliers)`

6. **Compose overlay multipliers.** For each rule_id in `interpolated_multipliers`, compute `overlay_product = product of (overlay_config.multipliers.get(rule_id, 1.0) for overlay in active_overlays)`. Build `overlay_multipliers_composed: dict[str, float]` keyed on every rule_id.

7. **Resolve effective limits.** For each rule_id in `interpolated_multipliers`:
   - `base_value = loaded_config.profiles[loaded_config.main.active_profile].rule_values[rule_id]`
   - `effective_value = base_value * interpolated_multipliers[rule_id] * overlay_multipliers_composed[rule_id]`
   - Build `effective_limits: dict[str, float]`.

8. **Detect regime-transition breaches.** Call `detect_regime_transition_breaches(held_positions=..., risk_budget=..., new_effective_limits=effective_limits, transition_state=next_transition.transition_state, rule_metadata=rule_metadata)`.

9. **Assemble the active risk parameter set.** Call `assemble_active_risk_parameter_set(next_transition=next_transition, base_profile_rule_values=base_values, active_regime_multipliers=active_regime_multipliers, interpolated_multipliers=interpolated_multipliers, overlay_multipliers_composed=overlay_multipliers_composed, active_overlays=active_overlays, rule_metadata=rule_metadata, prior_parameter_set=prior_parameter_set)`.

10. **Construct the new persisted state.** Build `RegimeAdaptationState` with all fields wired from `next_transition`, `active_overlays`, `distillation_regime_label.value`, `distillation_vix_level`, `distillation_regime_skip_emergency`, `as_of=now_utc.isoformat()`, `invocation_id=invocation_id`. The typed-record invariants (story 02) catch malformed states.

11. **Emit audit log entries.** See §3 for the full enumeration.

12. **Return.** Build and return the `RegimeAdaptationOutput`.

### 3. Audit log emission rules

The orchestrator returns `audit_log_entries: tuple[RegimeAdaptationAuditEntry, ...]` covering the events worth tracing through the activity log:

- **`regime_transition`** — emitted whenever the active regime *changed* this invocation: `prior_state is not None AND next_transition.active_regime != prior_state.active_regime`. The predicate fires for fresh transitions from `STABLE`, for tightening that overrides an in-flight loosening (prior `LOOSENING`, new `TIGHTENING`), and for re-issued loosening starts (prior `LOOSENING`, new origin/destination). It does NOT fire on bootstrap (`prior_state is None`), on stable continuation (regime unchanged), or on loosening countdowns (regime unchanged across the 3-invocation interpolation). Payload: `{prior_regime, new_regime, direction, prior_multipliers_snapshot, new_multipliers_snapshot, transition_invocations_remaining}`. The `direction` is `tightening` or `loosening` derived from `regime_ladder_index` comparison.
- **`overlay_activated`** — emitted for each overlay whose `is_active` flipped from `False` (in prior state's `active_overlays`) to `True` in this invocation. One entry per newly-activated overlay. Payload: `{overlay, rationale}` from the `OverlayActivationDecision`.
- **`overlay_deactivated`** — emitted for each overlay whose `is_active` flipped from `True` (in prior state) to `False`. Payload: `{overlay}` (no rationale needed for deactivation; absence is the reason).
- **`regime_skip_emergency`** — emitted when `distillation_regime_skip_emergency is True`. Payload: `{distillation_regime_label, distillation_vix_level, prior_distillation_regime_label}`. Surfaces the upstream emergency flag for activity-log review.
- **`stale_event_calendar`** — emitted when `warn_on_stale_calendar(...)` returns `is_stale=True`. Payload: `{latest_event_timestamp_utc, days_until_latest}`.

The audit entries are returned for the caller to commit alongside the rest of the invocation's writes; the orchestrator does not write them itself (the activity-log table doesn't exist yet per the cross-feature gates note in the state-delivery work tree).

### 4. Bootstrap and missing-data handling

- **No prior persisted state.** `prior_state=None` → `next_transition` is bootstrap-stable per story 04b. No `regime_transition` audit event (since the transition state did not change — bootstrap = stable). The new persisted state captures the bootstrap regime.
- **No prior parameter set** (`prior_parameter_set=None`) → the assembled `ActiveRiskParameterSet.parameter_change_flag=False` per story 08.
- **No event calendar entries** → empty tuple flows through; pre-event activator returns `is_active=False`. No `stale_event_calendar` audit event (empty calendar is `is_stale=False` per story 05).
- **Empty composite-alert state** (no rows in `DistillationCompositeState`) → `composite_alert_state` has `False`/`UNAVAILABLE` for both kinds; stress activator returns `is_active=False`.
- **No held positions** → breach detector returns `()`.

### 5. Tests

Tests at `tests/risk_guardrails/regime_adaptation/test_orchestrator.py`. Use a SQLite session fixture for the persistence read; mock or fixture the rest.

- **Bootstrap path:** no prior persisted state, `LOW_VOL_COMPRESSION` from distillation, `vix_level=10`, no overlays, no positions. Returns `RegimeAdaptationOutput` with `runtime_dimensions_active_regime=Regime.low_vol`, `runtime_dimensions_active_overlays=()`, `effective_limits` matching the profile base × low_vol multipliers, `regime_transition_breaches=()`, `regime_skip_emergency=False`, `audit_log_entries=()`. The new persisted state has `transition_state=STABLE`, `prior_regime=None`, `active_regime=Regime.low_vol`.
- **Stable continuation:** prior persisted state STABLE in `normal`; distillation reports `VOL_EXPANSION` with `vix_level=18` (mid-normal-band). Returns STABLE in `normal`, no breaches, no transition audit event.
- **Tightening transition:** prior STABLE in `normal`; new `CRISIS_SPIKE` with `vix_level=40`. Returns `next_transition=TIGHTENING`, `active_regime=crisis`. The `regime_transition` audit entry includes `direction="tightening"`, `prior_regime="normal"`, `new_regime="crisis"`.
- **Tightening transition with breaches:** same as above plus held positions whose sizes exceed the new `crisis` regime's tighter limits. Returns one or more `RegimeTransitionBreach` records.
- **Loosening transition (first invocation):** prior STABLE in `crisis`; new `VOL_EXPANSION` with `vix_level=18`. Returns LOOSENING, `transition_invocations_remaining=3`, `transition_origin_regime=crisis`. Effective limits are interpolated one-third toward `normal`. `regime_transition` audit entry with `direction="loosening"`.
- **Loosening countdown:** prior LOOSENING `remaining=3, origin=crisis, active=normal`; same `VOL_EXPANSION` distillation reading. Returns LOOSENING `remaining=2`. Effective limits two-thirds toward `normal`. No new `regime_transition` audit entry (the transition was already emitted).
- **Loosening complete:** prior LOOSENING `remaining=1`; same reading. Returns STABLE. Effective limits at full `normal` multipliers. No `regime_transition` audit entry (transition completion is a STABLE-arrival, not a fresh transition).
- **Tightening overrides loosening:** prior LOOSENING in `normal` from `elevated`; new `CRISIS_SPIKE`. Returns TIGHTENING in `crisis`. The loosening interpolation is abandoned. `regime_transition` audit entry with `direction="tightening"`.
- **Pre-event overlay activates:** event calendar has an FOMC event at the right time; pre-event activator returns `is_active=True`. The output's `runtime_dimensions_active_overlays` includes `Overlay.pre_event`. The effective limits for `position_max_size_pct` reflect the 0.80 overlay multiplier. The `overlay_activated` audit entry is emitted.
- **Stress overlay activates:** composite alert state shows `funding_stress_alert_active=True (CALIBRATED)`. The output includes `Overlay.stress`. The effective limits for sector / directional / gross are tightened by 0.85. The `overlay_activated` audit entry is emitted.
- **Both overlays active:** event calendar + composite alert state. The output includes `(pre_event, stress)` sorted alphabetically. Both `overlay_activated` audit entries are emitted.
- **Overlay deactivates:** prior persisted state has `active_overlays=(stress,)`; current invocation's stress activator returns `is_active=False`. The `overlay_deactivated` audit entry is emitted.
- **Regime-skip emergency passthrough:** `distillation_regime_skip_emergency=True`. The output's `regime_skip_emergency=True`. The `regime_skip_emergency` audit entry is emitted with the payload populated.
- **Stale event calendar:** event calendar's latest event 5 days ahead → `stale_event_calendar` audit entry emitted. Latest event 30 days ahead → no entry.
- **Empty held positions:** breach detector returns `()`; output's `regime_transition_breaches=()`.
- **No composite alert rows:** stress activator returns `is_active=False`; no `overlay_activated` event for stress.
- **Determinism (with mocked time):** identical inputs produce identical outputs.

Out of scope:

- Persisting the new state — the caller calls `insert_state(session, output.new_persisted_state)`.
- Calling `compose_config(...)` — the caller does that with the orchestrator's runtime-dimensions fields.
- Persisting the audit log entries — the caller commits them alongside the activity log when the table ships.
- Triggering the emergency invocation — the continuous monitor consumes `regime_skip_emergency` and decides whether to fire the emergency invocation; the orchestrator just surfaces the flag.
- Handling DB read failures (session errors) — the orchestrator does not catch exceptions from `select_most_recent_state`; the caller's session-management layer is responsible.
- A retry path for partial state corruption — fail-closed per the project's existing failure-handling discipline.

## Notes

The orchestrator's I/O footprint is small: one DB read (`select_most_recent_state`). All other inputs are passed in by the caller. This keeps the function unit-testable with simple fixture inputs and preserves the project's "pure-function-where-possible" discipline.

**Output is regime-resolved, not tier-overridden.** The returned `ActiveRiskParameterSet` reflects regime multipliers, loosening interpolation, and overlay multipliers — but does NOT yet reflect cumulative-drawdown progressive-tier overrides. When cumulative drawdown is at tier 1 or tier 2 (per `breach_behavior/04b-cumulative-drawdown-tier.md`), `position_max_size_pct` and `gross_exposure_pct` are further-tightened by the tier override, and an extra `cumulative_drawdown_tier_{N}` tag is appended to `active_overlays`. That refinement is the responsibility of the Phase 1 enforcement-layer composition step (a future feature; see `state-delivery.md § Portfolio state ingestion payload` and `breach_behavior/04b`'s integration-site note). **The orchestrator does not call `apply_progressive_tier_overrides` itself** — keeping regime-adaptation focused on regime/overlay resolution and breach-behavior focused on breach response. Until the Phase 1 composition feature ships, downstream consumers needing the fully-composed parameter set (e.g., scenario tests, breach-behavior E2E story 08) compose the two function calls inline. See `docs/project-tracker.md § Backlog` for the tracking entry on the missing wiring.

The `audit_log_entries` field is a typed-record tuple, not a database write. The caller (the pipeline runtime, which today does not exist as a single home; the activity-log table is forthcoming under the execution layer) decides when to persist. Until persistence wires up, the orchestrator's audit entries are runtime-visible but not persisted; the test suite asserts the entries are emitted with the right shape.

Per `feedback_no_inventing_component_names.md`, `resolve_regime_adaptation` is the only new function name; it mirrors `compose_config` in shape and naming convention. All other names are existing primitives.

Per `feedback_simplify_before_building.md`, the orchestrator does not implement caching, retry, fallback paths, or partial degradation. A single failure path: any underlying primitive raising an exception bubbles up; the caller's invocation-level error handling decides what to do. No silent recovery.

Per `feedback_avoid_numeric_anchors.md`, the orchestrator has no numeric thresholds; all numbers come from the loaded config or the underlying primitives.

Per `feedback_per_producer_schema.md`, the orchestrator has one producer (this function) and emits one record (`RegimeAdaptationOutput`). The output bundles multiple sub-records but does not conflate them into a discriminated union — each sub-field has a distinct schema.

The audit-log emission logic is the orchestrator's most behavior-rich part — five event kinds with five different "should I emit?" predicates. Each predicate is testable in isolation (the test suite covers each event kind independently); the orchestrator's role is to aggregate them.

The `direction` field on `regime_transition` audit entries (`tightening` / `loosening`) is derived from `regime_ladder_index` comparison: `index(new) > index(prior)` → tightening; `index(new) < index(prior)` → loosening. Reuses the helper from story 04b.

Per the project's failure-handling discipline (per `docs/design/llm-agent-failure-handling.md` and the `feedback_no_decision_trails.md` principle), the orchestrator is fail-closed at the invocation boundary: any exception aborts the invocation; no degraded mode where the regime-adaptation output is partial.

The 5-event audit surface is a deliberate choice — each event reflects a state change worth a feedback-loop dashboard line. The activity-log table will ship with these event kinds in its CHECK constraint vocabulary; this story documents them so the future activity-log schema includes them.

## Acceptance criteria

- [ ] `src/alphamind/risk_guardrails/regime_adaptation/orchestrator.py` exists and defines `resolve_regime_adaptation`.
- [ ] `resolve_regime_adaptation` is re-exported from `src/alphamind/risk_guardrails/regime_adaptation/__init__.py`.
- [ ] Bootstrap path returns `STABLE`, `prior_regime=None`, no audit entries, and the persisted state captures the new regime.
- [ ] Stable continuation does not emit a `regime_transition` audit entry.
- [ ] Tightening transition emits a `regime_transition` audit entry with `direction="tightening"`.
- [ ] Loosening transition emits a `regime_transition` audit entry with `direction="loosening"`, and the orchestrator returns `transition_invocations_remaining=3` on the first invocation.
- [ ] Loosening countdown decrements `transition_invocations_remaining` and does not emit a fresh `regime_transition` audit entry.
- [ ] Loosening completion (after 3 invocations) returns STABLE with no audit entry.
- [ ] Tightening overrides loosening: prior LOOSENING + new tightening returns TIGHTENING in the new regime AND emits a fresh `regime_transition` audit entry with `direction="tightening"` (the override is a regime change, so the audit predicate fires).
- [ ] Pre-event overlay activation surfaces in `runtime_dimensions_active_overlays`, in `effective_limits` (multiplied by overlay multipliers), and emits `overlay_activated`.
- [ ] Stress overlay activation surfaces similarly with the appropriate multipliers.
- [ ] Both overlays active produce sorted-alphabetically `runtime_dimensions_active_overlays` and both `overlay_activated` audit entries.
- [ ] Overlay deactivation emits `overlay_deactivated`.
- [ ] `regime_skip_emergency` flag flows through to the output and emits a `regime_skip_emergency` audit entry when true.
- [ ] Stale event calendar (under 7 days remaining) emits `stale_event_calendar`; non-stale does not.
- [ ] Held positions exceeding the new effective limits produce `RegimeTransitionBreach` records on tightening transitions.
- [ ] No held positions returns `regime_transition_breaches=()`.
- [ ] The orchestrator is deterministic given the same inputs (no internal randomness).
- [ ] The orchestrator only reads the database (via `select_most_recent_state`); no other I/O.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
