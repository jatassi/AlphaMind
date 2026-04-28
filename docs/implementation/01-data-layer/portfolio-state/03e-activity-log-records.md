---
status: in_progress
completed_date:
commit_id:
---

# 03e — Activity log records

## Goal

Define the typed activity-log entry record and all per-event-type detail-payload classes at
`src/alphamind/portfolio_state/records/activity_log.py`, with a corresponding test suite at
`tests/portfolio_state/records/test_activity_log.py`.

The activity log is the semantic changelog for portfolio state (raw state category 5). Each entry
records a specific event — what changed, why, and from which OMS subsystem — and carries a
structured detail payload whose shape depends on the event type. This story defines the full
discriminated-union contract; filtering and projection helpers (categories 5a/5b/5c) live in
story 05e.

## Reading

- `../../../design/01-data-layer/internal/portfolio-state.md` — section 5: intra-invocation
  changelog (5a), PM decision log (5b), position modification trail (5c); the three delivery views
  this record set serves
- `../../../design/05-execution-layer/state-persistence.md` — Activity log entries section,
  including the full **Event type catalog** with eight groups (position lifecycle, order lifecycle,
  bracket events, thesis events, cash and margin events, risk and guardrail events, PM decision
  events, corporate action events). Each event type has a structured detail payload — those payloads
  are exactly what this story models
- `../../../design/04-decision-layer/portfolio-manager.md` — Envelope structure section: base
  envelope fields, PM-originated envelope fields (source provenance, evaluation, modifications array,
  resulting command IDs), engine-originated envelope fields. The `PMDecisionDetail` stores these as
  JSON-shaped dict fields; this story does not reproduce the full envelope schema
- `../../../design/05-execution-layer/corporate-actions.md` — per-action-type matrix: the nine
  action types the `CorporateActionType` enum covers, and the `corporate_action_applied` detail
  fields
- `02-package-skeleton-and-config.md` — package layout; `src/alphamind/portfolio_state/records/activity_log.py`
  is listed as story 03e's target
- `../../../implementation/03-analysis-layer/synthesizer/05b-portfolio-state-read-protocol.md` —
  sibling pattern: frozen Pydantic v2 models, StrEnum discriminators, validation tests,
  in-scope/out-of-scope split, acceptance-criteria style

## Depends on

- 02

## Scope

In scope:

### 1. Top-level enums — all `StrEnum`, members in ALL CAPS

**`EventGroup`** — the eight catalog groups from state-persistence.md:

- `POSITION_LIFECYCLE`
- `ORDER_LIFECYCLE`
- `BRACKET`
- `THESIS`
- `CASH_AND_MARGIN`
- `RISK_AND_GUARDRAIL`
- `PM_DECISION`
- `CORPORATE_ACTION`

**`EventType`** — one member per event in the catalog. Enumerate all 35 members in catalog
order; each name is the snake_case identifier as it appears in state-persistence.md:

Position lifecycle:
- `POSITION_OPENED`
- `POSITION_CLOSED`
- `POSITION_ADDED`
- `POSITION_REDUCED`

Order lifecycle:
- `ORDER_SUBMITTED`
- `ORDER_FILLED`
- `ORDER_PARTIALLY_FILLED`
- `ORDER_CANCELLED`
- `ORDER_EXPIRED`
- `ORDER_REJECTED`
- `ORDER_MODIFIED`

Bracket events:
- `BRACKET_ACTIVATED`
- `BRACKET_COMPLETED`
- `BRACKET_DISSOLVED`
- `BRACKET_MODIFIED`
- `BRACKET_INCOMPLETE_WARNING`
- `BRACKET_CANCELLED_CORPORATE_ACTION`

Thesis events:
- `THESIS_CREATED`
- `THESIS_COMPONENT_ADDED`
- `THESIS_COMPONENT_UPDATED`
- `THESIS_RESOLVED`
- `THESIS_STATUS_CHANGED`

