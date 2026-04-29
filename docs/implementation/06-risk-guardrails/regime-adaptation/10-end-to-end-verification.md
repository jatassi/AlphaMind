---
status: in_progress
completed_date:
commit_id:
---

# 10 — End-to-end verification

## Goal

Land the integration test that drives `resolve_regime_adaptation` through the full multi-invocation lifecycle of regime classification, transition mechanics, overlay activation, breach detection, persistence, and audit log emission. Two scripted scenarios — a calm-week → crisis-week → recovery-week trajectory and a pre-event window with stress overlay overlap — verify that every primitive composes correctly when wired together by the orchestrator. This is the regime-adaptation feature's seam-test surface; nothing in this story re-tests the per-primitive behavior already covered by stories 03–09.

## Reading

- `docs/design/06-risk-guardrails/regime-adaptation.md` — every behavior under test traces to this doc
- `docs/design/06-risk-guardrails/scenario-tests.md` — the broader scenario-test discipline (this story is one regime-adaptation-specific scenario; the wider scenario test surface is owned by `scenario_tests/`)
- `docs/implementation/06-risk-guardrails/state-delivery/08-end-to-end-verification.md` — sibling pattern for an end-to-end story under a risk-guardrails feature work tree
- `09-resolve-regime-adaptation-orchestrator.md` — the orchestrator under test
- `02-package-skeleton-and-types.md` through `08-active-risk-parameter-set-assembler.md` — the primitives composed
- `src/alphamind/persistence/session.py` — the SQLite session fixture pattern this story uses

## Depends on

- 01 (README link)
- 02 (package skeleton + types)
- 03 (regime mapping)
- 04a (persistence)
- 04b (transition state machine)
- 04c (interpolation)
- 05 (event calendar loader)
- 06a (pre-event overlay activator)
- 06b (stress overlay activator)
- 07 (regime-transition breach detector)
- 08 (active risk parameter set assembler)
- 09 (orchestrator)

## Scope

In scope: integration test files plus a small `tests/risk_guardrails/regime_adaptation/conftest.py` if the per-test fixture overhead is worth centralizing. Tests at `tests/risk_guardrails/regime_adaptation/test_e2e_regime_lifecycle.py` and `tests/risk_guardrails/regime_adaptation/test_e2e_overlay_overlap.py`.

### 1. Scenario A — calm-week → crisis-week → recovery-week

A 9-invocation sequence walking through the full transition lifecycle:

| Invocation | Distillation regime | VIX | Expected guardrail regime | Expected transition_state | Expected `transition_invocations_remaining` |
|---|---|---|---|---|---|
| 1 (bootstrap) | LOW_VOL_COMPRESSION | 12 | low_vol | STABLE | 0 |
| 2 | LOW_VOL_COMPRESSION | 13 | low_vol | STABLE | 0 |
| 3 | VOL_EXPANSION | 18 | normal | TIGHTENING | 0 |
| 4 | VOL_EXPANSION | 19 | normal | STABLE | 0 |
| 5 | CRISIS_SPIKE | 42 | crisis | TIGHTENING | 0 |
| 6 | CRISIS_SPIKE | 38 | crisis | STABLE | 0 |
| 7 | VOL_EXPANSION | 25 | elevated | LOOSENING | 3 |
| 8 | VOL_EXPANSION | 24 | elevated | LOOSENING | 2 |
| 9 | VOL_EXPANSION | 23 | elevated | LOOSENING | 1 |
| 10 | VOL_EXPANSION | 22 | elevated | STABLE | 0 |

The test runs `resolve_regime_adaptation` 10 times sequentially, persisting the state via `insert_state(session, output.new_persisted_state)` after each call so the next call's `select_most_recent_state` reads the prior state. Per-invocation assertions:

- `output.runtime_dimensions_active_regime` matches the table.
- `output.active_risk_parameter_set.transition_state` and `transition_invocations_remaining` match the table.
- `output.audit_log_entries` includes a `regime_transition` event on invocations 3, 5, and 7 (fresh transitions); none on the others.
- `output.audit_log_entries` *does not* include a `regime_transition` event on invocation 10 (transition completion is a STABLE arrival, not a fresh transition).
- During invocations 7–9 (LOOSENING), `output.effective_limits["position_max_size_pct"]` equals the linearly-interpolated value between `crisis × base` and `elevated × base`. Specifically: invocation 7 → fraction 1/3, invocation 8 → 2/3, invocation 9 → 1.
- On invocation 5 (transition to crisis with high VIX), if the test seeds held positions sized at the `low_vol` cap (5%), the breach detector emits `RegimeTransitionBreach` records (since crisis multiplier on `position_max_size_pct` is 0.40 → effective limit 2%, far below 5%).

