---
status: done
completed_date: 2026-04-29
commit_id: 09cd6f1
---

# 04a — Zone classifier

## Goal

Land the deterministic primitive that maps a rule's current value, limit value, and per-rule escalation zones to one of the four `RiskZone` enum values (NORMAL / WARNING / CRITICAL / BLOCKED). Used everywhere a per-rule zone tag is rendered or evaluated: state-delivery's headroom blocks, the multi-rule-breach emergency trigger evaluator (story 05c), the cumulative drawdown classifier (story 04b), the secondary breach check (story 05b), and the portfolio-state assembler's `RiskBudgetEntry.zone` and `DrawdownState.daily_zone` / `cumulative_zone` populations.

## Reading

- `docs/design/06-risk-guardrails/breach-behavior.md` § Escalation model — the four zones (Normal / Warning / Critical / Hard block), the default 70/85/95 boundaries, and the per-rule overrides for daily drawdown (60/80/90) and cumulative drawdown (50/70/85).
- `docs/design/06-risk-guardrails/state-delivery.md` § Analyst guardrail state header — the `[NORMAL|⚠ WARNING|🔴 CRITICAL|BLOCKED]` zone tag rendered alongside per-rule headroom. State-delivery does not classify; it consumes this story's output.
- `docs/design/configuration-management.md` § `guardrails.yaml` — `escalation_zones: { warning, critical, hard_block }` per rule; the post-validator already enforces strict ordering `warning < critical < hard_block` (see `src/alphamind/config/models/guardrails.py:_escalation_zones_strictly_ordered`).
- `src/alphamind/config/models/guardrails.py` — already-shipped `EscalationZones` model.
- `src/alphamind/portfolio_state/records/capital.py` — `RiskZone` enum (NORMAL, WARNING, CRITICAL, BLOCKED) and `RiskBudgetEntry.zone` field this story's classifier ultimately populates.
- `src/alphamind/risk_guardrails/breach_behavior/types.py` — re-exports `RiskZone` and `EscalationZones`; consumers import from there.
- `docs/implementation/06-risk-guardrails/guardrail-evaluation/04-projection-engine-and-rule-contributions.md` § Projection engine — confirms the projection engine's PASS/WARNING/FAIL three-status output is a strict superset collapse of this classifier's four-zone output (PASS = NORMAL; WARNING = WARNING ∪ CRITICAL; FAIL = BLOCKED). The two layers are complementary: this classifier produces the four-zone label for rendering; the projection engine produces the three-status label for hard-rejection branching.

## Depends on

- 02 (package skeleton)
- 03 (canonical types — for `RiskZone` and `EscalationZones` re-exports)

## Scope

In scope, all under `src/alphamind/risk_guardrails/breach_behavior/zones.py`. Tests at `tests/risk_guardrails/breach_behavior/test_zones.py`.

### 1. Public function

```python
def classify_zone(
    *,
    current_value: float,
    limit_value: float,
    escalation_zones: EscalationZones,
) -> RiskZone:
    """Classify a rule's current value into one of four risk zones.

    consumption_pct = current_value / limit_value × 100, then:
        consumption_pct < zones.warning           → RiskZone.NORMAL
        zones.warning ≤ consumption_pct < zones.critical    → RiskZone.WARNING
        zones.critical ≤ consumption_pct < zones.hard_block → RiskZone.CRITICAL
        consumption_pct ≥ zones.hard_block        → RiskZone.BLOCKED

    Args:
        current_value: The rule's current value. Must be ≥ 0.
        limit_value: The rule's effective limit. Must be > 0.
        escalation_zones: The per-rule escalation zone thresholds (warning, critical, hard_block).

    Returns:
        The RiskZone the current value falls into.

    Raises:
        ValueError: when current_value is negative or limit_value is non-positive.
    """
```

### 2. Classification semantics

