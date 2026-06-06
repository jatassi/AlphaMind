# 02 — Runtime-dimensions resolver

## Goal

Ship `resolve_runtime_dimensions(...)` — the function that produces the four `(active_regime, active_mode, active_overlays, firing_trigger)` values the configuration resolver needs to compose the per-invocation `ResolvedConfig`. The values must be observable from persistent state at fire time, BEFORE the invocation runs distillation. This unblocks story 03a's `InvocationRecord` builder, which writes these onto the row's NOT-NULL columns.

**Vocabulary reconciliation (read before implementing).** Parent decision (I) articulates the row vocabulary `"normal" | "defensive_posture" | "halted"` (the `ActiveMode` literal in `state_persistence/invocation_context/records.py`). The existing config-layer `Mode` enum (`src/alphamind/config/models/modes.py`) has only `Mode.normal` and `Mode.halt`. `RuntimeDimensions.active_mode` is typed `Mode` and is consumed by `compose_config` via `inputs.modes[runtime.active_mode]`. This story does NOT widen the `Mode` enum or change `RuntimeDimensions`'s typing. It returns `Mode.normal` or `Mode.halt`; `Mode.defensive_posture` is unreachable until a future story lands the operator-pinning mechanism + `config/modes/defensive_posture.yaml`. Story 03a's row-builder is responsible for the `Mode → ActiveMode` translation when populating the row (`Mode.halt → "halted"`, `Mode.normal → "normal"`).

## Reading

* Parent issue `ALP-431` § Pre-resolved configuration decisions (I) — `active_mode` resolution rule (the row-vocabulary intent; this story emits the config-layer `Mode` and lets story 03a translate)
* Parent issue `ALP-431` § Notes for the orchestrator — surfacing condition (iv) on Mode-resolution divergence
* `docs/design/configuration-management.md` § Composition model — the four runtime dimensions the resolver consumes
* `docs/design/06-risk-guardrails/regime-adaptation.md` — regime is sticky between invocations; the current invocation operates on the prior invocation's classified regime, then distillation produces the new label for the next invocation
* `docs/design/05-execution-layer/state-persistence.md` § Invocation records — the column shape `active_regime`, `active_mode`, `active_overlays_json` populate
* `src/alphamind/config/resolver.py` § `RuntimeDimensions` — the dataclass this story builds the producer for (typing unchanged)
* `src/alphamind/config/models/modes.py` — `Mode.normal | Mode.halt`; the two reachable return values for `_resolve_active_mode`
* `src/alphamind/config/models/regimes.py` — `Regime` enum members
* `src/alphamind/config/models/overlays.py` — `Overlay` enum members
* `src/alphamind/risk_guardrails/breach_behavior/types.py` § `HaltState` — the two-boolean halt record (`daily_halt_active`, `cumulative_full_halt_active`); only constructed when at least one halt is active, so a non-None `HaltState` means "halted"
* `src/alphamind/risk_guardrails/regime_adaptation/pre_event_activator.py` § `evaluate_pre_event_overlay` — the existing activator this story delegates to via a precomputed `OverlayActivationDecision` parameter
* `src/alphamind/risk_guardrails/regime_adaptation/stress_activator.py` § `evaluate_stress_overlay` + `fetch_composite_alert_state` — the stress activator + composite-alert fetcher (caller invokes; this story takes the resulting `OverlayActivationDecision`)
* `src/alphamind/risk_guardrails/regime_adaptation/types.py` § `OverlayActivationDecision` — the typed result this story consumes (`overlay`, `is_active`, `rationale`, `pre_event_block_new_positions`)
* `src/alphamind/execution/state_persistence/tables/invocations.py` — query target for prior `active_regime`
* `src/alphamind/execution/state_persistence/invocation_context/records.py` § `ActiveMode` — the row-vocabulary literal (story 03a's translation target, NOT this story's return type)

## Depends on

* `ALP-442` (this work tree, story 01) — package skeleton ships the `alphamind.scheduler` namespace this module lives under.

## Scope

In scope under `src/alphamind/scheduler/runtime.py`. Tests at `tests/scheduler/test_runtime.py`. No new persistence, no new config keys, no design-doc additions. **Does NOT extend the** `Mode` enum, change `RuntimeDimensions`'s typing, or add fields to `MainConfig` — all such widenings are out of scope.

### 1\. `resolve_runtime_dimensions`

```python
async def resolve_runtime_dimensions(
    session: AsyncSession,
    *,
    firing_trigger: RunType,
    halt_state: HaltState | None,
    pre_event_decision: OverlayActivationDecision,
    stress_decision: OverlayActivationDecision,
) -> RuntimeDimensions:
    """Produce the four per-invocation runtime dimensions from persistent state."""
```

The caller (story 03b's `run_invocation`) is responsible for computing the inputs:

* `halt_state` via `compute_halt_state(drawdown_state, active_risk_parameters)` (returns `None` when no halt is active; non-None means "halted").
* `pre_event_decision` via `evaluate_pre_event_overlay(now_utc=..., event_calendar=..., scheduler_config=..., pre_event_overlay=...)`.
* `stress_decision` via `evaluate_stress_overlay(...)` after `fetch_composite_alert_state(session)`.

This story does NOT load `EventCalendar`, `CompositeAlertState`, or `ActiveRiskParameterSet`. Those are the caller's responsibility (they live in `run_invocation`'s setup phase, which already loads `PipelineConfig` and has the overlays bundle on hand).