Cash and margin events:
- `CASH_DEBITED`
- `CASH_CREDITED`
- `CAPITAL_RESERVED`
- `CAPITAL_RELEASED`
- `MARGIN_CALL`
- `MARGIN_CALL_RESOLVED`
- `MARGIN_LIQUIDATION`

Risk and guardrail events:
- `GUARDRAIL_REJECTION`
- `RISK_LIMIT_APPROACHED`
- `RISK_PARAMETER_CHANGED`

PM decision events:
- `PM_DECISION`
- `COMMAND_ABANDONED`

Corporate action events:
- `CORPORATE_ACTION_APPLIED`

**`EventSource`** — the OMS subsystems that emit entries, per state-persistence.md "Source: which
OMS subsystem generated this entry":

- `FILL_PROCESSOR`
- `COMMAND_EXECUTOR`
- `BRACKET_MANAGER`
- `MARGIN_MONITOR`
- `GUARDRAIL_LAYER`
- `CORPORATE_ACTION_PROCESSOR`

**Group-specific discriminator enums** — declare these only for the closed sets already named in the
design docs:

`PositionExitMethod` (per `position_closed` event detail, exit method field in state-persistence.md):
- `STOP_TRIGGERED`
- `TARGET_REACHED`
- `PM_DECISION`
- `TIME_EXPIRED`
- `MARGIN_LIQUIDATION`
- `FORCED_BUY_IN`
- `CORPORATE_ACTION_CASH_MERGER`

`PositionOpenMechanism` (per `position_opened` event detail, mechanism field):
- `ORDER_FILL`
- `SPIN_OFF_FROM_PARENT`

`OrderRejectionSource` (per `order_rejected` detail, rejection source field):
- `GUARDRAIL`
- `BROKER`

`CashDebitReason` (per `cash_debited` detail, reason field in state-persistence.md):
- `ENTRY_FILL`
- `FEES`
- `CASH_DIVIDEND_SHORT_OBLIGATION`

`CashCreditReason` (per `cash_credited` detail, reason field):
- `EXIT_FILL`
- `CASH_DIVIDEND_LONG`
- `FRACTIONAL_SHARE_CASH_OUT`
- `CASH_MERGER_PROCEEDS`

`CorporateActionType` (per corporate-actions.md per-action-type matrix):
- `SPLIT`
- `REVERSE_SPLIT`
- `STOCK_DIVIDEND`
- `CASH_DIVIDEND_LONG`
- `CASH_DIVIDEND_SHORT`
- `CASH_MERGER`
- `STOCK_MERGER`
- `SPIN_OFF`
- `SYMBOL_CHANGE`

`PMVerdict` (per state-persistence.md pm_decision detail, verdict field):
- `APPROVE`
- `APPROVE_WITH_MODIFICATION`
- `REJECT`

`BracketModificationSource` (per `bracket_modified` detail, source field):
- `PM`
- `FILL_ANCHOR_RECALCULATION`
- `CORPORATE_ACTION_ADJUSTMENT`

### 2. Per-event-type detail-payload classes

All are Pydantic v2 `BaseModel` with `model_config = {"frozen": True}`. One class per event type.
Import `Any` from `typing` for `dict[str, Any]` and `list[dict[str, Any]]` fields.

**Position lifecycle:**

`PositionOpenedDetail`:
- `ticker: str`
- `direction: str` — typed as `str` to keep this story's dependency graph closed; downstream sibling-enum convergence is a future tightening pass, not a blocker
- `fill_price: float`
- `quantity: float`
- `thesis_id: str | None`
- `bracket_id: str | None`
- `mechanism: PositionOpenMechanism`
- `parent_position_id: str | None` — present when `mechanism == SPIN_OFF_FROM_PARENT`,
  `None` otherwise; enforced by a `model_validator`

