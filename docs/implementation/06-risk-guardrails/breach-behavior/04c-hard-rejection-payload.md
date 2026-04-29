---
status: not_started
completed_date:
commit_id:
---

# 04c — Hard rejection payload assembler

## Goal

Land the deterministic primitive that composes a `HardRejectionPayload` from a guardrail-evaluation `LibraryOutput` (the per-rule projection result) and a rejected command identifier. The payload is what T3 returns synchronously to the PM when one or more rules fail under a proposed command — per `breach-behavior.md § Hard rejection semantics`. Carries every breaching rule's current/limit/projected/overage, a PM-actionable `suggested_modification` string, and the projected `headroom_after_hypothetical_compliance` so the PM sees the cure cost.

## Reading

- `docs/design/06-risk-guardrails/breach-behavior.md` § Hard rejection semantics — the authoritative payload contract:
  > - Rule(s) breached: which guardrail(s) blocked the command
  > - Current state: current value for each breaching rule
  > - Limit: the active limit
  > - Overage: how much the command would exceed the limit
  > - Suggested modification: mechanical compliance suggestion (e.g., "reduce size by 42%"). Starting point, not binding.
  > - Headroom after hypothetical compliance: headroom if the PM adopts the suggestion
- `docs/design/05-execution-layer/oms-commands.md` § Rejection handling — the synchronous rejection contract; the engine returns the payload to the PM within the same invocation; the PM may adjust and reissue.
- `docs/design/04-decision-layer/portfolio-manager.md` § Synchronous command feedback — confirms the PM's reissue path consumes the payload directly; the `suggested_modification` is a starting point and the PM retains agency to deviate.
- `docs/implementation/06-risk-guardrails/guardrail-evaluation/05-evaluate-proposals-entry-point.md` — the upstream library's entry point. Its `LibraryOutput` carries `per_rule: tuple[RuleProjection, ...]`; the `RuleProjection` shape is `{rule, status, current, limit, projected_after, headroom_remaining, unit}` — fields this story consumes verbatim.
- `docs/implementation/06-risk-guardrails/guardrail-evaluation/04-projection-engine-and-rule-contributions.md` — the projection engine's `RuleProjection` model and `Status` enum (PASS / WARNING / FAIL); breaching rules are those with `status == FAIL`.
- `src/alphamind/risk_guardrails/breach_behavior/types.py` — `HardRejectionPayload` and `RejectionRuleEntry` (story 03).

## Depends on

- 02 (package skeleton)
- 03 (canonical types — `HardRejectionPayload`, `RejectionRuleEntry`)
- Cross-feature (advisory, not blocking): the guardrail-evaluation library's `RuleProjection` and `LibraryOutput` shapes. The function consumes these via duck-typed Protocols defined in this story; if the upstream types are not yet shipped, the implementing subagent uses Protocols and provides a stub. When guardrail-evaluation lands, the Protocols match the production shapes and no rewiring is needed.

## Scope

In scope, all under `src/alphamind/risk_guardrails/breach_behavior/hard_rejection.py`. Tests at `tests/risk_guardrails/breach_behavior/test_hard_rejection.py`.

### 1. Public function

```python
def compose_hard_rejection_payload(
    *,
    rejected_command_id: str,
    library_output: LibraryOutputProtocol,
    library_output_after_hypothetical_compliance: LibraryOutputProtocol,
    suggested_modification: str,
) -> HardRejectionPayload:
    """Assemble the synchronous T3 rejection payload returned to the PM.

    Args:
        rejected_command_id: The OMS command ID of the command being rejected (e.g., "PM.{invocation}.{ordinal}.1").
        library_output: The guardrail-evaluation library's output for the *as-submitted* command.
            Must carry at least one per_rule entry with status==FAIL — otherwise the command
            should not have been rejected; ValueError raised.
        library_output_after_hypothetical_compliance: The library's output for the *post-modification*
            command (i.e., the suggested-modification result). Carries the projected per-rule headroom
            the PM sees if it adopts the suggestion.
        suggested_modification: PM-actionable text — e.g., "reduce size by 42%" or
            "switch to a lower-delta strike". Caller-constructed; this primitive does not synthesize.

    Returns:
        A frozen HardRejectionPayload with:
          - rejected_command_id (passed through),
          - breaching_rules: tuple of RejectionRuleEntry, one per FAIL'd rule in library_output.per_rule,
            preserving library_output's iteration order,
          - suggested_modification (passed through),
          - headroom_after_hypothetical_compliance: tuple of RejectionRuleEntry, one per rule in
            library_output_after_hypothetical_compliance.per_rule (every rule, not just the previously
            breaching ones — the PM sees the full headroom picture after the cure).

    Raises:
        ValueError: when library_output.per_rule has zero entries with status==FAIL (no rejection
            warranted), or when suggested_modification is empty/whitespace.
    """
```

