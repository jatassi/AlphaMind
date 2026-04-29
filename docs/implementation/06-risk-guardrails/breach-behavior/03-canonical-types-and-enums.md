---
status: not_started
completed_date:
commit_id:
---

# 03 — Canonical typed records & enums

## Goal

Define every typed record and enum the breach-behavior package exposes. Subsequent stories in this work tree consume these types; downstream features (state-delivery, regime-adaptation, the continuous monitor) import from this module rather than redeclaring local copies. The types are pure value objects — frozen Pydantic v2 models or `StrEnum` — with no behavior beyond field validation and a small number of post-validators where invariants warrant.

This story formalizes the package's public type surface in one place so the parallel stories at wave 4 (zone classifier, drawdown tier, hard rejection, position selection, regime-transition detection) and beyond can compose against a stable API.

## Reading

- `docs/design/06-risk-guardrails/breach-behavior.md` — the source of truth for every type defined here. Particularly:
  - § Escalation model — the four zones already typed as `RiskZone` in `portfolio_state/records/capital.py`; this story re-uses that enum, does not redefine it.
  - § Forced reduction policy — `BreachClassification` (immediate-engine vs. deferred-to-pm), `SecondaryBreachCheckResult` shape, `PositionSelectionRationale` discipline.
  - § Drawdown halt mode — `HaltState` shape (daily-halt + cumulative-full-halt flags + drawdown numbers for the banner), `DrawdownTier` already typed in `portfolio_state/records/capital.py`.
  - § Margin call cascade handling — `CascadeId` semantics (string identifier shared across cascade chain).
  - § Emergency invocation trigger — `EmergencyTrigger` enum (regime_jump, multi_rule_breach, daily_drawdown_velocity, margin_call), `EmergencyContext` shape (trigger + detail + minutes-since-last-invocation + normal-cadence-minutes).
  - § Hard rejection semantics — `HardRejectionPayload` shape (rule(s) breached, current state, limit, overage, suggested modification, headroom after).
  - § Traceability for engine-originated actions — `EngineGuardrailTriggerRecord` shape (rule, breach details, trigger timestamp, regime at breach, position selection rationale, optional cascade_id, optional secondary breach check result).