`PositionClosedDetail`:
- `exit_method: PositionExitMethod`
- `exit_price: float`
- `realized_pnl_usd: float`
- `thesis_resolution_category: str`

`PositionAddedDetail`:
- `additional_quantity: float`
- `new_average_cost_basis: float`
- `addition_thesis_component_id: str`

`PositionReducedDetail`:
- `reduced_quantity: float`
- `partial_realized_pnl_usd: float`
- `close_rationale_classification: str`

**Order lifecycle:**

`OrderSubmittedDetail`:
- `order_parameters_json: dict[str, Any]`
- `pm_command_id: str`

`OrderFilledDetail`:
- `fill_price: float`
- `fill_quantity: float`
- `slippage: float`
- `fees: float`

`OrderPartiallyFilledDetail`:
- `fill_price: float`
- `fill_quantity: float`
- `remaining_quantity: float`

`OrderCancelledDetail`:
- `cancel_reason: str`
- `filled_quantity_at_cancellation: int`

`OrderExpiredDetail`:
- `filled_quantity_at_expiration: int`

`OrderRejectedDetail`:
- `rejection_reason: str`
- `rejection_source: OrderRejectionSource`

`OrderModifiedDetail`:
- `field_changed: str`
- `old_value: str`
- `new_value: str`
- `pm_rationale: str`

**Bracket events:**

`BracketActivatedDetail`:
- `bracket_id: str`
- `protective_leg_order_ids: tuple[str, ...]`

`BracketCompletedDetail`:
- `triggered_leg_id: str`
- `fill_details_json: dict[str, Any]`

`BracketDissolvedDetail`:
- `cancelled_leg_order_ids: tuple[str, ...]`

`BracketModifiedDetail`:
- `source: BracketModificationSource`
- `field_changed: str`
- `old_value: str`
- `new_value: str`
- `rationale: str | None` — `None` for `FILL_ANCHOR_RECALCULATION`; non-`None` for `PM` and
  `CORPORATE_ACTION_ADJUSTMENT`; enforced by a `model_validator`

`BracketIncompleteWarningDetail`:
- `missing_leg_types: tuple[str, ...]`
- `expected_resolution: str`

`BracketCancelledCorporateActionDetail`:
- `bracket_id: str`
- `cancellation_reason: str`
- `cancelled_leg_order_ids: tuple[str, ...]`

**Thesis events:**

`ThesisCreatedDetail`:
- `thesis_id: str`
- `summary: str`

`ThesisComponentAddedDetail`:
- `component_id: str`
- `component_type: str`

`ThesisComponentUpdatedDetail`:
- `component_id: str`
- `field_changed: str`
- `old_value: str`
- `new_value: str`

`ThesisResolvedDetail`:
- `resolution_category: str`
- `component_outcomes_json: dict[str, str]`

`ThesisStatusChangedDetail`:
- `old_status: str`
- `new_status: str`

**Cash and margin events:**

`CashDebitedDetail`:
- `amount_usd: float`
- `reason: CashDebitReason`
- `new_balance_usd: float`

`CashCreditedDetail`:
- `amount_usd: float`
- `reason: CashCreditReason`
- `new_balance_usd: float`

`CapitalReservedDetail`:
- `order_id: str`
- `amount_usd: float`

`CapitalReleasedDetail`:
- `order_id: str`
- `amount_usd: float`

`MarginCallDetail`:
- `position_id: str`
- `margin_required_usd: float`
- `margin_available_usd: float`
- `deficit_usd: float`

`MarginCallResolvedDetail`:
- `resolution_method: str`

`MarginLiquidationDetail`:
- `position_id: str`
- `liquidation_price: float`
- `loss_usd: float`

**Risk and guardrail events:**

`GuardrailRejectionDetail`:
- `command_summary: str`
- `blocking_rule_ids: tuple[str, ...]`
- `current_limit_values_json: dict[str, float]`
- `headroom_json: dict[str, float]`
- `suggested_modification: str | None`