### 2. Library-output Protocol

The function consumes guardrail-evaluation's library output via a structural type protocol, decoupling this story from upstream's exact module path:

```python
from typing import Protocol


class RuleProjectionProtocol(Protocol):
    rule: str
    status: str             # one of "PASS", "WARNING", "FAIL"
    current: float
    limit: float
    projected_after: float
    headroom_remaining: float
    unit: str


class LibraryOutputProtocol(Protocol):
    per_rule: tuple[RuleProjectionProtocol, ...]
```

The Protocols match the shipped guardrail-evaluation `RuleProjection` and `LibraryOutput` shapes verbatim. Call sites pass the production types directly without conversion.

### 3. Per-rule entry construction

For each FAIL'd rule in `library_output.per_rule`, construct a `RejectionRuleEntry`:

```python
RejectionRuleEntry(
    rule_id=rule_projection.rule,
    current_value=rule_projection.current,
    limit_value=rule_projection.limit,
    projected_after=rule_projection.projected_after,
    overage=rule_projection.projected_after - rule_projection.limit,  # always positive for FAIL
    headroom_remaining=rule_projection.headroom_remaining,            # negative for FAIL (over-limit)
    unit=rule_projection.unit,
)
```

For the post-compliance set, every rule (not just previously breaching ones) becomes a `RejectionRuleEntry`:

```python
RejectionRuleEntry(
    rule_id=rule_projection.rule,
    current_value=rule_projection.current,
    limit_value=rule_projection.limit,
    projected_after=rule_projection.projected_after,
    overage=max(0.0, rule_projection.projected_after - rule_projection.limit),  # 0 when within limit
    headroom_remaining=rule_projection.headroom_remaining,
    unit=rule_projection.unit,
)
```

The `overage` semantics differ slightly between the two sets:
- In `breaching_rules`, `overage = projected_after - limit` is always positive (FAIL by definition).
- In `headroom_after_hypothetical_compliance`, `overage = max(0.0, projected_after - limit)` zeros out cured rules; only any *remaining* over-limit rules carry positive overage. (The PM uses this to confirm the suggested modification fully cures, or to flag a partial cure for follow-up.)

### 4. Tests

Tests at `tests/risk_guardrails/breach_behavior/test_hard_rejection.py`:

#### Happy paths

