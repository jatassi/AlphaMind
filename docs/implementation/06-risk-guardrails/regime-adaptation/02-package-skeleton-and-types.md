---
status: in_progress
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

#### 2c. `RegimeTransitionBreach` — per-position or per-rule breach record

```python
@dataclass(frozen=True, slots=True)
class RegimeTransitionBreach:
    """A rule now exceeding a tightened regime limit.

    Two flavors, distinguished by `position_id`:

    1. **Per-position rule breach** (`position_id` is set): a rule whose limit applies
       per-position has at least one position above the tightened limit. One record per
       breaching position. Examples: `position_max_size_pct` (each position checked
       individually against the per-position size cap), `single_short_max_pct` (each
       short position checked individually).

    2. **Aggregate rule breach** (`position_id` is None): a rule whose limit applies to a
       portfolio-level aggregate has the aggregate above the tightened limit. One record
       per breaching rule. Examples: `sector_concentration_*` (sector total),
       `net_long_pct` / `net_short_pct` / `gross_exposure_pct` (directional totals),
       `options_delta_pct` (options delta-adjusted total).

    For aggregate breaches the strategist consults the `Sector exposure breakdown
    (per position)` block in its state header to identify which positions contribute and
    plan trims. The breach record names the rule and the overage; per-position attribution
    is not invented at the breach-record layer (the source data — per-position
    contributions — is already in the strategist's bundle via the sector breakdown).
    """
    position_id: str | None                  # set for per-position rules; None for aggregate rules
    rule_id: str                             # e.g., "position_max_size_pct" or "sector_concentration_tech"
    rule_label: str                          # e.g., "Per-position max size"
    current_value: float                     # for per-position: the position's contribution; for aggregate: the rule's total
    new_limit_value: float                   # the post-tightening limit
    overage: float                           # current_value - new_limit_value (always > 0)
    unit: str                                # mirrors RiskBudgetEntry.unit (e.g., "% of portfolio")
```

Consumed by state-delivery's strategist guardrail state header (`Regime-transition breaches` block) and PM guardrail state header (same block).

The detector (story 07) emits at most one record per `(rule_id, position_id)` pair for per-position rules and exactly one record per breaching aggregate rule.

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

#### 2g. `VixBoundaryThresholds` — VIX classification boundaries

```python
@dataclass(frozen=True, slots=True)
class VixBoundaryThresholds:
    """The three VIX-band boundaries that classify the current VIX into a guardrail regime.

    Sourced from `RegimeClassification` in distillation config; consumed by
    `regime_mapping.map_distillation_to_guardrail_regime` (story 03) and the orchestrator (09).
    """
    low_vol_vix_max: float
    normal_vix_max: float
    elevated_vix_max: float
```

Construction invariant: `0 < low_vol_vix_max < normal_vix_max < elevated_vix_max`. `__post_init__` raises `ValueError` naming all three fields when the ordering is violated or any value is non-positive.

Story 03 (`regime_mapping.py`) imports the type and supplies a `from_regime_classification(rc: RegimeClassification) -> VixBoundaryThresholds` adapter that bridges the upstream Pydantic config to this dataclass. The adapter lives in `regime_mapping.py`, not here — story 02 ships only the typed record.

#### 2h. `NextTransitionDecision` — transition-state-machine output

```python
@dataclass(frozen=True, slots=True)
class NextTransitionDecision:
    """The transition-state-machine output the orchestrator persists.

    Produced by `transition_machine.compute_next_transition` (story 04b); consumed by
    `parameter_set.assemble_active_risk_parameter_set` (story 08) and the orchestrator (09).
    """
    active_regime: Regime
    prior_regime: Regime | None                       # passthrough of new_regime's predecessor for audit
    transition_state: RegimeTransitionState
    transition_invocations_remaining: int
    transition_started_invocation_id: str | None
    transition_origin_regime: Regime | None
```

The orchestrator (story 09) constructs the full `RegimeAdaptationState` by combining this decision with the auxiliary passthrough fields (`distillation_regime_label`, `distillation_vix_level`, `regime_skip_emergency`, `active_overlays`, `as_of`).

#### 2i. `CompositeAlertState` — funding/liquidity composite alert snapshot