- **Boundary semantics — inclusive on the lower bound.** A `current_value` whose consumption percentage is exactly equal to `zones.warning` classifies as `WARNING` (not `NORMAL`). Same for the `CRITICAL` and `BLOCKED` lower bounds. This matches the design's "70–85% of limit ⇒ Warning" and "85–95% ⇒ Critical" and "95–100%+ ⇒ Hard block" wording, where each zone owns its lower bound.
- **Beyond-limit values classify as `BLOCKED`.** A current value exceeding `limit_value` (consumption_pct > 100, e.g., a regime-tightening transition that puts an existing position above the new limit) classifies as `BLOCKED` because it satisfies `consumption_pct ≥ zones.hard_block` for any `hard_block ≤ 100`.
- **Zero current value is `NORMAL`.** `current_value=0, limit_value=any_positive` produces `consumption_pct=0`, classifies as `NORMAL`.
- **Negative `current_value` raises.** Defensive — risk-budget current values are non-negative by construction. A negative value indicates a structural error upstream.
- **Non-positive `limit_value` raises.** Zero or negative limits are blocked by the configuration semantic-self-test; this is a defensive guard. (A zero limit would imply division by zero; a negative limit is meaningless.)

### 3. Examples (informative — not test cases)

```
Default zones (70/85/95):
    current=0,    limit=100 → NORMAL    (consumption 0%)
    current=69.9, limit=100 → NORMAL    (consumption 69.9%)
    current=70,   limit=100 → WARNING   (boundary — inclusive lower bound)
    current=84.9, limit=100 → WARNING
    current=85,   limit=100 → CRITICAL  (boundary)
    current=94.9, limit=100 → CRITICAL
    current=95,   limit=100 → BLOCKED   (boundary)
    current=120,  limit=100 → BLOCKED   (beyond limit)

Daily drawdown zones (60/80/90):
    current=1.4,  limit=2.5 → WARNING   (consumption 56% — just under warning)
    current=1.5,  limit=2.5 → WARNING   (consumption 60% — inclusive boundary)
    current=2.0,  limit=2.5 → CRITICAL  (consumption 80%)
    current=2.25, limit=2.5 → BLOCKED   (consumption 90%)

Cumulative drawdown zones (50/70/85):
    current=4.0,  limit=8.0 → WARNING   (consumption 50%)
    current=5.6,  limit=8.0 → CRITICAL  (consumption 70%)
    current=6.8,  limit=8.0 → BLOCKED   (consumption 85%)
```

### 4. Tests

Tests at `tests/risk_guardrails/breach_behavior/test_zones.py`:

- **NORMAL zone:** `current_value=0`, `current_value=69.9` (just under default warning) — both classify NORMAL.
- **WARNING zone — default thresholds:** `current_value=70` (inclusive lower bound) classifies WARNING; `current_value=84.9` classifies WARNING.
- **CRITICAL zone — default thresholds:** `current_value=85` (boundary) classifies CRITICAL; `current_value=94.9` classifies CRITICAL.
- **BLOCKED zone — default thresholds:** `current_value=95` (boundary) classifies BLOCKED; `current_value=100` classifies BLOCKED; `current_value=120` (beyond limit) classifies BLOCKED.
- **Daily drawdown overrides (60/80/90):** values 1.4, 1.5, 2.0, 2.25 over limit 2.5 produce WARNING, WARNING, CRITICAL, BLOCKED respectively (per the example table above).
- **Cumulative drawdown overrides (50/70/85):** values 4.0, 5.6, 6.8 over limit 8.0 produce WARNING, CRITICAL, BLOCKED respectively.
- **Boundary inclusivity:** for arbitrary `EscalationZones(warning=70, critical=85, hard_block=95)` and `limit_value=100`:
  - `current_value=70.0` → WARNING (inclusive)
  - `current_value=69.999` → NORMAL
  - `current_value=85.0` → CRITICAL (inclusive)
  - `current_value=84.999` → WARNING
  - `current_value=95.0` → BLOCKED (inclusive)
  - `current_value=94.999` → CRITICAL
- **Negative current_value rejected:** `classify_zone(current_value=-1, limit_value=100, escalation_zones=...)` raises `ValueError` with a message naming the offending value.
- **Non-positive limit_value rejected:** `classify_zone(current_value=10, limit_value=0, ...)` and `limit_value=-5` both raise `ValueError`.
- **Determinism:** same inputs produce same output across repeated calls.
- **Pure function:** no I/O, no logging, no clock reads.

Out of scope:
- Joining classified zones with `RiskBudgetEntry` records — that's the rules-and-limits / portfolio-state-assembler work tree's concern.
- Rendering zone labels into agent-facing text (`⚠ WARNING`, `🔴 CRITICAL`) — state-delivery's primitives own that.
- Per-rule escalation zone resolution from `RuleRegistry` — callers pass the resolved `EscalationZones` directly. The rule registry's accessor is shipped by `rules-and-limits/01a-rule-registry.md`.