### 2. Scenario B — pre-event window with stress overlay overlap

A 4-invocation sequence with a scheduled FOMC event and an active stress alert, demonstrating overlay composition:

Setup:
- `event_calendar.yaml` populated with one FOMC event 1 day ahead.
- `DistillationCompositeState` rows seeded so `funding_stress_alert_active=True (CALIBRATED)` from invocation 1 onward; `market_liquidity` calm.
- Distillation regime stable in `VOL_EXPANSION` with VIX=20 (normal regime) throughout.

| Invocation | Description | Expected `active_overlays` | Expected `pre_event_block_new_positions` |
|---|---|---|---|
| 1 (T-3) | Two firings before event | (stress,) | False (pre_event not active yet) |
| 2 (T-2) | One firing before event | (pre_event, stress) | False |
| 3 (T-1) | Final firing before event | (pre_event, stress) | True |
| 4 (T+0, post-event) | First firing after event | (stress,) | False |

Per-invocation assertions:

- `output.runtime_dimensions_active_overlays` matches the table (sorted alphabetically).
- `output.effective_limits["sector_concentration_pct"]` reflects the appropriate multipliers: stress=0.85 always; pre_event does not touch this rule (no compounding).
- `output.effective_limits["position_max_size_pct"]` reflects: stress does not touch (no compounding); pre_event=0.80 on invocations 2, 3.
- The first appearance of `Overlay.pre_event` in `active_overlays` (invocation 2) emits an `overlay_activated` audit entry.
- The disappearance of `Overlay.pre_event` from `active_overlays` (invocation 4) emits an `overlay_deactivated` audit entry.
- Stress overlay activation on invocation 1 emits an `overlay_activated` audit entry; subsequent invocations (where stress remains active) do not re-emit.
- Invocation 3's `OverlayActivationDecision.pre_event_block_new_positions=True` flows through to the `RegimeAdaptationOutput`'s active overlay decisions surface.

### 3. Scenario C — regime-skip emergency passthrough

A 2-invocation sequence demonstrating the emergency-flag passthrough:

| Invocation | Distillation regime | VIX | `regime_skip_emergency` from distillation | Expected output |
|---|---|---|---|---|
| 1 | LOW_VOL_COMPRESSION | 12 | False | regime=low_vol, no emergency |
| 2 | CRISIS_SPIKE | 50 | True | regime=crisis, `output.regime_skip_emergency=True`, `regime_skip_emergency` audit entry emitted |

Per-invocation assertions:

- `output.regime_skip_emergency` matches the upstream flag.
- `output.audit_log_entries` includes a `regime_skip_emergency` entry on invocation 2 with the correct payload.
- A `regime_transition` audit entry is also emitted on invocation 2 (low_vol → crisis is a fresh transition, in addition to being an emergency).

### 4. Bootstrap and edge cases

Beyond the three scenarios:

- **Empty event calendar:** scenario A's first invocation runs against `EventCalendar(entries=())`; pre-event activator returns `is_active=False` throughout; no `overlay_activated` for `pre_event`.
- **Empty composite alert state:** scenario A runs against an empty `DistillationCompositeState` table; stress activator returns `is_active=False`.
- **No held positions:** scenario A's first 6 invocations run with `held_positions=()`; `regime_transition_breaches=()` always.
- **Persistence round-trip:** verify by reading back the last persisted state via `select_most_recent_state` after scenario A completes; the recovered state matches the orchestrator's last `output.new_persisted_state` field-by-field.

### 5. Test infrastructure

The test fixtures live in `tests/risk_guardrails/regime_adaptation/conftest.py`:

```python
@pytest.fixture
def in_memory_session() -> Session:
    """SQLite in-memory session with schema applied."""
    ...

@pytest.fixture
def loaded_config_micro_normal() -> LoadedConfig:
    """A LoadedConfig with main.active_profile='micro' and the shipped regimes/overlays/scheduler/etc."""
    ...

@pytest.fixture
def rule_metadata_from_shipped_registry() -> Mapping[str, RuleMetadata]:
    """RuleMetadata map built from the shipped guardrails.yaml registry."""
    ...

@pytest.fixture
def held_position_at_5pct() -> Position:
    """A synthetic NVDA long position sized at 5% of portfolio for breach-detector tests."""
    ...
```