`RiskLimitApproachedDetail`:
- `metric_id: str`
- `current_value: float`
- `threshold_value: float`
- `limit_value: float`

`RiskParameterChangedDetail`:
- `old_parameter_set_json: dict[str, Any]`
- `new_parameter_set_json: dict[str, Any]`
- `regime_label: str`

**PM decision events:**

`PMDecisionDetail`:
- `envelope_id: str`
- `source_provenance_json: dict[str, Any]` — carries source type (`pm_analyst` /
  `pm_strategist`), source recommendation ID, recommendation type, and position ID when applicable;
  opaque dict here because the full envelope schema lives in
  `docs/design/04-decision-layer/pm-envelope-schema.md`; subsequent stories may introduce sharper
  inner types without breaking this contract
- `evaluation_json: dict[str, Any]` — carries verdict, pass/fail criteria, concerns, rationale
  narrative; schema varies by source type per portfolio-manager.md
- `modifications_json: list[dict[str, Any]]` — zero or more modification records; each carries
  field, original value, approved value, adjustment category, per-modification rationale
- `resulting_command_ids: tuple[str, ...]` — empty for rejections and holds
- `verdict: PMVerdict`

`CommandAbandonedDetail`:
- `envelope_id: str`
- `command_id: str`
- `originating_agent: str`
- `failure_reason: str`
- `retry_attempt_count: int`

**Corporate action events:**

`CorporateActionAppliedDetail`:
- `action_type: CorporateActionType`
- `alpaca_activity_id: str`
- `ticker: str`
- `new_ticker: str | None` — present for `SYMBOL_CHANGE` and `STOCK_MERGER`; `None` otherwise
- `ratio_or_amount: float` — ratio for splits/stock dividends, amount for cash events, allocation
  for spin-offs; as reported by Alpaca
- `pre_action_quantity: float`
- `post_action_quantity: float`
- `pre_action_cost_basis: float`
- `post_action_cost_basis: float`
- `signed_cash_impact_usd: float` — positive for credits, negative for debits, zero for no-cash
  actions