## Notes

**Why a single classifier rather than per-rule wrappers.** Every rule's escalation thresholds are encoded in its `EscalationZones` config; one general-purpose classifier handles the default 70/85/95 and the per-rule overrides (drawdown rules) uniformly. Adding a new override pattern requires only a YAML edit, no code change.

**Why this lives in breach_behavior rather than guardrail_evaluation.** The projection engine's PASS/WARNING/FAIL output is a three-status compression intended for the engine's hard-rejection branching: PASS → submit, WARNING → submit (advisory), FAIL → reject. The four-zone output is intended for state-delivery's rendering and breach_behavior's downstream primitives (multi-rule breach trigger, cumulative-tier classification, secondary-breach check). Co-locating the four-zone classifier with the rest of breach_behavior's primitives keeps the two compression layers clearly separated. The four-zone output can be losslessly re-derived to the three-status output by any caller that needs it.

**`EscalationZones` Python-type duality.** This story's `classify_zone` consumes `alphamind.config.models.guardrails.EscalationZones` (Pydantic v2; re-exported via `breach_behavior/03`). Guardrail-evaluation's projection engine consumes `alphamind.risk_guardrails.guardrail_evaluation.types.EscalationZones` (frozen dataclass; declared in `guardrail_evaluation/01`). Both encode the same per-rule warning/critical/hard-block percentages from `config/guardrails.yaml`; the duplication accommodates guardrail-evaluation's hash-stability requirement on its `LibraryOutput`. The `from_resolved_config` adapter (`guardrail_evaluation/02c`) is the bridge. Callers composing this classifier with the projection engine pass two different `EscalationZones` instances over the same source data — see `guardrail_evaluation/01` Notes for the parallel-types explanation.

**Per `feedback_simplify_before_building.md`,** the classifier is one function with a single responsibility. No class, no caching, no per-rule registry — callers pass the `EscalationZones` they already have. Story 04b (cumulative drawdown tier) and 05c (multi-rule breach) compose against this primitive; they do not redefine it.

**Per `feedback_no_inventing_component_names.md`,** the function name `classify_zone` and the boundary semantics (zone names, threshold percentages, inclusive lower bound) match the design's `Escalation model` table verbatim. The four `RiskZone` values are re-exported from upstream and used unchanged.

The portfolio-state assembler currently constructs `RiskBudgetEntry` records with `zone: RiskZone` as a passed-in field; the assembler does not yet compute the zone. Once this story lands, the rules-and-limits production-populator story (cross-feature gate; the orchestrator surfaces) can call `classify_zone` to populate `RiskBudgetEntry.zone` automatically. State-delivery's renderers consume `RiskBudgetEntry.zone` directly without re-classifying.

## Acceptance criteria

- [ ] `src/alphamind/risk_guardrails/breach_behavior/zones.py` exists and defines `classify_zone` with the documented signature.
- [ ] `classify_zone` is re-exported from `src/alphamind/risk_guardrails/breach_behavior/__init__.py`.
- [ ] `classify_zone` is a pure function — no I/O, no global state, no clock reads.
- [ ] NORMAL classification: `current_value < zones.warning × limit_value / 100` returns `RiskZone.NORMAL` (verified at multiple consumption percentages).
- [ ] WARNING classification: consumption in `[zones.warning, zones.critical)` returns `RiskZone.WARNING`. The lower bound is inclusive.
- [ ] CRITICAL classification: consumption in `[zones.critical, zones.hard_block)` returns `RiskZone.CRITICAL`. The lower bound is inclusive.
- [ ] BLOCKED classification: consumption ≥ `zones.hard_block`, including consumption > 100% (current beyond limit), returns `RiskZone.BLOCKED`.
- [ ] Daily-drawdown-override case (zones 60/80/90): the four documented input pairs map to WARNING / WARNING / CRITICAL / BLOCKED respectively.
- [ ] Cumulative-drawdown-override case (zones 50/70/85): the three documented input pairs map to WARNING / CRITICAL / BLOCKED respectively.
- [ ] Negative `current_value` raises `ValueError` whose message names the value.
- [ ] Zero or negative `limit_value` raises `ValueError` whose message names the value.
- [ ] Determinism: 100 repeated calls with identical inputs produce identical outputs (verified by a parametrized test).
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