- `docs/design/05-execution-layer/engine-envelope-schema.md` — the formal JSON Schema for engine-originated envelopes. The Python types defined here mirror that schema; field names, types, and invariants must match what the schema enforces. Note `commands` is a single-element array (the schema's `maxItems: 1`); the Python `EngineEnvelope` exposes `command: OmsCloseCommand` as a single field with the array-shape preserved at JSON serialization time via a custom serializer or post-validator (see Notes below).
- `docs/design/06-risk-guardrails/state-delivery.md` — confirms `HaltState`, `EmergencyContext`, `RegimeTransitionBreach` are inputs to the rendering layer; the state-delivery package imports these from `breach_behavior` rather than redefining them.
- `docs/design/06-risk-guardrails/regime-adaptation.md` — confirms `RegimeTransitionBreach` is the typed surface that flows from the regime-tightening event to the strategist/PM via state-delivery.
- `docs/design/05-execution-layer/oms-command-schema.md` — the OMS command schema; `EngineEnvelope.command` is a `Close` command with `close_rationale_type=risk_management` and `risk_management_subtype=engine_guardrail`. The Python type for the embedded command can be a `TypedDict` or a Pydantic alias of the OMS-side command class once that lands; for this story, declare it as a `BaseModel` with the structural fields the schema requires (the engine-envelope-schema's `commands[0]` constraint).
- `src/alphamind/portfolio_state/records/capital.py` — already-shipped `RiskZone`, `DrawdownTier`, `RegimeLabel`, `RegimeTransitionState` enums and `DrawdownState`, `RiskBudgetEntry`, `RiskBudgetConsumption`, `ActiveRiskParameterSet` records. This story imports — does NOT redefine — these.
- `src/alphamind/portfolio_state/records/positions.py` — already-shipped `PositionRecord`, `Direction`, `InstrumentType`. This story imports for type annotations.
- `src/alphamind/config/models/guardrails.py` — already-shipped `EnforcementTier`, `BreachResponse` (immediate_engine / deferred_to_pm), `EscalationZones`, `ProgressiveTier`. This story imports — does NOT redefine — these.
- `docs/implementation/06-risk-guardrails/state-delivery/05-halt-mode-header-modifications.md` — the sibling story that currently defines `HaltState` inline. This work tree's `HaltState` is the canonical version; state-delivery's local `HaltState` declaration becomes an `import` from `breach_behavior.types` when state-delivery dispatches. Cross-feature integration note in `Notes` below.
- `docs/implementation/06-risk-guardrails/state-delivery/06-emergency-invocation-header.md` — the sibling story that currently defines `EmergencyTrigger` and `EmergencyContext` inline. Same cross-feature integration: state-delivery's local declarations become imports from `breach_behavior.types`.

## Depends on

- 02 (package skeleton)

## Scope

In scope, all under `src/alphamind/risk_guardrails/breach_behavior/types.py`. Re-exports in `src/alphamind/risk_guardrails/breach_behavior/__init__.py`. Tests at `tests/risk_guardrails/breach_behavior/test_types.py`.

### 1. Re-exports of upstream enums

Re-export — do NOT redefine — the enums and records this package consumes from upstream:

```python
from alphamind.config.models.guardrails import (
    BreachResponse,
    EnforcementTier,
    EscalationZones,
    ProgressiveTier,
)
from alphamind.portfolio_state.records.capital import (
    DrawdownState,
    DrawdownTier,
    RegimeLabel,
    RegimeTransitionState,
    RiskBudgetConsumption,
    RiskBudgetEntry,
    RiskZone,
    ActiveRiskParameterSet,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    InstrumentType,
    PositionRecord,
)
```

These re-exports give downstream consumers a single import surface (`from alphamind.risk_guardrails.breach_behavior import RiskZone`) without coupling to upstream module paths. Per `feedback_no_inventing_component_names.md`, the names match the upstream definitions verbatim.

### 2. New enums

```python
class EmergencyTrigger(StrEnum):
    """Emergency invocation trigger taxonomy from breach-behavior § Emergency invocation trigger."""

    REGIME_JUMP = "regime_jump"
    MULTI_RULE_BREACH = "multi_rule_breach"
    DAILY_DRAWDOWN_VELOCITY = "daily_drawdown_velocity"
    MARGIN_CALL = "margin_call"


class SecondaryBreachOutcome(StrEnum):
    """Outcome of a secondary-breach check on a proposed protective close.

    Mirrors engine-envelope-schema.md § secondary_breach_check_result.result enum.
    """

    NO_SECONDARY_BREACH = "no_secondary_breach"
    SECONDARY_BREACH_AVOIDED = "secondary_breach_avoided"
    DEFERRED_TO_PM = "deferred_to_pm"


class CloseRationaleType(StrEnum):
    """Close rationale type the engine envelope's embedded close command carries."""

    RISK_MANAGEMENT = "risk_management"


class RiskManagementSubtype(StrEnum):
    """Sub-classification within risk_management close rationale.

    engine_guardrail distinguishes monitor-issued protective closes from PM-issued
    risk-management closes in the feedback loop. Only this value is valid for
    engine-originated envelopes per engine-envelope-schema.md.
    """

    ENGINE_GUARDRAIL = "engine_guardrail"
```

### 3. Engine-side typed records

#### 3.1 `EngineGuardrailTriggerRecord`

Mirrors engine-envelope-schema.md § `guardrail_trigger_record`. Fields:

```python
class BreachDetails(BaseModel):
    model_config = ConfigDict(frozen=True)

    current_value: float
    limit_value: float
    overage: float                          # signed; positive for overages
    unit: str | None = None
    regime_at_breach: RegimeLabel | None = None


class SecondaryBreachCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    result: SecondaryBreachOutcome
    notes: str | None = None


class EngineGuardrailTriggerRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    rule_breached: str                      # canonical rule ID from guardrails.yaml
    trigger_timestamp: datetime             # tz-aware
    breach_details: BreachDetails
    position_selection_rationale: str       # human-readable; deterministic per breach-behavior.md
    cascade_id: str | None = None           # present when part of a cascade
    secondary_breach_check_result: SecondaryBreachCheckResult | None = None

    @field_validator("trigger_timestamp")
    @classmethod
    def _require_tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            msg = "trigger_timestamp must be timezone-aware"
            raise ValueError(msg)
        return v
```

#### 3.2 `EngineCloseCommand` and `EngineEnvelope`

The embedded close command is a thin Pydantic model with the structural fields the engine-envelope schema's allOf constraint enforces (command_type, close_rationale_type, risk_management_subtype, plus the OMS-side close-command identifying fields — position_id, quantity-or-all, execution method). For now, declare only the breach-behavior-relevant subset; the OMS-side full schema lands in the execution-layer work tree and the import wires up there.

```python
class EngineCloseCommand(BaseModel):
    model_config = ConfigDict(frozen=True)

    command_id: str                         # MON.{session}.{trigger}.{ordinal} — see oms-command-ids.md
    command_type: Literal["close"] = "close"
    close_rationale_type: CloseRationaleType = CloseRationaleType.RISK_MANAGEMENT
    risk_management_subtype: RiskManagementSubtype = RiskManagementSubtype.ENGINE_GUARDRAIL

    position_id: str
    quantity_or_all: Literal["all"] | float  # "all" for full close; positive float for partial trim
    execution_method: Literal["market", "limit"] = "market"
    limit_price: float | None = None        # required when execution_method == "limit"

    @model_validator(mode="after")
    def _validate_limit_price(self) -> EngineCloseCommand:
        if self.execution_method == "limit" and self.limit_price is None:
            msg = "limit_price required when execution_method is 'limit'"
            raise ValueError(msg)
        if self.execution_method == "market" and self.limit_price is not None:
            msg = "limit_price must be None when execution_method is 'market'"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _validate_quantity(self) -> EngineCloseCommand:
        if isinstance(self.quantity_or_all, float) and self.quantity_or_all <= 0:
            msg = f"quantity_or_all must be positive when not 'all'; got {self.quantity_or_all}"
            raise ValueError(msg)
        return self


class EngineEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True)

    envelope_id: str                        # MON.{monitor_session_id}.{trigger_id} — see oms-command-ids.md
    invocation_id: None = None              # always None for engine-originated; trigger_timestamp anchors
    trigger_timestamp: datetime             # tz-aware; matches guardrail_trigger_record.trigger_timestamp
    source_provenance: Literal["engine_guardrail"] = "engine_guardrail"
    guardrail_trigger_record: EngineGuardrailTriggerRecord
    command: EngineCloseCommand             # exactly one; serializes as a single-element array per the JSON schema

    @field_validator("envelope_id")
    @classmethod
    def _validate_envelope_id_pattern(cls, v: str) -> str:
        # MON.{monitor_session_id}.{trigger_id}; trigger_id is digits only
        pattern = r"^MON\.[^.]+\.[0-9]+$"
        if not re.fullmatch(pattern, v):
            msg = f"envelope_id must match pattern {pattern!r}; got {v!r}"
            raise ValueError(msg)
        return v

    @field_validator("trigger_timestamp")
    @classmethod
    def _require_tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            msg = "trigger_timestamp must be timezone-aware"
            raise ValueError(msg)
        return v

    @model_validator(mode="after")
    def _validate_trigger_timestamps_match(self) -> EngineEnvelope:
        # engine-envelope-schema.md cross-field invariant: top-level trigger_timestamp == guardrail_trigger_record.trigger_timestamp
        if self.trigger_timestamp != self.guardrail_trigger_record.trigger_timestamp:
            msg = "trigger_timestamp must match guardrail_trigger_record.trigger_timestamp"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _validate_command_id_envelope_relationship(self) -> EngineEnvelope:
        # engine-envelope-schema.md: embedded command_id is "{envelope_id}.{ordinal}" with envelope_id as MON.{session}.{trigger}
        # For single-command envelopes (this is the only case), ordinal is "1".
        expected_prefix = f"{self.envelope_id}."
        if not self.command.command_id.startswith(expected_prefix):
            msg = (
                f"command.command_id must begin with {expected_prefix!r}; "
                f"got {self.command.command_id!r}"
            )
            raise ValueError(msg)
        return self
```

JSON serialization: `EngineEnvelope.model_dump()` produces `command` as a scalar field; the engine-envelope JSON Schema expects `commands` as a single-element array. The schema-aligned serialization is the engine-envelope-assembler story's responsibility (story 06); this story exposes the Python-side scalar shape, which is more ergonomic for callers that compose envelopes one at a time.

### 4. Halt-state typed record

```python
class HaltState(BaseModel):
    """Active drawdown-halt state surfaced to state-delivery and the continuous monitor.

    Constructed only when at least one halt is active. Per breach-behavior.md § Drawdown halt mode,
    daily-halt fires when daily drawdown ≥ 100% of the daily limit; cumulative full halt fires when
    cumulative drawdown ≥ tier 3 threshold (DrawdownTier.FULL_HALT). Tier 1 and tier 2 cumulative
    drawdown responses are NOT halts — they apply progressive parameter overrides without blocking
    new positions outright.
    """

    model_config = ConfigDict(frozen=True)

    daily_halt_active: bool
    cumulative_full_halt_active: bool
    daily_drawdown_pct: float                       # current intraday drawdown, for the banner
    daily_drawdown_limit_pct: float                 # active daily limit, for the banner

    @model_validator(mode="after")
    def _validate_at_least_one_active(self) -> HaltState:
        if not (self.daily_halt_active or self.cumulative_full_halt_active):
            msg = "HaltState should not be constructed unless at least one halt is active"
            raise ValueError(msg)
        return self

    @field_validator("daily_drawdown_pct", "daily_drawdown_limit_pct")
    @classmethod
    def _require_non_negative(cls, v: float) -> float:
        if v < 0:
            msg = f"value must be >= 0; got {v}"
            raise ValueError(msg)
        return v
```

### 5. Emergency invocation context

```python
class EmergencyContext(BaseModel):
    """Emergency-invocation trigger context surfaced to state-delivery and the scheduler.

    Constructed only when the continuous monitor's emergency-trigger evaluator fires.
    Per breach-behavior.md § Emergency invocation trigger.
    """

    model_config = ConfigDict(frozen=True)

    trigger: EmergencyTrigger
    trigger_detail: str                             # human-readable; e.g., "Regime jump: low-vol → crisis (VIX 12 → 38)"
    minutes_since_last_invocation: float
    normal_cadence_minutes: float                   # configured scheduler cadence

    @field_validator("minutes_since_last_invocation", "normal_cadence_minutes")
    @classmethod
    def _require_positive(cls, v: float) -> float:
        if v <= 0:
            msg = f"value must be positive; got {v}"
            raise ValueError(msg)
        return v
```

### 6. Hard rejection payload

```python
class RejectionRuleEntry(BaseModel):
    """One rule's entry in the hard rejection payload.

    Mirrors guardrail-evaluation's RuleProjection shape but adds suggested_modification and
    headroom_after_compliance — caller-side compositions documented in breach-behavior.md
    § Hard rejection semantics.
    """

    model_config = ConfigDict(frozen=True)

    rule_id: str
    current_value: float
    limit_value: float
    projected_after: float
    overage: float                                  # projected_after - limit_value when projected_after > limit_value
    headroom_remaining: float
    unit: str


class HardRejectionPayload(BaseModel):
    """Synchronous T3 rejection payload returned to the PM.

    Per breach-behavior.md § Hard rejection semantics. Carries every breaching rule, the
    PM-actionable suggested modification, and the projected post-compliance headroom.
    """

    model_config = ConfigDict(frozen=True)

    rejected_command_id: str
    breaching_rules: tuple[RejectionRuleEntry, ...]
    suggested_modification: str                     # e.g., "reduce size by 42%" or "switch to lower-delta strike"
    headroom_after_hypothetical_compliance: tuple[RejectionRuleEntry, ...]

    @model_validator(mode="after")
    def _validate_at_least_one_breaching_rule(self) -> HardRejectionPayload:
        if len(self.breaching_rules) == 0:
            msg = "HardRejectionPayload must carry at least one breaching rule"
            raise ValueError(msg)
        return self
```

### 7. Regime-transition breach record

```python
class RegimeTransitionBreach(BaseModel):
    """A position breaching a newly-tightened limit after a regime transition.

    Per regime-adaptation.md § Position handling when tightening creates breaches and
    state-delivery.md § Strategist guardrail state header — Regime-transition breaches.
    Detection is owned by breach_behavior; state-delivery renders these into the strategist
    and PM headers; the strategist proposes remedies and the PM executes.
    """

    model_config = ConfigDict(frozen=True)

    position_id: str
    rule_breached: str                              # canonical rule ID
    current_value: float                            # the position's contribution to the rule (e.g., 4.5% of portfolio)
    new_regime_limit: float                         # the post-transition limit (e.g., 3.5% under elevated)
    overage: float                                  # current_value - new_regime_limit; always positive
    regime_label: RegimeLabel                       # the new (post-transition) regime label
    unit: str

    @model_validator(mode="after")
    def _validate_overage_arithmetic(self) -> RegimeTransitionBreach:
        expected = self.current_value - self.new_regime_limit
        if not math.isclose(self.overage, expected, abs_tol=1e-9):
            msg = (
                f"overage ({self.overage}) must equal current_value - new_regime_limit "
                f"({expected})"
            )
            raise ValueError(msg)
        if self.overage <= 0:
            msg = f"overage must be positive (in-breach by definition); got {self.overage}"
            raise ValueError(msg)
        return self
```

### 8. Per-breach-type position selection result

```python
class PositionSelectionAction(StrEnum):
    """Action prescribed by a position-selection primitive."""

    FULL_CLOSE = "full_close"
    PARTIAL_TRIM = "partial_trim"


class PositionSelectionResult(BaseModel):
    """Output of a position-selection primitive (story 04d).

    Carries the selected position, the prescribed action (full close or partial trim with
    target post-trim size), and the human-readable rationale that feeds
    EngineGuardrailTriggerRecord.position_selection_rationale.
    """

    model_config = ConfigDict(frozen=True)

    position_id: str
    action: PositionSelectionAction
    target_post_action_size_pct_of_portfolio: float | None = None  # required for PARTIAL_TRIM, None for FULL_CLOSE
    rationale: str

    @model_validator(mode="after")
    def _validate_action_target_consistency(self) -> PositionSelectionResult:
        if self.action == PositionSelectionAction.PARTIAL_TRIM and self.target_post_action_size_pct_of_portfolio is None:
            msg = "target_post_action_size_pct_of_portfolio required for PARTIAL_TRIM"
            raise ValueError(msg)
        if self.action == PositionSelectionAction.FULL_CLOSE and self.target_post_action_size_pct_of_portfolio is not None:
            msg = "target_post_action_size_pct_of_portfolio must be None for FULL_CLOSE"
            raise ValueError(msg)
        return self
```

### 9. Tests at `tests/risk_guardrails/breach_behavior/test_types.py`

Cover every model's:
- Happy-path construction with valid fields.
- Each field validator firing on the documented invalid input.
- Each post-validator firing on the documented invalid combination.
- Frozen-ness: assigning to a constructed model raises `ValidationError` or `pydantic.errors.PydanticUserError` (Pydantic v2 emits `ValidationError` on assignment when `frozen=True`).
- For `EngineEnvelope`: envelope_id pattern matches `MON.{session}.{trigger}` with digit-only trigger_id; embedded command_id starts with `{envelope_id}.`; top-level trigger_timestamp matches the embedded record's; tz-aware-ness enforced.
- For `HaltState`: at least one of daily/cumulative-full-halt must be active; both can be active simultaneously.
- For `EmergencyContext`: positive minutes-since-last-invocation and normal-cadence-minutes; trigger enum values match the four documented cases.
- For `RegimeTransitionBreach`: overage arithmetic identity holds; overage must be strictly positive.
- For `PositionSelectionResult`: FULL_CLOSE requires None target; PARTIAL_TRIM requires non-None target.
- For `HardRejectionPayload`: at least one breaching rule.
- Re-export sanity: `from alphamind.risk_guardrails.breach_behavior import RiskZone, DrawdownTier, BreachResponse` succeeds and references the same enum classes as the upstream modules.

### 10. Re-exports from `__init__.py`

`src/alphamind/risk_guardrails/breach_behavior/__init__.py` re-exports every public type in this story. The list:

```python
from alphamind.risk_guardrails.breach_behavior.config import (
    BreachBehaviorConfig,
    load_breach_behavior_config,
)
from alphamind.risk_guardrails.breach_behavior.types import (
    # Re-exports (do not redefine)
    ActiveRiskParameterSet,
    BreachResponse,
    Direction,
    DrawdownState,
    DrawdownTier,
    EnforcementTier,
    EscalationZones,
    InstrumentType,
    PositionRecord,
    ProgressiveTier,
    RegimeLabel,
    RegimeTransitionState,
    RiskBudgetConsumption,
    RiskBudgetEntry,
    RiskZone,
    # New types
    BreachDetails,
    CloseRationaleType,
    EmergencyContext,
    EmergencyTrigger,
    EngineCloseCommand,
    EngineEnvelope,
    EngineGuardrailTriggerRecord,
    HaltState,
    HardRejectionPayload,
    PositionSelectionAction,
    PositionSelectionResult,
    RegimeTransitionBreach,
    RejectionRuleEntry,
    RiskManagementSubtype,
    SecondaryBreachCheckResult,
    SecondaryBreachOutcome,
)
```

Subsequent stories' modules add their public callables to this re-export list as they land.

Out of scope:
- Behavior beyond field validation: zone classification, tier determination, payload assembly, secondary-breach checking, envelope construction, etc. — all owned by stories 04+.
- The OMS command schema's full close-command shape — `EngineCloseCommand` declares only the fields the engine-envelope schema's allOf constraint enforces. The full OMS-side schema lands in the execution-layer work tree; once available, this story's `EngineCloseCommand` may be replaced by an import from there in a coordinated edit.
- Any rendering of these types into agent-facing text — state-delivery owns rendering.

## Notes

**Cross-feature integration with state-delivery.** State-delivery's stories 05 (halt-mode wrappers) and 06 (emergency-invocation block) currently declare `HaltState`, `EmergencyTrigger`, and `EmergencyContext` inline within `state_delivery/halt_mode.py` and `state_delivery/emergency.py`. When state-delivery lands after this work tree, those local declarations become imports from `alphamind.risk_guardrails.breach_behavior`. The orchestrator surfaces this dependency at dispatch time. Until the state-delivery stories are revised, the two work trees may carry parallel definitions; the schema and field set are designed to match exactly so the cutover is mechanical (replace local class with `from … import …`).

**Why a single canonical-types story rather than embedding types in their using stories.** The state-delivery work tree spreads typed records across using stories (`HaltState` in 05, `EmergencyContext` in 06, `RegimeTransitionBreach` in 04b). For breach-behavior, multiple primitive stories (04a–04e, 05a–05c, 06, 07) consume overlapping subsets — `EngineEnvelope` is used by 06 and 07; `HaltState` is used by 05a and emerges into state-delivery; `RegimeTransitionBreach` is used by 04e and emerges into state-delivery. Centralizing the types in story 03 lets the wave-3 parallel batch dispatch cleanly without ordering pain.

**Pydantic v2 conventions.** Every model is `model_config = ConfigDict(frozen=True)`. Field validators are `@field_validator` with `@classmethod`; cross-field invariants are `@model_validator(mode="after")`. Datetime fields are `datetime` (not `str`); tz-awareness is enforced where the design calls for it (envelope and trigger timestamps are tz-aware; halt-state drawdown numbers are floats with no time component).

**Per `feedback_no_inventing_component_names.md`,** every type name maps to a documented design-doc concept. `HaltState` ← state-delivery 05's local class; `EmergencyContext` ← state-delivery 06's local class; `EmergencyTrigger` ← state-delivery 06's local enum; `EngineEnvelope` ← engine-envelope-schema's "Engine-originated command envelope" title; `EngineGuardrailTriggerRecord` ← engine-envelope-schema's `guardrail_trigger_record` $def; `BreachDetails` ← engine-envelope-schema's `breach_details` field; `SecondaryBreachCheckResult` ← engine-envelope-schema's `secondary_breach_check_result` field; `SecondaryBreachOutcome` ← engine-envelope-schema's `secondary_breach_check_result.result` enum; `HardRejectionPayload` and `RejectionRuleEntry` ← breach-behavior § Hard rejection semantics's bullet list; `RegimeTransitionBreach` ← state-delivery's strategist-header "Regime-transition breaches" block + regime-adaptation § Position handling. `PositionSelectionResult` and `PositionSelectionAction` are package-internal — they encode the "full close" / "trim to 95%" distinction the design specifies, with names paraphrasing the design's per-breach-type prescriptions.

**Per `feedback_per_producer_schema.md`,** each typed record has a single producer (the story that owns the corresponding primitive — 04a–04e, 05a–05c, 06, 07). This story declares the schema; the producer story populates it.

**Why `EngineEnvelope.command` is a scalar field rather than a `tuple[EngineCloseCommand, ...]` of length 1.** The engine-envelope JSON Schema enforces `commands` as a single-element array (`maxItems: 1`). A Python tuple-of-length-1 for what is structurally always one command is awkward at the call site; story 06's envelope assembler converts the scalar to the array shape at JSON serialization time via a custom `model_dump` override or a sibling schema-aligned serializer function. Keeping the Python-side type scalar matches the cascade-emits-one-envelope-per-CLOSE-trigger semantics from breach-behavior § Margin call cascade.

**`EngineCloseCommand.quantity_or_all` typing.** The OMS close-command schema accepts `"all"` (string literal) for full closes or a positive numeric quantity for partial trims. Python's union typing handles this cleanly with `Literal["all"] | float`. The post-validator catches the float-≤-0 case; the `Literal` enforces the string side at parse time.

**Test coverage discipline.** The acceptance criteria below name the categories, not the individual cases. The implementing subagent uses `/tdd` to drive the work and produces one test per documented invariant; ad-hoc additional tests are welcome but not required.

## Acceptance criteria

- [ ] `src/alphamind/risk_guardrails/breach_behavior/types.py` exists and defines every type in this story.
- [ ] Re-exports from `src/alphamind/risk_guardrails/breach_behavior/__init__.py` cover every type listed in section 10 above.
- [ ] Every model uses `model_config = ConfigDict(frozen=True)`.
- [ ] `EmergencyTrigger` is a `StrEnum` with members `REGIME_JUMP="regime_jump"`, `MULTI_RULE_BREACH="multi_rule_breach"`, `DAILY_DRAWDOWN_VELOCITY="daily_drawdown_velocity"`, `MARGIN_CALL="margin_call"`.
- [ ] `SecondaryBreachOutcome` is a `StrEnum` with members `NO_SECONDARY_BREACH`, `SECONDARY_BREACH_AVOIDED`, `DEFERRED_TO_PM` matching the engine-envelope schema's enum verbatim.
- [ ] `HaltState._validate_at_least_one_active` raises `ValueError` when both `daily_halt_active=False` and `cumulative_full_halt_active=False`.
- [ ] `HaltState` accepts `daily_halt_active=True, cumulative_full_halt_active=True` together (concurrent halt) without error.
- [ ] `EmergencyContext._require_positive` raises on zero or negative `minutes_since_last_invocation` or `normal_cadence_minutes`.
- [ ] `EngineEnvelope.envelope_id` validator rejects ids not matching `^MON\.[^.]+\.[0-9]+$` (e.g., `"PM.123"`, `"MON..123"`, `"MON.s.abc"`).
- [ ] `EngineEnvelope` cross-field validator rejects mismatched top-level `trigger_timestamp` vs. embedded `guardrail_trigger_record.trigger_timestamp`.
- [ ] `EngineEnvelope` cross-field validator rejects an embedded `command.command_id` that does not start with `{envelope_id}.`.
- [ ] `EngineEnvelope.trigger_timestamp` and `EngineGuardrailTriggerRecord.trigger_timestamp` reject naive (non-tz-aware) datetimes.
- [ ] `EngineCloseCommand` rejects `quantity_or_all=0`, `-5`, etc. (strictly-positive when not `"all"`).
- [ ] `EngineCloseCommand` requires `limit_price` when `execution_method="limit"` and forbids it when `"market"`.
- [ ] `RegimeTransitionBreach._validate_overage_arithmetic` rejects mismatched `overage` vs. `current_value - new_regime_limit` (within 1e-9 tolerance) and rejects non-positive `overage`.
- [ ] `PositionSelectionResult._validate_action_target_consistency` requires `target_post_action_size_pct_of_portfolio` non-None for `PARTIAL_TRIM` and None for `FULL_CLOSE`.
- [ ] `HardRejectionPayload._validate_at_least_one_breaching_rule` rejects empty `breaching_rules`.
- [ ] Re-exports of upstream enums (`RiskZone`, `DrawdownTier`, `BreachResponse`, `EnforcementTier`, `EscalationZones`, `ProgressiveTier`, `RegimeLabel`, `RegimeTransitionState`) reference the same enum classes as their upstream modules (`enum is enum` identity check passes).
- [ ] All models, when constructed with valid fields, are immutable: assigning to a field raises a `ValidationError` (Pydantic v2 frozen behavior).
- [ ] `tests/risk_guardrails/breach_behavior/test_types.py` covers each documented invariant with at least one test.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