- **Single-rule rejection — sector concentration:** `library_output.per_rule = [PASS for net_long, FAIL for sector_concentration_tech]`. Output payload has one breaching rule (sector_concentration_tech) with the expected current/limit/projected/overage/headroom values; suggested modification surfaces verbatim; the post-compliance set carries both rules with the cured sector showing `overage=0.0`.
- **Multi-rule rejection — options delta + theta:** `library_output.per_rule = [FAIL for options_delta, FAIL for portfolio_theta, PASS for vega]`. Output carries two breaching rules in the input's iteration order; post-compliance set carries all three.
- **Iteration order preserved:** for a `library_output.per_rule` of 5 rules with FAIL at indices 1, 3, the output `breaching_rules` tuple has the rule at index 1 first, then the rule at index 3. (Caller can depend on the order matching the projection engine's stable rule-spec order.)
- **Post-compliance set is full per_rule:** even rules that were PASS in the rejection (and therefore not in `breaching_rules`) appear in `headroom_after_hypothetical_compliance` so the PM sees the headroom of every rule after the cure.
- **Cured rule shows overage=0 in post-compliance set:** an input where the post-compliance projection moves a previously-FAIL'd rule's `projected_after` to *equal* its limit (consumption=100%) has `overage=0.0` and `headroom_remaining=0.0` in the post-compliance entry. (Boundary inclusivity matches `classify_zone`'s 95%-of-limit hard-block lower bound — exactly at limit is BLOCKED but `overage=0`. The PM sees this as "exactly cured.")
- **Partial cure surfaced:** an input where the suggested modification cures sector but leaves theta still over-limit (post-compliance projection has theta `projected_after > limit`) results in the post-compliance entry for theta having `overage > 0` — the PM sees the suggestion does not fully comply and can adjust further.

#### Validation

- **No FAIL'd rules in input — raises:** `library_output.per_rule = [PASS, PASS, WARNING]` raises `ValueError` whose message names "no rules failed; rejection payload requires at least one FAIL".
- **Empty suggested_modification — raises:** `suggested_modification=""` and `suggested_modification="   "` (whitespace only) both raise `ValueError`.
- **Empty per_rule in either output — raises:** `library_output.per_rule = ()` raises `ValueError` (cannot fail a non-existent rule); same for `library_output_after_hypothetical_compliance.per_rule = ()` (the PM needs the post-compliance picture).

#### Determinism and shape

- **Pure function:** same inputs produce equal outputs (frozen Pydantic equality).
- **Determinism:** repeated calls with identical inputs produce identical payloads.
- **Frozen output:** assigning to a constructed payload's field raises `ValidationError`.

#### Worked example — sector-concentration rejection (informative, validated by a test)

Inputs:
```python
library_output.per_rule = [
    RuleProjection(rule="position_max_size_pct", status="PASS",
                   current=0.0, limit=5.0, projected_after=2.5, headroom_remaining=2.5,
                   unit="% of portfolio"),
    RuleProjection(rule="sector_concentration_tech", status="FAIL",
                   current=23.7, limit=25.0, projected_after=28.2, headroom_remaining=-3.2,
                   unit="% of portfolio (delta-adjusted)"),
    RuleProjection(rule="net_long_pct", status="PASS",
                   current=42.0, limit=60.0, projected_after=44.5, headroom_remaining=15.5,
                   unit="% of portfolio (delta-adjusted)"),
]
library_output_after_hypothetical_compliance.per_rule = [
    RuleProjection(rule="position_max_size_pct", status="PASS",
                   current=0.0, limit=5.0, projected_after=1.4, headroom_remaining=3.6,
                   unit="% of portfolio"),
    RuleProjection(rule="sector_concentration_tech", status="WARNING",
                   current=23.7, limit=25.0, projected_after=25.0, headroom_remaining=0.0,
                   unit="% of portfolio (delta-adjusted)"),
    RuleProjection(rule="net_long_pct", status="PASS",
                   current=42.0, limit=60.0, projected_after=43.4, headroom_remaining=16.6,
                   unit="% of portfolio (delta-adjusted)"),
]
suggested_modification = "reduce size by 42% (from 4.5% to 2.6% of portfolio)"
rejected_command_id = "PM.20260428T100000Z.1.1"
```

Expected output:
```python
HardRejectionPayload(
    rejected_command_id="PM.20260428T100000Z.1.1",
    breaching_rules=(
        RejectionRuleEntry(rule_id="sector_concentration_tech", current_value=23.7,
                           limit_value=25.0, projected_after=28.2, overage=3.2,
                           headroom_remaining=-3.2,
                           unit="% of portfolio (delta-adjusted)"),
    ),
    suggested_modification="reduce size by 42% (from 4.5% to 2.6% of portfolio)",
    headroom_after_hypothetical_compliance=(
        RejectionRuleEntry(rule_id="position_max_size_pct", current_value=0.0,
                           limit_value=5.0, projected_after=1.4, overage=0.0,
                           headroom_remaining=3.6, unit="% of portfolio"),
        RejectionRuleEntry(rule_id="sector_concentration_tech", current_value=23.7,
                           limit_value=25.0, projected_after=25.0, overage=0.0,
                           headroom_remaining=0.0,
                           unit="% of portfolio (delta-adjusted)"),
        RejectionRuleEntry(rule_id="net_long_pct", current_value=42.0,
                           limit_value=60.0, projected_after=43.4, overage=0.0,
                           headroom_remaining=16.6,
                           unit="% of portfolio (delta-adjusted)"),
    ),
)
```

Out of scope:
- Synthesizing the `suggested_modification` text — that's caller-side reasoning. The guardrail-evaluation library may emit a `failure_guidance` field at its wrapper layer (see `state-delivery.md § Guardrail validation tool`); the engine's T3 path constructs its own suggestion or reuses the library's. Either way, this primitive consumes the constructed string verbatim.
- Re-running the projection to compute `library_output_after_hypothetical_compliance` — that's the engine's responsibility (it knows how to apply the suggested modification to the rejected command and re-evaluate). This primitive consumes the second projection as input.
- Logging the rejection or persisting it to the activity log — the engine wraps the primitive's output in its own audit-trail handling.
- Rendering the payload into agent-facing text — the PM consumes the typed object directly per `oms-commands.md § Rejection handling`.

## Notes

**Why a separate compliance-projection input rather than computing it here.** The post-compliance projection requires applying the `suggested_modification` to the original command and re-running guardrail-evaluation. That's the engine's domain — it knows the command's original parameters, how to interpret the modification (size scalar, instrument substitution), and how to invoke the library. This primitive's job is to *compose the payload*, not to drive the modification semantics. Splitting concerns keeps both layers small.

**Why a Protocol rather than importing guardrail-evaluation directly.** Story 04c can run in parallel with — or before — guardrail-evaluation's library entry point landing. Using a structural Protocol decouples the two work trees: this story's tests use stub objects with the documented shape; production wiring uses guardrail-evaluation's real types. Per the state-delivery cross-feature gate convention, the orchestrator surfaces if the upstream types are absent and proceeds with the Protocol stub.

**Per `feedback_simplify_before_building.md`,** the function does one thing: compose the payload from the two projection sets. No suggestion synthesis, no command modification, no re-projection, no logging.

**Per `feedback_no_inventing_component_names.md`,** every field in the payload (`rejected_command_id`, `breaching_rules`, `suggested_modification`, `headroom_after_hypothetical_compliance`) maps directly to the bullet list in `breach-behavior.md § Hard rejection semantics`. The `RejectionRuleEntry` shape mirrors the per-rule projection shape with two name additions (`current_value`, `limit_value` instead of `current`, `limit`) for unambiguous semantics in the rejection context. The added `overage` field is the difference projected_after - limit, named per the design's "Overage" bullet.

**Per `feedback_per_producer_schema.md`,** the `HardRejectionPayload` schema is owned by the breach_behavior package; the engine's T3 layer is the sole producer. Other call sites (e.g., the PM's evaluation framework) consume it but do not produce it.

**Why `overage` is signed differently in the two sets.** In the rejection set, `overage = projected_after - limit` is always positive (the rule failed by construction). In the post-compliance set, `overage = max(0.0, projected_after - limit)` zeros out the cured case, since negative "overage" is meaningless when within the limit. Surfacing positive overage in the post-compliance set lets the PM detect a partial cure (the suggested modification doesn't fully resolve the rule). This is a small but real signal worth preserving.

**Edge case: the suggested modification leaves a *different* rule over-limit.** The PM may see, e.g., a sector cure that pushes options delta over its limit. The post-compliance set surfaces this as `overage > 0` for `options_delta_pct` even though the rule was PASS in the rejection. The primitive does not attempt to detect or warn about the new breach — it surfaces the projected state and trusts the PM to read it. (A future enhancement could add a `new_breaches_introduced: tuple[str, ...]` field to the payload; deferred until the PM's behavioral signal demands it.)

## Acceptance criteria

- [ ] `src/alphamind/risk_guardrails/breach_behavior/hard_rejection.py` exists and defines `compose_hard_rejection_payload` and the two Protocol classes (`RuleProjectionProtocol`, `LibraryOutputProtocol`) with the documented signatures.
- [ ] `compose_hard_rejection_payload` is re-exported from `src/alphamind/risk_guardrails/breach_behavior/__init__.py`.
- [ ] `compose_hard_rejection_payload` is a pure function — no I/O, no global state.
- [ ] Single-rule FAIL rejection produces a payload with one entry in `breaching_rules` matching the FAIL'd rule's projection.
- [ ] Multi-rule FAIL rejection produces a payload with N entries in `breaching_rules` in the input's iteration order.
- [ ] Post-compliance set contains every rule from `library_output_after_hypothetical_compliance.per_rule`, not just previously-breaching ones.
- [ ] Cured rule (post-compliance projected_after ≤ limit) has `overage=0.0` in the post-compliance entry.
- [ ] Partial cure (post-compliance projected_after > limit) has `overage > 0.0` in the post-compliance entry.
- [ ] Boundary case: post-compliance `projected_after = limit` (exactly cured) has `overage=0.0` and `headroom_remaining=0.0`.
- [ ] `library_output.per_rule` containing zero FAIL'd rules raises `ValueError` whose message names the absence of failures.
- [ ] Empty `suggested_modification` (empty string or whitespace-only) raises `ValueError`.
- [ ] Empty `library_output.per_rule` or `library_output_after_hypothetical_compliance.per_rule` raises `ValueError`.
- [ ] Determinism: 100 repeated calls with identical inputs produce identical outputs.
- [ ] Output is frozen: assigning to any field of the returned `HardRejectionPayload` raises `ValidationError`.
- [ ] The worked example in section 4 produces the documented expected output (verified by a parametrized test).
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