- `parent_position_id: str | None` — present for `SPIN_OFF` (child's record); `None` otherwise
- `resulting_position_status: str`

### 3. Module-level static mappings

Declare both as module-level `dict` constants. They serve as the single source of truth for the
`ActivityLogEntry` validators and for downstream consumers like story 05e's filtering helpers.

**`EVENT_TYPE_TO_DETAIL_CLASS: dict[EventType, type]`** — maps every `EventType` member to its
detail-payload class. Must cover all members with no gaps; a `mypy` `--strict` pass will catch
missing keys if the dict is declared with a comprehensive literal. Example partial:

```python
EVENT_TYPE_TO_DETAIL_CLASS: dict[EventType, type] = {
    EventType.POSITION_OPENED: PositionOpenedDetail,
    EventType.POSITION_CLOSED: PositionClosedDetail,
    # … one entry per EventType member …
}
```

**`EVENT_TYPE_TO_GROUP: dict[EventType, EventGroup]`** — maps every `EventType` member to its
`EventGroup`. Declare alongside `EVENT_TYPE_TO_DETAIL_CLASS`; both must be exhaustive. Example
partial:

```python
EVENT_TYPE_TO_GROUP: dict[EventType, EventGroup] = {
    EventType.POSITION_OPENED: EventGroup.POSITION_LIFECYCLE,
    EventType.POSITION_CLOSED: EventGroup.POSITION_LIFECYCLE,
    # … one entry per EventType member …
}
```

Both dicts are imported and relied upon by story 05e. Keep them at module scope (not inside a class
or function) for straightforward importability.

### 4. `ActivityLogEntry` — the wrapping record

Pydantic v2 `BaseModel`, `model_config = {"frozen": True}`.

Fields:
- `entry_id: str` — monotonically-assigned identifier; non-empty enforced via `Field(min_length=1)`
- `invocation_id: str` — which pipeline invocation this event belongs to; non-empty via
  `Field(min_length=1)`
- `timestamp: datetime` — tz-aware UTC; enforced by a `model_validator` that rejects naive datetimes
- `event_type: EventType`
- `event_group: EventGroup` — derived from `event_type` via `EVENT_TYPE_TO_GROUP`; supplied by the
  caller but validated against the mapping in a `model_validator`; mismatch raises `ValidationError`
  with a message naming both the supplied group and the expected group
- `position_id: str | None`
- `order_id: str | None`
- `thesis_id: str | None`
- `source: EventSource`
- `detail: AnyDetailType` — the discriminated-union field; see below

**Discriminated-union pattern for `detail`:**

Pydantic v2's `Annotated[Union[...], Field(discriminator=...)]` requires a literal discriminator
field shared across all union members. Because this story's detail classes are purposefully
minimal per-type records without a shared `event_type` discriminator field on the payload itself,
use an alternative pattern: declare `detail` as `Any` in the Pydantic model, and enforce the
class-match in a `model_validator(mode="after")` that looks up `EVENT_TYPE_TO_DETAIL_CLASS[self.event_type]`
and checks `isinstance(self.detail, expected_class)`. If the check fails, raise `ValueError` with a
message of the form:

```
detail class mismatch: event_type=<EventType.X: 'x'> expects <ClassName> but got <ActualClassName>
```

This approach keeps each detail class self-contained (no injected discriminator field) while
enforcing the mapping at construction time. Subsequent stories may introduce a shared base class
with a literal `event_type` field if Pydantic's native discriminated-union validation becomes
preferable; no change to callers is required at that point.

**Model validators summary** (all `mode="after"`):

1. `timestamp` is tz-aware UTC — rejects naive datetimes.
2. `event_group` matches `EVENT_TYPE_TO_GROUP[event_type]` — mismatch raises `ValidationError`.
3. `detail` class matches `EVENT_TYPE_TO_DETAIL_CLASS[event_type]` — mismatch raises `ValidationError`
   with the message pattern above.
4. `entry_id` and `invocation_id` are non-empty (handled by `Field(min_length=1)`, no explicit
   validator needed).

### 5. Type alias

Declare a `AnyDetailType` type alias (using `Union[PositionOpenedDetail, PositionClosedDetail, ...]`)
covering all 35 detail-payload classes. This alias is used in the `detail` field annotation and
is exported from the module for downstream importers.

## Depends on

- 02

No dependency on 03a–03d at the Python import level. Event-type detail fields that overlap with
concepts from sibling stories (e.g., `direction` in `PositionOpenedDetail`) use `str` or the
appropriate primitive rather than importing sibling enums, to keep this story's dependency graph
closed. A cross-story convergence pass can tighten these types later without breaking
`ActivityLogEntry`'s contract.

## Out of scope

- Activity log filtering helpers (5a intra-invocation changelog, 5b PM decision log, 5c position
  modification trail) — story 05e.
- Activity log persistence (read/write against the durable store) — execution-layer and
  persistence stories.
- The full inner PM envelope schema as typed Pydantic models — `PMDecisionDetail` stores it as
  `dict[str, Any]` fields; sharpened in subsequent stories once the PM envelope schema story lands.
- Event emission (the write side) — owned by execution-layer modules; this story defines the
  consumer-facing record shape only.
- Any `EventType` member not found by name in state-persistence.md's catalog — per
  `feedback_no_inventing_component_names.md`, no additions without a design-doc anchor.

## Notes

Per `feedback_per_producer_schema.md`, this story declares one detail-payload class per event
type rather than one large union with optional fields across all types. Each OMS subsystem
(fill-processor, command-executor, bracket-manager, margin-monitor, guardrail-layer) emits
distinct event types; the per-type class makes each producer's contract self-contained and legible
without implicit cross-producer coupling.

The `*_json: dict[str, Any]` fields on `PMDecisionDetail`, `GuardrailRejectionDetail`,
`BracketCompletedDetail`, and `RiskParameterChangedDetail` model nested structures whose
authoritative schema lives elsewhere (PM envelope, etc.). Storing them as opaque dicts keeps this
story self-contained. Consumers that need inner shape (e.g., the strategist's PM decision log
slicing in story 05e) receive the raw dict and are responsible for parsing it against the schema
documented in the respective design docs.

The `EVENT_TYPE_TO_DETAIL_CLASS` and `EVENT_TYPE_TO_GROUP` dicts are the load-bearing contracts for
both the `ActivityLogEntry` validators in this story and the filtering helpers in story 05e. Keep
them exhaustive and at module scope; the `mypy` pass will surface any `EventType` member not covered
when the dicts are annotated `dict[EventType, ...]`.

Per `feedback_avoid_numeric_anchors.md`, no judgmental thresholds or target numbers appear in field
docstrings. Fields carry the structured payload the design specifies; interpretation is the
consumer's concern.

The `PositionOpenedDetail.parent_position_id` validator enforces the design's semantic: the field is
meaningful only for spin-off children (`mechanism == SPIN_OFF_FROM_PARENT`). Callers constructing
an `order_fill`-mechanism entry with a non-`None` `parent_position_id` receive a `ValidationError`.

`BracketModifiedDetail.rationale` follows the same pattern: `None` is valid only when
`source == FILL_ANCHOR_RECALCULATION`; the design states the fill-anchor recalculation is
deterministic and self-describing, requiring no separate rationale narrative.

## Acceptance criteria

- [ ] `EventGroup`, `EventType`, `EventSource`, `PositionExitMethod`, `PositionOpenMechanism`,
      `OrderRejectionSource`, `CashDebitReason`, `CashCreditReason`, `CorporateActionType`,
      `PMVerdict`, and `BracketModificationSource` are declared as `StrEnum` with the documented
      members and no extras.
- [ ] `EventType` contains exactly one member per event in state-persistence.md's catalog; no
      member is invented.
- [ ] Each per-event-type detail-payload class is a frozen Pydantic v2 model with the documented
      fields and types.
- [ ] `PositionOpenedDetail` rejects `parent_position_id != None` when
      `mechanism == ORDER_FILL`.
- [ ] `BracketModifiedDetail` rejects `rationale == None` when
      `source != FILL_ANCHOR_RECALCULATION`.
- [ ] `EVENT_TYPE_TO_DETAIL_CLASS` covers every member of `EventType` with no gaps; `mypy` confirms
      no missing keys under `--strict`.
- [ ] `EVENT_TYPE_TO_GROUP` covers every member of `EventType` with no gaps; `mypy` confirms no
      missing keys under `--strict`.
- [ ] `ActivityLogEntry` rejects a mismatched `detail` / `event_type` pair with a
      `ValidationError` message naming both the expected and actual class names.
- [ ] `ActivityLogEntry` rejects a mismatched `event_group` / `event_type` pair with a
      `ValidationError` message naming both the supplied and expected group.
- [ ] `ActivityLogEntry` rejects a naive (tz-unaware) `timestamp` with a `ValidationError`.
- [ ] `ActivityLogEntry` rejects an empty `entry_id` or empty `invocation_id`.
- [ ] One happy-path test fixture per event type round-trips through Pydantic without
      `ValidationError` — 35 fixtures, one per detail-payload class.
- [ ] A mismatched-detail fixture (e.g., `PositionClosedDetail` passed alongside
      `EventType.ORDER_FILLED`) is rejected; the `ValidationError` message references both
      the expected class name and the actual class name.
- [ ] `AnyDetailType` alias is exported and covers all 35 detail-payload classes.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