Implements the per-dimension resolution:

* `active_regime` — query the most recent successful row in `invocations` (`ORDER BY start_at DESC` with `phase2_completed_at IS NOT NULL`, `LIMIT 1`) and return its `active_regime` parsed as a `Regime` enum member. If no prior successful invocation exists (first-ever run or every prior aborted): return `Regime.normal`.
* `active_mode` — derived purely from `halt_state`:
  * `halt_state is None` → `Mode.normal`
  * `halt_state is not None` → `Mode.halt` (HaltState is constructed only when at least one halt flag is active per its own invariant)
* `active_overlays` — derived purely from the two decisions:
  * Empty tuple by default.
  * If `pre_event_decision.is_active` → include `Overlay.pre_event`.
  * If `stress_decision.is_active` → include `Overlay.stress`.
  * Order: `(pre_event, stress)` when both fire (matches the design's review-surface stability convention).
* `firing_trigger` — pass through unchanged from the input.

Returns `RuntimeDimensions(active_regime=..., active_mode=..., active_overlays=..., firing_trigger=firing_trigger)`.

### 2\. Helper module structure

Three private helpers, each independently unit-testable:

```python
async def _resolve_active_regime(session: AsyncSession) -> Regime
def _resolve_active_mode(halt_state: HaltState | None) -> Mode
def _resolve_active_overlays(
    *,
    pre_event_decision: OverlayActivationDecision,
    stress_decision: OverlayActivationDecision,
) -> tuple[Overlay, ...]
```

`_resolve_active_regime` is the only one with a DB read; the other two are pure functions of their inputs.

### Out of scope

* Invocation context assembly — story 03a (consumes this story's output).
* `compute_halt_state`, `evaluate_pre_event_overlay`, `evaluate_stress_overlay`, `fetch_composite_alert_state`, `load_event_calendar` orchestration — story 03b's `run_invocation` setup.
* Mode-enum extension / `defensive_posture` mode YAML / operator-pinning mechanism — future work; surfacing condition (iv) in the parent issue covers re-vocabulary when those land.
* Distillation-driven regime updates — owned by the distillation orchestrator; this story only reads prior state.
* `RuntimeDimensions` typing changes — out of scope.
* Overlay arithmetic / multiplier composition — owned by `compose_config` (already shipped).

## Acceptance criteria

- [ ] `resolve_runtime_dimensions` is importable from `alphamind.scheduler.runtime` and accepts the documented kwargs.
- [ ] With no rows in `invocations`, `_resolve_active_regime(session)` returns `Regime.normal`.
- [ ] With one row in `invocations` carrying `active_regime="elevated"` and `phase2_completed_at` populated, `_resolve_active_regime(session)` returns `Regime.elevated`.
- [ ] With one row carrying `phase2_completed_at IS NULL` (aborted invocation), `_resolve_active_regime(session)` skips it and returns the next-most-recent successful row's regime (or `Regime.normal` if none).
- [ ] `_resolve_active_mode(halt_state=None)` returns `Mode.normal`.
- [ ] `_resolve_active_mode(halt_state=HaltState(daily_halt_active=True, cumulative_full_halt_active=False, daily_drawdown_pct=..., daily_drawdown_limit_pct=...))` returns `Mode.halt`.
- [ ] `_resolve_active_mode(halt_state=HaltState(daily_halt_active=False, cumulative_full_halt_active=True, daily_drawdown_pct=..., daily_drawdown_limit_pct=...))` returns `Mode.halt`.
- [ ] With both decisions `is_active=False`, `_resolve_active_overlays` returns `()`.
- [ ] With `pre_event_decision.is_active=True` and `stress_decision.is_active=False`, `_resolve_active_overlays` returns `(Overlay.pre_event,)`.
- [ ] With `pre_event_decision.is_active=False` and `stress_decision.is_active=True`, `_resolve_active_overlays` returns `(Overlay.stress,)`.
- [ ] With both decisions `is_active=True`, `_resolve_active_overlays` returns `(Overlay.pre_event, Overlay.stress)` in that order.
- [ ] `resolve_runtime_dimensions(session, firing_trigger=RunType.market_hours_rolling, halt_state=None, pre_event_decision=<inactive>, stress_decision=<inactive>)` composes the three helpers and returns a `RuntimeDimensions(active_regime=Regime.normal, active_mode=Mode.normal, active_overlays=(), firing_trigger=RunType.market_hours_rolling)`.
- [ ] `tests/scheduler/test_runtime.py` covers the criteria above and passes under `uv run pytest tests/scheduler/test_runtime.py -n auto`.
- [ ] `uv run pytest -n auto` (full suite) passes — no regressions in existing tests.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` is clean.

## Verification

* `uv run pytest tests/scheduler/test_runtime.py -n auto -v` — every new test passes.
* `uv run pytest -n auto` — full suite green.
* Lint clean per CLAUDE.md.