The fixtures use the *shipped* configuration (config/regimes/, config/overlays/, config/scheduler.yaml, config/guardrails.yaml, config/profiles/, config/main.yaml) rather than synthetic configs — the goal is to verify the orchestrator works against production config, not a stripped-down test config.

The composite-alert-state fixture inserts rows into `DistillationCompositeState` directly via the session; the regime-state fixture seeds the `distillation_regime_state` table for the sequential lookback tests.

### 6. What this story does not cover

- The configuration resolver's `compose_config(...)` is not invoked here. The orchestrator returns `runtime_dimensions_active_regime` and `runtime_dimensions_active_overlays`, which would feed `compose_config(...)` in production. This story asserts against the orchestrator's outputs directly; the resolver's downstream behavior is verified by configuration-management tests.
- The state-delivery feature's renderers consume `RegimeTransitionBreach` records to produce the strategist header's `Regime-transition breaches` block. That rendering is verified by state-delivery's story 04b and is not re-tested here.
- The activity-log table does not exist yet; audit log entries are asserted as typed records, not as persisted rows. When the activity-log table ships, an integration test can extend this story to assert persistence.
- The continuous monitor's response to `regime_skip_emergency=True` (firing an emergency invocation) is the monitor's responsibility and is not tested here.
- Cross-feature integration with the proposal pre-processor or the engine's T3 enforcement is not in scope.

## Notes

This story exists to catch composition errors that per-primitive unit tests would miss — e.g., a transition state machine that produces `LOOSENING` with `transition_origin_regime=None` would slip past 04b's per-decision tests but break 09's invariant when the assembler tries to look up the origin regime's multipliers. The end-to-end test runs the orchestrator with realistic state and asserts the full output against a worked example.

The three scenarios are deliberately scoped: scenario A walks the regime ladder, scenario B walks the overlay composition, scenario C walks the emergency passthrough. Each scenario is independent; failures isolate cleanly to one orchestration path.

The shipped-config approach means a config edit (e.g., changing the `position_max_size_pct` crisis multiplier from 0.40 to 0.35) breaks the test. This is intentional — the test asserts the behavior the config encodes, so a config drift surfaces immediately. If the operator later wants to tune values, the test updates alongside the config edit per `feedback_simplify_before_building.md`.

Per `feedback_no_inventing_component_names.md`, the test scenarios use existing distillation regime labels, existing overlay names, and existing rule ids. No synthetic test-only vocabulary.

Per `feedback_avoid_numeric_anchors.md`, the test scenarios assert specific numeric values (e.g., `transition_invocations_remaining=3`, `pre_event_block_new_positions=True`), but these are spec-defined values, not LLM behavior targets. Locking them in tests catches regressions.

Per `feedback_per_producer_schema.md`, the test asserts the orchestrator's single output record (`RegimeAdaptationOutput`) per invocation. No introspection of intermediate records (the per-primitive outputs); the unit tests cover those.

The use of in-memory SQLite for the persistence read makes the test fast (no migration setup beyond `Base.metadata.create_all`) and isolated (each test gets a fresh session).

## Acceptance criteria

- [ ] `tests/risk_guardrails/regime_adaptation/test_e2e_regime_lifecycle.py` exists and runs Scenario A's 10-invocation sequence, asserting per-invocation `runtime_dimensions_active_regime`, `transition_state`, `transition_invocations_remaining`, audit log emission, and effective-limit interpolation values.
- [ ] `tests/risk_guardrails/regime_adaptation/test_e2e_overlay_overlap.py` exists and runs Scenario B's 4-invocation sequence, asserting per-invocation `active_overlays`, `pre_event_block_new_positions`, audit log emission, and effective-limit overlay multiplication.
- [ ] Scenario C's 2-invocation regime-skip-emergency sequence is covered by a test asserting the flag passthrough and the audit log entry.
- [ ] Empty event calendar and empty composite alert state cases are covered.
- [ ] Empty held positions case is covered.
- [ ] Persistence round-trip is verified at the end of Scenario A.
- [ ] `tests/risk_guardrails/regime_adaptation/conftest.py` provides the documented fixtures (`in_memory_session`, `loaded_config_micro_normal`, `rule_metadata_from_shipped_registry`, `held_position_at_5pct`).
- [ ] All tests use the *shipped* `config/regimes/`, `config/overlays/`, `config/scheduler.yaml`, `config/guardrails.yaml`, `config/profiles/`, and `config/main.yaml` rather than synthetic test configs.
- [ ] All tests are deterministic (no random inputs; mocked time).
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
