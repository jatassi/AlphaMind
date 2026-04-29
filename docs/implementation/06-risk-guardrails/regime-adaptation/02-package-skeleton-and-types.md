---
status: not_started
completed_date:
commit_id:
---

# 02 — Package skeleton and typed records

## Goal

Stand up the Python package layout for regime adaptation under `src/alphamind/risk_guardrails/regime_adaptation/` and land the typed value objects every downstream story consumes — the persisted `RegimeAdaptationState` shape, the orchestrator output `RegimeAdaptationOutput` shape, the `RegimeTransitionBreach` record the breach detector emits, the `EventCalendarEntry` / `EventCalendar` shapes the event-calendar loader emits, and the `OverlayActivationDecision` shape the per-overlay activators emit. Empty submodules with import-only smoke test plus the typed-record file. All subsequent stories drop function modules into this skeleton.

## Reading

- `docs/design/06-risk-guardrails/regime-adaptation.md` — the full feature spec; all four regimes, the multiplier table, transition mechanics (immediate tightening, gradual loosening over 3 invocations), pre-event tightening overlay, stress overlay, and position handling on tightening live here
- `docs/design/06-risk-guardrails/README.md` — placement of regime-adaptation within the four-tier guardrail enforcement model
- `docs/design/configuration-management.md` § Composition model — regime, overlay, and run-type bundles compose into `ResolvedConfig`; the regime-adaptation feature feeds the runtime dimensions consumed by `compose_config(...)`
- `docs/design/01-data-layer/internal/portfolio-state.md` § 4d — the `ActiveRiskParameterSet` typed record this feature populates; the persistence story (04a) reads back the shape this story locks
- `src/alphamind/portfolio_state/records/capital.py` — existing `RegimeLabel`, `RegimeTransitionState`, `ActiveRiskParameterSet`, `ActiveRiskParameterEntry`, `RiskZone`, `DrawdownTier` types; this story reuses those, does not redefine them
- `src/alphamind/config/models/regimes.py` — `Regime` StrEnum (`low_vol`, `normal`, `elevated`, `crisis`) and `RegimeConfig` Pydantic model
- `src/alphamind/config/models/overlays.py` — `Overlay` StrEnum (`pre_event`, `stress`), `EventType`, `StressTrigger`, `PreEventOverlay`, `StressOverlay`
- `src/alphamind/distillation/regime.py` — `RegimeLabel` (distillation's four labels: `LOW_VOL_COMPRESSION`, `VOL_EXPANSION`, `CRISIS_SPIKE`, `VOL_NORMALIZATION`), `RegimeRefreshResult`, `REGIME_BLOCK_ID`. Distillation's labels are *not* the same enum as the guardrail-side `Regime`; story 03 lands the mapping
- `src/alphamind/risk_guardrails/regime_adaptation/__init__.py` — the (currently empty) directory this story populates
- `src/alphamind/portfolio_state/__init__.py` — sibling reference for the `<feature>Config` Pydantic + YAML loader pattern (this story does not land a YAML config because the regime-adaptation feature has no per-feature operator-tunable knobs beyond what `regimes/`, `overlays/`, and `event_calendar.yaml` already own; left as a Notes pointer for future operators)
- `docs/implementation/06-risk-guardrails/state-delivery/02-package-skeleton-and-config.md` — sibling pattern for `risk_guardrails/<feature>/` package layout

## Depends on

None within this work tree (story 01 is README-only and runs in parallel).

## Scope

In scope:

### 1. Package layout

- Top-level package at `src/alphamind/risk_guardrails/regime_adaptation/` with submodules organized by concern (per-component depth per `feedback_scaffold_per_component_depth.md`). Empty `__init__.py` files for the submodules subsequent stories populate; the top-level `__init__.py` re-exports the typed records from `types.py`:

  ```
  src/alphamind/risk_guardrails/regime_adaptation/
      __init__.py
      types.py                  # this story — typed value objects
      regime_mapping.py         # 03 — distillation→guardrail label mapping
      persistence.py            # 04a — SQLAlchemy table + repository
      transition_machine.py     # 04b — pure transition-state machine
      interpolation.py          # 04c — loosening linear interpolation
      event_calendar.py         # 05 — event calendar loader + lookup
      pre_event_activator.py    # 06a — pre-event overlay activation
      stress_activator.py       # 06b — stress overlay activation
      breach_detector.py        # 07 — regime-transition breach detection
      parameter_set.py          # 08 — ActiveRiskParameterSet assembler
      orchestrator.py           # 09 — top-level entry point
  ```

- Test mirror at `tests/risk_guardrails/regime_adaptation/` with `__init__.py` only; subsequent stories populate per-module test files.

### 2. Typed records — `types.py`

All records live in `src/alphamind/risk_guardrails/regime_adaptation/types.py`. All are stdlib `dataclass(frozen=True, slots=True)` unless they wrap a Pydantic model from `portfolio_state` or `config`.

#### 2a. `RegimeAdaptationState` — persisted snapshot

```python
@dataclass(frozen=True, slots=True)
class RegimeAdaptationState:
    """One invocation's regime-adaptation state, persisted forward-only."""
    as_of: str                               # UTC ISO datetime, primary key
    invocation_id: str                       # the invocation that produced this row
    active_regime: Regime                    # the four-tier guardrail regime
    prior_regime: Regime | None              # None on bootstrap; else the last persisted regime
    transition_state: RegimeTransitionState  # STABLE | TIGHTENING | LOOSENING
    transition_invocations_remaining: int    # 0 for STABLE/TIGHTENING; 1..3 for LOOSENING
    transition_started_invocation_id: str | None  # invocation that began the current LOOSENING; None for STABLE/TIGHTENING
    transition_origin_regime: Regime | None  # regime we are loosening *from*; None for STABLE/TIGHTENING
    active_overlays: tuple[Overlay, ...]     # alphabetical-sorted tuple of overlay names active for this invocation
    distillation_regime_label: str           # passthrough of the distillation block's `regime_label`
    distillation_vix_level: float            # passthrough; supports the regime mapping audit
    regime_skip_emergency: bool              # passthrough of the distillation block's `regime_skip_emergency` flag
```

Field rationale:
- `active_regime` is the guardrail-side four-tier label the resolver consumes; the distillation label is recorded separately for audit.
- `transition_started_invocation_id` and `transition_origin_regime` are populated only during LOOSENING; they let the interpolation primitive (04c) reconstruct the linear interpolation state from a single persisted row.
- `regime_skip_emergency` is mirrored from the distillation block so the orchestrator does not need to re-read the block when persisting this state record.

#### 2b. `RegimeAdaptationOutput` — orchestrator return value

```python
@dataclass(frozen=True, slots=True)
class RegimeAdaptationOutput:
    """The orchestrator's per-invocation output bundle."""
    runtime_dimensions_active_regime: Regime           # feeds compose_config()
    runtime_dimensions_active_overlays: tuple[Overlay, ...]  # feeds compose_config()
    effective_limits: Mapping[str, float]              # post-interpolation, post-overlay rule_id → limit
    active_risk_parameter_set: ActiveRiskParameterSet  # for portfolio_state §4d
    regime_transition_breaches: tuple[RegimeTransitionBreach, ...]
    regime_skip_emergency: bool                        # passthrough; surfaced for the continuous monitor
    new_persisted_state: RegimeAdaptationState         # the orchestrator caller persists this
    audit_log_entries: tuple[RegimeAdaptationAuditEntry, ...]  # 0+ entries the caller appends to activity_log
```

The orchestrator does not perform persistence side-effects itself; it returns the new state and the audit log entries for the pipeline runtime to commit alongside the rest of the invocation's writes.

#### 2c. `RegimeTransitionBreach` — per-position breach record

```python
@dataclass(frozen=True, slots=True)
class RegimeTransitionBreach:
    """A held position now exceeding a tightened regime limit."""
    position_id: str
    rule_id: str                             # e.g., "position_max_size_pct"
    rule_label: str                          # e.g., "Per-position max size"
    current_value: float                     # the position's current contribution to the rule
    new_limit_value: float                   # the post-tightening limit
    overage: float                           # current_value - new_limit_value (always > 0)
    unit: str                                # mirrors RiskBudgetEntry.unit (e.g., "% of portfolio")
```

Consumed by state-delivery's strategist guardrail state header (`Regime-transition breaches` block) and PM guardrail state header (same block).

#### 2d. `EventCalendarEntry` and `EventCalendar`

```python
@dataclass(frozen=True, slots=True)
class EventCalendarEntry:
    """One scheduled high-impact catalyst."""
    event_type: EventType                    # fomc | cpi | ppi | pce | nfp | earnings
    event_timestamp_utc: datetime            # tz-aware UTC datetime of the event itself
    label: str                               # human-readable identifier (e.g., "FOMC June 2026")

@dataclass(frozen=True, slots=True)
class EventCalendar:
    """The full operator-curated calendar."""
    entries: tuple[EventCalendarEntry, ...]
```

The calendar is intentionally minimal — events have a type, a timestamp, and a label. Earnings clusters are represented as a single `earnings` entry per cluster (the operator decides cluster boundaries).

#### 2e. `OverlayActivationDecision`

```python
@dataclass(frozen=True, slots=True)
class OverlayActivationDecision:
    """One overlay's per-invocation activation result."""
    overlay: Overlay                         # pre_event | stress
    is_active: bool
    rationale: str                           # human-readable activation reason; empty when is_active is False
    pre_event_block_new_positions: bool      # True only on the final invocation before a pre-event activation; False otherwise
```

The `pre_event_block_new_positions` field is overlay-specific; it is `False` for the stress overlay regardless. Co-located on the same record so the assembler (08) does not need to introspect the overlay enum to decide which auxiliary fields to surface.

#### 2f. `RegimeAdaptationAuditEntry`

```python
@dataclass(frozen=True, slots=True)
class RegimeAdaptationAuditEntry:
    """An activity-log entry the orchestrator emits for review-surface visibility."""
    event_kind: str                          # "regime_transition" | "overlay_activated" | "overlay_deactivated" | "regime_skip_emergency"
    payload: Mapping[str, object]            # event-kind-specific payload; serialized to JSON downstream
```

Activity-log persistence is not in scope for this work tree (the activity-log table is forthcoming under the execution layer per the state-delivery cross-feature gates note); the orchestrator returns these entries as typed records and the caller decides when persistence wires up.

### 3. Re-exports

`src/alphamind/risk_guardrails/regime_adaptation/__init__.py` re-exports every typed record from `types.py`:

```python
from alphamind.risk_guardrails.regime_adaptation.types import (
    EventCalendar,
    EventCalendarEntry,
    OverlayActivationDecision,
    RegimeAdaptationAuditEntry,
    RegimeAdaptationOutput,
    RegimeAdaptationState,
    RegimeTransitionBreach,
)
```

Subsequent stories add their own re-exports.

### 4. Import smoke test

`tests/risk_guardrails/regime_adaptation/test_imports.py` imports `alphamind.risk_guardrails.regime_adaptation` and the `types` submodule and asserts every documented public symbol exists.

### 5. Tests for the typed records

`tests/risk_guardrails/regime_adaptation/test_types.py`:
- `RegimeAdaptationState` is hashable when constructed with valid types (frozen+slots dataclass guarantees this); two equal-by-field instances are equal and hash-equal.
- `RegimeAdaptationState` rejects construction (via `__post_init__` or `__init__` invariant check) when `transition_state == STABLE` and `transition_invocations_remaining != 0` (mirrors the `ActiveRiskParameterSet` invariant in `portfolio_state.records.capital`); raises `ValueError` with both fields named in the message.
- `RegimeAdaptationState` rejects construction when `transition_state == LOOSENING` and `transition_started_invocation_id is None` (the field is mandatory during loosening); raises `ValueError` naming the field.
- `RegimeAdaptationState` rejects construction when `transition_state == LOOSENING` and `transition_origin_regime is None`; raises `ValueError` naming the field.
- `RegimeTransitionBreach` rejects construction when `overage` is not strictly positive (i.e., `current_value <= new_limit_value`); raises `ValueError` naming the rule_id and the computed overage.
- `RegimeTransitionBreach` rejects construction when `overage` is not equal to `current_value - new_limit_value` (within `1e-9` absolute tolerance); raises `ValueError`.
- `EventCalendarEntry.event_timestamp_utc` rejects naive datetimes; raises `ValueError` mentioning the field name.
- `OverlayActivationDecision` rejects `pre_event_block_new_positions=True` when `overlay != Overlay.pre_event`; raises `ValueError`.
- `OverlayActivationDecision` rejects `pre_event_block_new_positions=True` when `overlay == Overlay.pre_event` and `is_active is False`; raises `ValueError` (cannot block on an inactive overlay).

Out of scope:

- The actual function modules (`regime_mapping.py`, `persistence.py`, `transition_machine.py`, `interpolation.py`, `event_calendar.py`, `pre_event_activator.py`, `stress_activator.py`, `breach_detector.py`, `parameter_set.py`, `orchestrator.py`) — those are stories 03 through 09. Each is empty in this story.
- A `RegimeAdaptationConfig` Pydantic + YAML loader. The regime-adaptation feature has no per-feature operator-tunable knobs that are not already owned by `regimes/`, `overlays/`, or the event-calendar YAML (story 05). If a future operator-tunable knob emerges (e.g., loosening interpolation curve shape, breach-detection tolerance), it lands as a follow-up — not preemptively.
- The `event_calendar.yaml` schema and loader — story 05.
- Any persistence operations (read/write of `RegimeAdaptationState` rows) — story 04a.
- Any pure-function logic — stories 03–09.

## Notes

`src/alphamind/risk_guardrails/regime_adaptation/` is the canonical placement; the `risk_guardrails/` parent package already exists with empty `__init__.py` files for the six feature siblings.

The `regime_adaptation/` layout uses per-file flat depth rather than subdirectories. Each module is a single-file concern; promoting any to a subdirectory in a follow-up does not change import paths if the public API is re-exported through `__init__.py`.

Per `feedback_simplify_before_building.md`, this story does not introduce a per-feature YAML config. The regime, overlay, and event-calendar inputs already own all operator-tunable surfaces. Resist the urge to add a `regime_adaptation.yaml` preemptively.

Per `feedback_no_inventing_component_names.md`, the typed-record names use the design-doc terminology directly: "regime adaptation state", "regime-transition breach", "event calendar entry", "overlay activation decision". No new component names introduced.

Per `feedback_per_producer_schema.md`, each typed record is one record (not a discriminated union); the orchestrator output bundles records rather than conflating them. `OverlayActivationDecision` carries one overlay-specific field (`pre_event_block_new_positions`); the stress overlay's activation decision uses the same record with the field set to `False`. The single-record shape across overlays keeps the consumer (08) uniform; the invariant tests in this story prevent meaningless combinations.

Per `feedback_avoid_numeric_anchors.md`, this story introduces no numeric thresholds. The transition policy values (loosening over 3 invocations, pre-event window of 2 invocations) live in `regimes/*.yaml` and `overlays/pre-event.yaml` — not in the typed records.

The `RegimeAdaptationState.transition_started_invocation_id` and `transition_origin_regime` fields are populated only during `LOOSENING`. The invariant tests catch the case where a caller forgets to populate them; the interpolation primitive (04c) trusts the invariants.

The `audit_log_entries: tuple[RegimeAdaptationAuditEntry, ...]` field on `RegimeAdaptationOutput` is the seam between this work tree and the execution layer's activity-log table. Persistence integration is explicitly out of scope; the orchestrator returns typed records, and a follow-up wiring story commits them.

## Acceptance criteria

- [ ] `src/alphamind/risk_guardrails/regime_adaptation/__init__.py` exists and re-exports `EventCalendar`, `EventCalendarEntry`, `OverlayActivationDecision`, `RegimeAdaptationAuditEntry`, `RegimeAdaptationOutput`, `RegimeAdaptationState`, `RegimeTransitionBreach`.
- [ ] `src/alphamind/risk_guardrails/regime_adaptation/types.py` exists and defines all seven typed records with the documented field lists.
- [ ] All seven records are `dataclass(frozen=True, slots=True)`.
- [ ] Empty submodule files exist at the documented paths (`regime_mapping.py`, `persistence.py`, `transition_machine.py`, `interpolation.py`, `event_calendar.py`, `pre_event_activator.py`, `stress_activator.py`, `breach_detector.py`, `parameter_set.py`, `orchestrator.py`).
- [ ] `tests/risk_guardrails/regime_adaptation/__init__.py` exists.
- [ ] `tests/risk_guardrails/regime_adaptation/test_imports.py` imports `alphamind.risk_guardrails.regime_adaptation` and `alphamind.risk_guardrails.regime_adaptation.types` successfully and asserts the seven public symbols are reachable from the top-level package.
- [ ] `RegimeAdaptationState` raises `ValueError` when `transition_state == STABLE` and `transition_invocations_remaining != 0`, with both field names in the message.
- [ ] `RegimeAdaptationState` raises `ValueError` when `transition_state == LOOSENING` and `transition_started_invocation_id is None`, naming the field.
- [ ] `RegimeAdaptationState` raises `ValueError` when `transition_state == LOOSENING` and `transition_origin_regime is None`, naming the field.
- [ ] `RegimeTransitionBreach` raises `ValueError` when `overage <= 0` or when `overage` does not equal `current_value - new_limit_value` within `1e-9` tolerance.
- [ ] `EventCalendarEntry` raises `ValueError` on a naive (tz-unaware) `event_timestamp_utc` argument.
- [ ] `OverlayActivationDecision` raises `ValueError` when `pre_event_block_new_positions=True` and `overlay != Overlay.pre_event`, or when `pre_event_block_new_positions=True` and `is_active is False`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
