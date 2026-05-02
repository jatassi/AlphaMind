---
status: done
completed_date: 2026-04-29
commit_id: f95cad2
---

# 06 — Engine-originated envelope assembler

## Goal

Land the deterministic primitive that composes a complete `EngineEnvelope` from a position-selection result, a secondary-breach check, and the breach context. Produces the typed object the engine envelope JSON Schema enforces — one envelope per protective CLOSE trigger, carrying exactly one CLOSE command with `engine_guardrail` provenance, the full guardrail trigger record, and (optionally) cascade linkage. Includes the `MON.{session}.{trigger}.{ordinal}` ID format helpers for both `envelope_id` and embedded `command_id`.

This primitive is what the continuous monitor invokes after detecting a breach classified as `immediate_engine`: select position (story 04d) → check secondary breach (story 05b) → compose envelope (this story) → submit to OMS. Story 07 (margin cascade) calls this primitive once per cascade step.

## Reading

- `docs/design/05-execution-layer/engine-envelope-schema.md` — the formal JSON Schema this primitive produces. Full required-fields list, ID patterns, cross-field invariants (top-level `trigger_timestamp` matches embedded record's; `command_id` starts with `{envelope_id}.`), the single-CLOSE-per-envelope constraint, and the discipline that `position_selection_rationale` must name a documented selection rule from `breach-behavior.md`.
- `docs/design/oms-command-ids.md` — the engine-originated command ID format `MON.{monitor_session_id}.{trigger_id}.{ordinal}`:
  > MON.{monitor_session_id} — the continuous monitor's session ID (assigned at monitor process start; new monitor session ⇒ new session ID).
  > {trigger_id} — monotonically increasing within a session, starting from 1; assigned in receipt order; out-of-order or gapped IDs are a structural error.
  > {ordinal} — 1-indexed ordinal within the trigger's commands; engine-originated envelopes always carry exactly one command, so ordinal is always "1".
- `docs/design/06-risk-guardrails/breach-behavior.md` § Traceability for engine-originated actions — confirms the envelope's payload structure and the close-rationale-type discipline:
  > Stored in the activity log with the same structure as PM-originated envelopes. Thesis resolution carries `engine_guardrail` provenance so the feedback loop can segment engine-originated closures in outcome analysis.
- `docs/design/06-risk-guardrails/breach-behavior.md` § Forced reduction policy — Position selection logic — confirms the position_selection_rationale discipline (each rationale string names the deterministic selection rule).
- `src/alphamind/risk_guardrails/breach_behavior/types.py` — `EngineEnvelope`, `EngineCloseCommand`, `EngineGuardrailTriggerRecord`, `BreachDetails`, `SecondaryBreachCheckResult`, `PositionSelectionResult`, `PositionSelectionAction`, `RegimeLabel`, `CloseRationaleType`, `RiskManagementSubtype` (story 03).
- `src/alphamind/risk_guardrails/breach_behavior/position_selection.py` — `PositionSelectionResult` shape (story 04d).
- `src/alphamind/risk_guardrails/breach_behavior/secondary_breach.py` — `SecondaryBreachCheckResult` (story 05b).
- `docs/design/06-risk-guardrails/scenario-tests.md` § A6, A7 — concrete walkthroughs the assembler must support (single envelope and multi-envelope cascade).

## Depends on

- 02 (package skeleton)
- 03 (canonical types — every envelope component)
- 04d (position selection — for `PositionSelectionResult`)
- 05b (secondary breach check — for `SecondaryBreachCheckResult`)

## Scope

In scope, all under `src/alphamind/risk_guardrails/breach_behavior/engine_envelope.py`. Tests at `tests/risk_guardrails/breach_behavior/test_engine_envelope.py`.

### 1. ID generation helpers

```python
def envelope_id_for(*, monitor_session_id: str, trigger_id: int) -> str:
    """Return the canonical envelope_id 'MON.{session}.{trigger}'.

    Args:
        monitor_session_id: The monitor process's session ID. Must be a non-empty string
            containing no '.' characters (the dot is the field separator).
        trigger_id: 1-indexed monotonic trigger ID within the session. Must be ≥ 1.

    Raises:
        ValueError: when monitor_session_id is empty or contains '.', or when trigger_id < 1.
    """


def command_id_for(*, envelope_id: str, ordinal: int = 1) -> str:
    """Return the canonical command_id '{envelope_id}.{ordinal}'.

    Engine-originated envelopes always carry exactly one command, so ordinal is always 1
    by default. The argument is exposed for future-proofing if the schema's single-command
    invariant ever changes.

    Args:
        envelope_id: The envelope_id this command belongs to. Must match the engine
            envelope schema's pattern.
        ordinal: 1-indexed ordinal. Must be ≥ 1.

    Raises:
        ValueError: when envelope_id does not match the documented pattern, or ordinal < 1.
    """
```

### 2. Trigger record assembler

```python
def compose_guardrail_trigger_record(
    *,
    rule_breached: str,
    trigger_timestamp: datetime,
    breach_details: BreachDetails,
    position_selection: PositionSelectionResult,
    cascade_id: str | None = None,
    secondary_breach_check: SecondaryBreachCheckResult | None = None,
) -> EngineGuardrailTriggerRecord:
    """Assemble the EngineGuardrailTriggerRecord from breach context inputs.

    Args:
        rule_breached: Canonical rule ID from guardrails.yaml (e.g., "position_max_loss_equity_pct",
            "sector_concentration_pct"). Must be non-empty.
        trigger_timestamp: When the breach was detected. Tz-aware.
        breach_details: BreachDetails value carrying current/limit/overage values.
        position_selection: The selection result naming the position chosen and the rationale.
        cascade_id: Present when this trigger is part of a cascade chain (margin call → forced
            reduction; primary breach → secondary breach). None for standalone triggers.
        secondary_breach_check: Present when the position selection required a secondary-breach
            check (per oms-commands.md § Command origins). None when no secondary check was performed
            (e.g., position-level max loss closes the breaching position only — no cross-rule risk).

    Returns:
        A frozen EngineGuardrailTriggerRecord with position_selection.rationale flowing into
        the position_selection_rationale field.

    Raises:
        ValueError: when rule_breached is empty or when trigger_timestamp is naive.
    """
```

### 3. Envelope assembler — full

```python
def compose_engine_envelope(
    *,
    monitor_session_id: str,
    trigger_id: int,
    trigger_timestamp: datetime,
    rule_breached: str,
    breach_details: BreachDetails,
    position_selection: PositionSelectionResult,
    cascade_id: str | None = None,
    secondary_breach_check: SecondaryBreachCheckResult | None = None,
    execution_method: Literal["market", "limit"] = "market",
    limit_price: float | None = None,
) -> EngineEnvelope:
    """Compose a complete EngineEnvelope from breach + selection inputs.

    Steps:
      1. Generate envelope_id via envelope_id_for(...).
      2. Generate command_id via command_id_for(envelope_id, ordinal=1).
      3. Translate position_selection.action into the embedded EngineCloseCommand:
         - PositionSelectionAction.FULL_CLOSE → EngineCloseCommand.quantity_or_all = "all"
         - PositionSelectionAction.PARTIAL_TRIM → EngineCloseCommand.quantity_or_all =
           the trim's *post-action* size (the position will hold that much after the trim;
           the close size = pre-trim size - target post-action size; the OMS receives a CLOSE
           with the close-quantity computed by the engine from the position's current size and
           the post-action target).
      4. Compose the trigger record via compose_guardrail_trigger_record(...).
      5. Construct the EngineEnvelope; the post-validators on EngineEnvelope verify ID-pattern
         match, command_id-starts-with-envelope_id, and trigger_timestamp consistency.

    Args:
        monitor_session_id, trigger_id, trigger_timestamp: ID-generation inputs.
        rule_breached, breach_details, position_selection: trigger record inputs.
        cascade_id, secondary_breach_check: optional trigger record fields.
        execution_method, limit_price: close command inputs. Default "market" with no limit.

    Returns:
        A fully-validated EngineEnvelope ready for submission.

    Raises:
        ValueError: on any input validation failure or post-validator violation.
    """
```

### 4. Partial-trim quantity semantics

For partial trims, `EngineCloseCommand.quantity_or_all` is a positive `float` representing the *post-action target size*, NOT the close quantity. Rationale:

The OMS-side close-command schema accepts either a quantity-to-close or a target-post-close-size; the design selects target-post-close because position-selection's `PositionSelectionResult.target_post_action_size_pct_of_portfolio` already names the target. The OMS computes the close-quantity = pre-action-size - target-post-action-size at submission time.

For partial trims, the assembler reads:
- `position_selection.target_post_action_size_pct_of_portfolio` — the percentage target.
- The current position's size (in shares/contracts and dollars) to convert percentage to absolute quantity. The position is identified by `position_selection.position_id`; the assembler looks up via a `positions_by_id: dict[str, PositionRecord]` parameter (not part of `PositionSelectionResult` to avoid circular references; the caller provides the lookup mapping).

Updated signature for `compose_engine_envelope`:

```python
def compose_engine_envelope(
    *,
    monitor_session_id: str,
    trigger_id: int,
    trigger_timestamp: datetime,
    rule_breached: str,
    breach_details: BreachDetails,
    position_selection: PositionSelectionResult,
    positions_by_id: dict[str, PositionRecord],          # added
    portfolio_value_usd: float,                          # added — converts pct to absolute
    cascade_id: str | None = None,
    secondary_breach_check: SecondaryBreachCheckResult | None = None,
    execution_method: Literal["market", "limit"] = "market",
    limit_price: float | None = None,
) -> EngineEnvelope:
```

The assembler computes:
- For FULL_CLOSE: `quantity_or_all = "all"`.
- For PARTIAL_TRIM:
  - `target_post_action_usd = position_selection.target_post_action_size_pct_of_portfolio / 100.0 × portfolio_value_usd`
  - `current_size_usd = positions_by_id[position_selection.position_id].size_usd` (or whatever the canonical position-record field name is — the implementing subagent reads `PositionRecord` to confirm).
  - `close_quantity_usd = current_size_usd - target_post_action_usd`
  - The OMS-side close command typically takes shares/contracts; the assembler converts USD to shares/contracts using the position's per-unit price.
  - Wait — this is getting more complex than the design's "engine envelope assembler" wants to be. **Resolution:** the assembler emits `quantity_or_all = target_post_action_size_usd` (a positive float in USD), not the close quantity. The downstream OMS interprets the field per its own contract: for engine-originated CLOSE on a partial trim, the float is the *post-trim target USD size of the position*, and the OMS computes close-quantity. This decouples the assembler from share-vs-contract conversions.

For test simplicity, the assembler's initial implementation uses USD as the unit for `quantity_or_all` in PARTIAL_TRIM cases. The OMS-side schema (execution-layer work tree) finalizes the unit convention; if the eventual decision is shares/contracts, this story's assembler is a coordinated edit (small mechanical change). The orchestrator notes the follow-up.

### 5. Tests

Tests at `tests/risk_guardrails/breach_behavior/test_engine_envelope.py`:

#### ID helpers

- **`envelope_id_for` happy path:** `monitor_session_id="abc123"`, `trigger_id=1` → `"MON.abc123.1"`.
- **`envelope_id_for` higher trigger_id:** `trigger_id=42` → `"MON.abc123.42"`.
- **`envelope_id_for` rejects empty session ID:** `monitor_session_id=""` → `ValueError`.
- **`envelope_id_for` rejects session ID containing dot:** `monitor_session_id="a.b"` → `ValueError`.
- **`envelope_id_for` rejects trigger_id < 1:** `trigger_id=0` → `ValueError`.
- **`command_id_for` happy path:** `envelope_id="MON.s.1"`, `ordinal=1` → `"MON.s.1.1"`.
- **`command_id_for` higher ordinal:** `ordinal=3` → `"MON.s.1.3"`.
- **`command_id_for` rejects ordinal < 1:** `ValueError`.
- **`command_id_for` rejects malformed envelope_id:** `envelope_id="PM.s.1"` (wrong prefix) → `ValueError`.

#### Trigger record assembler

- **Happy path standalone:** all fields populated, `cascade_id=None`, `secondary_breach_check=None` → returns frozen `EngineGuardrailTriggerRecord` with the documented fields.
- **Cascade context:** `cascade_id="cascade-abc"` → record carries the cascade ID.
- **Secondary breach result:** `secondary_breach_check=SecondaryBreachCheckResult(result=DEFERRED_TO_PM, notes="...")` → record carries the result.
- **Empty `rule_breached` rejected:** `ValueError`.
- **Naive `trigger_timestamp` rejected:** `ValueError`.
- **`position_selection.rationale` flows into `position_selection_rationale`:** verified by string equality.

#### Full envelope assembler

- **A6 reproduction — full close:** monitor_session_id="s1", trigger_id=1, position-level max loss breach on POS-MARA-001 (40% loss on short, full close). Output envelope:
  - `envelope_id = "MON.s1.1"`
  - `trigger_timestamp` matches input (tz-aware UTC)
  - `source_provenance = "engine_guardrail"`
  - `guardrail_trigger_record.rule_breached = "position_max_loss_equity_pct"`
  - `guardrail_trigger_record.position_selection_rationale` matches the position-selection result's rationale string
  - `command.command_id = "MON.s1.1.1"`
  - `command.command_type = "close"`
  - `command.close_rationale_type = "risk_management"`
  - `command.risk_management_subtype = "engine_guardrail"`
  - `command.position_id = "POS-MARA-001"`
  - `command.quantity_or_all = "all"` (FULL_CLOSE)
  - `command.execution_method = "market"`
- **A7 reproduction — partial trim:** total short exposure breach; PARTIAL_TRIM action with `target_post_action_size_pct_of_portfolio = 2.5`. Output:
  - `command.quantity_or_all = 2500.0` (USD; portfolio value $100K × 2.5%)
- **Cascade — second envelope in chain:** `cascade_id="cascade-abc"`, `trigger_id=2`. Output:
  - `envelope_id = "MON.s1.2"` (different trigger ID from the first)
  - `guardrail_trigger_record.cascade_id = "cascade-abc"` (shared with the first)
- **Secondary breach avoided:** `secondary_breach_check.result = SECONDARY_BREACH_AVOIDED`. Envelope's trigger record carries the result; the envelope is otherwise identical in structure.
- **Limit-price execution:** `execution_method="limit"`, `limit_price=18.50` → `command.execution_method="limit"`, `command.limit_price=18.50`.

#### Cross-field invariants (post-validator coverage)

- **`trigger_timestamp` mismatch rejected:** if the assembler is bypassed and `EngineEnvelope` is constructed directly with a top-level `trigger_timestamp` differing from the embedded `guardrail_trigger_record.trigger_timestamp`, the model raises `ValidationError`. (Tests construct directly to verify the type's invariant; the assembler always sets them equal.)
- **`command_id` not starting with `{envelope_id}.` rejected:** `ValidationError`.
- **`envelope_id` pattern violation rejected:** `ValidationError`.

#### Validation

- **Position not in `positions_by_id` raises:** `position_selection.position_id` not in the dict → `ValueError` naming the missing position.
- **`portfolio_value_usd <= 0` raises:** `ValueError` (zero or negative portfolio is structurally impossible).
- **`limit_price` required for `execution_method="limit"`:** `EngineCloseCommand` post-validator catches; assembler propagates as `ValueError`.

#### Determinism

- **Pure function:** repeated calls with identical inputs produce equal envelopes.
- **Frozen output:** assigning to envelope fields raises `ValidationError`.

Out of scope:
- The continuous monitor's session-ID assignment and trigger-ID monotonicity tracking — those are session-state concerns the monitor owns.
- Submitting the envelope to the OMS — the engine envelope is the typed output; submission is execution-layer territory.
- Activity log writing (the engine envelope persists alongside PM-originated envelopes) — execution-layer.
- Decoding the JSON Schema's `commands` single-element-array shape from the Python-side scalar — the JSON-schema-compliant serializer is a follow-up if/when the envelope is round-tripped through JSON. The Python-side type is the assembler's output.

## Notes

**Why the assembler computes `quantity_or_all` rather than letting the OMS compute close-quantity from a target-post-action-size field.** The engine-envelope JSON Schema has a single `command` field whose shape is determined by the OMS command schema. The schema's `quantity_or_all` field is the canonical place to encode either `"all"` or a numeric quantity. The assembler is the right layer to translate the position-selection's percentage target into the schema-compliant value because the assembler has the portfolio context (portfolio value, current position size) at hand. Pushing the translation into the OMS would scatter the same arithmetic across N callers; centralizing it in the assembler keeps the OMS contract simple.

**Why USD as the partial-trim unit.** Position-selection's `target_post_action_size_pct_of_portfolio` is a percentage of portfolio value. Converting to USD requires only the portfolio value; converting to shares/contracts requires the per-unit price (and for options, the contract multiplier). USD is the lowest-coupling encoding for this story; the OMS-side decoding to shares/contracts is its own primitive in the execution-layer work tree. If the eventual OMS schema decides shares/contracts, this story's assembler does a small coordinated edit.

**Why `command_id_for` exposes an `ordinal` parameter despite always being `1`.** Future-proofing — if the engine envelope schema ever relaxes the single-command constraint, the function signature is already correct. Per `feedback_simplify_before_building.md`, keeping the parameter is cheap (default value) and avoids a future breaking-change story.

**Per `feedback_no_inventing_component_names.md`,** function names match the engine-envelope-schema's $defs and the design's terminology: `compose_guardrail_trigger_record` ↔ `guardrail_trigger_record`; `compose_engine_envelope` ↔ "Engine-originated command envelope". `envelope_id_for` and `command_id_for` are package-internal helpers naming the IDs they construct.

**Per `feedback_per_producer_schema.md`,** `EngineEnvelope` has a single producer (this primitive). The continuous monitor and the cascade orchestrator (story 07) both call this primitive; neither builds the envelope by hand.

**Cascade composition.** Story 07 calls this primitive once per cascade step. Each call uses an incrementing `trigger_id` (so each step's `envelope_id` is unique within the session) and the shared `cascade_id` (so the cascade chain is linkable). The assembler does not own the cascade ID generation — that's story 07's domain. The assembler simply propagates whatever `cascade_id` the caller provides.

## Acceptance criteria

- [ ] `src/alphamind/risk_guardrails/breach_behavior/engine_envelope.py` exists and defines `envelope_id_for`, `command_id_for`, `compose_guardrail_trigger_record`, `compose_engine_envelope`.
- [ ] All four functions are re-exported from `src/alphamind/risk_guardrails/breach_behavior/__init__.py`.
- [ ] All four functions are pure (no I/O, no clock reads except input).
- [ ] `envelope_id_for("abc123", 1)` returns `"MON.abc123.1"`; `("abc123", 42)` returns `"MON.abc123.42"`.
- [ ] `envelope_id_for` rejects empty session ID, dot-containing session ID, and `trigger_id < 1` with `ValueError`.
- [ ] `command_id_for("MON.s.1", 1)` returns `"MON.s.1.1"`; `("MON.s.1", 3)` returns `"MON.s.1.3"`.
- [ ] `command_id_for` rejects malformed envelope_id and `ordinal < 1`.
- [ ] Trigger record assembler returns frozen `EngineGuardrailTriggerRecord` with all input fields populated; `position_selection_rationale` matches `position_selection.rationale`.
- [ ] Trigger record assembler rejects empty `rule_breached` and naive `trigger_timestamp`.
- [ ] Full envelope FULL_CLOSE case produces `command.quantity_or_all = "all"`.
- [ ] Full envelope PARTIAL_TRIM case produces `command.quantity_or_all` equal to `target_post_action_size_pct_of_portfolio / 100 × portfolio_value_usd` (USD float).
- [ ] Cascade chain: two envelopes with `trigger_id=1` and `trigger_id=2` produce distinct `envelope_id`s; both share the same `cascade_id` from the input.
- [ ] All envelope post-validators (envelope_id pattern, command_id starts-with-envelope_id, trigger_timestamp consistency) pass for all assembler outputs (no `ValidationError` from the assembler's own outputs in any test case).
- [ ] Position not in `positions_by_id` raises `ValueError` naming the missing position_id.
- [ ] Zero or negative `portfolio_value_usd` raises `ValueError`.
- [ ] Limit-price execution: `execution_method="limit"`, `limit_price=X` produces `command.execution_method="limit"`, `command.limit_price=X`.
- [ ] Returned envelope is frozen — assigning to fields raises `ValidationError`.
- [ ] Determinism: 100 repeated calls with identical inputs produce equal envelopes.
- [ ] A6 worked example (POS-MARA-001 max-loss full close on a short) produces the documented envelope structure (verified field-by-field).
- [ ] A7 worked example (cascade with shared cascade_id; first envelope is the margin call → forced reduction; second is the post-liquidation re-evaluation if any secondary breach surfaced) — verified by a parametrized test reproducing both envelopes.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