```python
@dataclass(frozen=True, slots=True)
class CompositeAlertState:
    """The funding-stress and market-liquidity composite alert flags read from the
    distillation layer's persisted state, used by the stress overlay activator (06b).
    """
    funding_stress_alert_active: bool
    funding_stress_calibration_state: CalibrationState
    market_liquidity_alert_active: bool
    market_liquidity_calibration_state: CalibrationState
    funding_stress_as_of: str | None         # None when no funding_stress row exists
    market_liquidity_as_of: str | None       # None when no market_liquidity row exists
```

Populated by `stress_activator.fetch_composite_alert_state(session)` (story 06b's DB-read helper). Consumed by `evaluate_stress_overlay` (story 06b) and the orchestrator (story 09).

#### 2j. `RuleMetadata` — minimal rule metadata for breach rendering

```python
@dataclass(frozen=True, slots=True)
class RuleMetadata:
    """Minimal per-rule metadata for the breach detector's render-context emission.

    Constructed from `RuleRegistry` (rules-and-limits work tree) by the orchestrator (story 09)
    and passed through to the breach detector (story 07). Decouples the breach detector from
    the full `RuleEntry` shape so the registry's evolution does not ripple through.
    """
    rule_id: str
    label: str          # human-readable, mirrors the registry's rule_label
    unit: str           # mirrors RiskBudgetEntry.unit (e.g., "% of portfolio (delta-adjusted)")
```

#### 2k. `StaleCalendarReport` — event-calendar freshness signal

```python
@dataclass(frozen=True, slots=True)
class StaleCalendarReport:
    """Output of the event-calendar staleness check.

    Produced by `event_calendar.warn_on_stale_calendar` (story 05); consumed by the
    orchestrator (story 09) which surfaces the report via the `stale_event_calendar`
    audit-log entry. An empty calendar is `is_stale=False, days_until_latest=None`.
    """
    is_stale: bool
    latest_event_timestamp_utc: datetime | None
    days_until_latest: float | None         # negative if latest is in the past; None if calendar is empty
```

#### 2l. `LOOSENING_INVOCATIONS` constant

```python
LOOSENING_INVOCATIONS: int = 3
```

The number of invocations over which a loosening transition's per-rule limits linearly interpolate from origin to destination. Mirrors `regimes/*.yaml`'s `transition.loosen_on_exit: linear_over_invocations_3` design directive. Declared once here and imported by the state machine (story 04b) and the interpolation primitive (story 04c) — single source of truth. The value is mechanical (matches the design's `_3` suffix), not operator-tunable per regime; if a future per-regime loosening duration emerges, this constant becomes a default and the state machine accepts an override parameter.

#### 2m. `overlays_to_strings` helper

```python
def overlays_to_strings(overlays: tuple[Overlay, ...]) -> tuple[str, ...]:
    """Convert an Overlay-enum tuple to the string-tuple shape required by
    `portfolio_state.records.capital.ActiveRiskParameterSet.active_overlays`.

    Returns the conversion alphabetically sorted by string value, matching the
    sort invariant the parameter-set assembler (story 08) and the orchestrator
    (story 09) depend on. Single source of truth for the conversion — both
    callers import this helper rather than open-coding the lambda.
    """
    return tuple(sorted(overlay.value for overlay in overlays))
```

The helper exists because two consumers (`08-active-risk-parameter-set-assembler` and `09-resolve-regime-adaptation-orchestrator`) need the same enum→sorted-strings transform; lifting it here removes the open-coded lambda and gives the conversion a single, testable home. The result tuple is alphabetically sorted; downstream `breach_behavior/04b`'s tier-override appender (which inserts `cumulative_drawdown_tier_{N}`) preserves the sort by re-sorting after appending (per `breach_behavior/04b` § Override identity).

### 3. Re-exports

`src/alphamind/risk_guardrails/regime_adaptation/__init__.py` re-exports every typed record from `types.py`:

```python
from alphamind.risk_guardrails.regime_adaptation.types import (
    CompositeAlertState,
    EventCalendar,
    EventCalendarEntry,
    LOOSENING_INVOCATIONS,
    NextTransitionDecision,
    OverlayActivationDecision,
    RegimeAdaptationAuditEntry,
    RegimeAdaptationOutput,
    RegimeAdaptationState,
    RegimeTransitionBreach,
    RuleMetadata,
    StaleCalendarReport,
    VixBoundaryThresholds,
    overlays_to_strings,
)
```

Subsequent stories add their own re-exports for callable surfaces.

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
- `RegimeTransitionBreach` accepts both `position_id="POS-..."` (per-position rule case) and `position_id=None` (aggregate rule case); both flavors construct without error and serialize identically.
- `EventCalendarEntry.event_timestamp_utc` rejects naive datetimes; raises `ValueError` mentioning the field name.
- `OverlayActivationDecision` rejects `pre_event_block_new_positions=True` when `overlay != Overlay.pre_event`; raises `ValueError`.
- `OverlayActivationDecision` rejects `pre_event_block_new_positions=True` when `overlay == Overlay.pre_event` and `is_active is False`; raises `ValueError` (cannot block on an inactive overlay).
- `VixBoundaryThresholds` rejects construction when bounds are not strictly ordered (`low_vol_vix_max=14, normal_vix_max=10, elevated_vix_max=35` raises `ValueError` naming all three) or when any value is non-positive.
- `NextTransitionDecision` is constructible with all valid combinations; its invariants are exercised indirectly via `RegimeAdaptationState` (which the orchestrator builds from the decision).
- `CompositeAlertState` is constructible with valid scalar inputs; its `*_as_of` fields tolerate `None` for absent composite rows.
- `RuleMetadata` is constructible with non-empty `rule_id`, `label`, and `unit` strings.
- `StaleCalendarReport` is constructible with `is_stale=False, latest_event_timestamp_utc=None, days_until_latest=None` (the empty-calendar case) and with the populated case.
- `overlays_to_strings(())` returns `()`. `overlays_to_strings((Overlay.stress, Overlay.pre_event))` returns `("pre_event", "stress")` — alphabetically sorted by string value. `overlays_to_strings((Overlay.pre_event,))` returns `("pre_event",)`.

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

- [ ] `src/alphamind/risk_guardrails/regime_adaptation/__init__.py` exists and re-exports `CompositeAlertState`, `EventCalendar`, `EventCalendarEntry`, `NextTransitionDecision`, `OverlayActivationDecision`, `RegimeAdaptationAuditEntry`, `RegimeAdaptationOutput`, `RegimeAdaptationState`, `RegimeTransitionBreach`, `RuleMetadata`, `StaleCalendarReport`, `VixBoundaryThresholds`.
- [ ] `src/alphamind/risk_guardrails/regime_adaptation/types.py` exists and defines all twelve typed records with the documented field lists.
- [ ] All twelve records are `dataclass(frozen=True, slots=True)`.
- [ ] Empty submodule files exist at the documented paths (`regime_mapping.py`, `persistence.py`, `transition_machine.py`, `interpolation.py`, `event_calendar.py`, `pre_event_activator.py`, `stress_activator.py`, `breach_detector.py`, `parameter_set.py`, `orchestrator.py`).
- [ ] `tests/risk_guardrails/regime_adaptation/__init__.py` exists.
- [ ] `tests/risk_guardrails/regime_adaptation/test_imports.py` imports `alphamind.risk_guardrails.regime_adaptation` and `alphamind.risk_guardrails.regime_adaptation.types` successfully and asserts the twelve public symbols are reachable from the top-level package.
- [ ] `RegimeAdaptationState` raises `ValueError` when `transition_state == STABLE` and `transition_invocations_remaining != 0`, with both field names in the message.
- [ ] `RegimeAdaptationState` raises `ValueError` when `transition_state == LOOSENING` and `transition_started_invocation_id is None`, naming the field.
- [ ] `RegimeAdaptationState` raises `ValueError` when `transition_state == LOOSENING` and `transition_origin_regime is None`, naming the field.
- [ ] `RegimeTransitionBreach` raises `ValueError` when `overage <= 0` or when `overage` does not equal `current_value - new_limit_value` within `1e-9` tolerance. Accepts both `position_id="POS-..."` and `position_id=None` constructions.
- [ ] `EventCalendarEntry` raises `ValueError` on a naive (tz-unaware) `event_timestamp_utc` argument.
- [ ] `OverlayActivationDecision` raises `ValueError` when `pre_event_block_new_positions=True` and `overlay != Overlay.pre_event`, or when `pre_event_block_new_positions=True` and `is_active is False`.
- [ ] `VixBoundaryThresholds` raises `ValueError` when bounds are not strictly ordered or when any value is non-positive.
- [ ] `NextTransitionDecision`, `CompositeAlertState`, `RuleMetadata`, `StaleCalendarReport` are constructible with valid inputs (including the empty-calendar `StaleCalendarReport(is_stale=False, latest_event_timestamp_utc=None, days_until_latest=None)`).
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
