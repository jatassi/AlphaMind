---
status: in_progress
completed_date:
commit_id:
---

# 06 — Emergency invocation header

## Goal

Land the small wrapper that prepends the emergency-invocation block to any audience's normal or halt-mode header. The block names the emergency trigger, the trigger detail, and the elapsed time since the last invocation. Per the design, emergency invocations and halt mode are independent states that can co-occur; both blocks render together when both apply, with halt-mode restrictions taking precedence behaviorally. Produces the verbatim text shape documented in [`state-delivery.md § Emergency invocation header`](../../../design/06-risk-guardrails/state-delivery.md#emergency-invocation-header).

## Reading

- `docs/design/06-risk-guardrails/state-delivery.md` § Emergency invocation header — the authoritative format spec; the four-line block immediately following envelope-open.
- `docs/design/06-risk-guardrails/breach-behavior.md` § Emergency invocation trigger — trigger conditions (regime jump, multi-rule breach, daily-drawdown velocity, margin call); cooldown rules; relationship to scheduled cadence.
- `docs/design/06-risk-guardrails/breach-behavior.md` § Agent context for emergency invocations — the worked example showing the emergency block followed by `[...normal guardrail state header follows...]`.
- `02-package-skeleton-and-config.md`, `03-shared-rendering-primitives.md` — foundation this story extends.
- `04a-analyst-header-renderer.md`, `04b-strategist-header-renderer.md`, `04c-pm-header-renderer.md` — sibling renderers whose output is wrapped.
- `../breach-behavior/03-canonical-types-and-enums.md` § 2, § 5 — canonical declarations of `EmergencyTrigger` and `EmergencyContext`; imported here, not redeclared.
- `../breach-behavior/05c-emergency-invocation-triggers.md` — the producer of the `EmergencyContext` records this story's wrapper consumes.

## Depends on

- 02 (package skeleton)
- 03 (shared rendering primitives)
- **Cross-feature dependency:** the breach-behavior work tree's `breach_behavior/03-canonical-types-and-enums.md` ships the canonical `EmergencyTrigger` enum and `EmergencyContext` typed record this story imports. For dispatch, that story must be `done`. Test fixtures construct `EmergencyContext` instances directly via the canonical class.

(Implementation independent of 04a/b/c at the source-code level — the wrapper takes a pre-rendered header string and prepends a block. Tests use stub header strings for unit testing; integration with real audience renderers is exercised in story 08.)

## Scope

In scope, all under `src/alphamind/risk_guardrails/state_delivery/emergency.py`. Tests at `tests/risk_guardrails/state_delivery/test_emergency.py`.

### 1. Trigger types and context

`EmergencyTrigger` and `EmergencyContext` are the canonical types owned by the breach-behavior work tree (declared in `breach_behavior/types.py` per `breach_behavior/03-canonical-types-and-enums.md` § 2 and § 5). Imported here:

```python
from alphamind.risk_guardrails.breach_behavior import EmergencyContext, EmergencyTrigger
```

Canonical shapes the renderer reads:

- `EmergencyTrigger` (StrEnum): `REGIME_JUMP="regime_jump"`, `MULTI_RULE_BREACH="multi_rule_breach"`, `DAILY_DRAWDOWN_VELOCITY="daily_drawdown_velocity"`, `MARGIN_CALL="margin_call"`. Adding a new trigger requires updating the canonical declaration in `breach_behavior/03` and `config/guardrails.yaml`'s `emergency_invocation.triggers` list.
- `EmergencyContext` (frozen Pydantic v2): `trigger: EmergencyTrigger`, `trigger_detail: str`, `minutes_since_last_invocation: float`, `normal_cadence_minutes: float`. Field validator `_require_positive` rejects non-positive minute values.

The `trigger_detail` is opaque to the renderer — the upstream layer (continuous monitor + breach-behavior's `evaluate_emergency_triggers` in story 05c) constructs the human-readable string per trigger type. This story does not encode per-trigger detail templates; it surfaces whatever the upstream constructs.

### 2. Block renderer

```python
def render_emergency_block(context: EmergencyContext) -> str
```

Produces the four-line block (excluding envelope-open and the normal header that follow):

```
** EMERGENCY INVOCATION — trigger: {trigger_value} **
Trigger detail: {trigger_detail}
Time since last invocation: {minutes}m (normal cadence: ~{cadence}m)
```

- Line 1: `** EMERGENCY INVOCATION — trigger: {trigger.value} **` — uses the enum's string value (e.g., `regime_jump`).
- Line 2: `Trigger detail: {trigger_detail}` — verbatim.
- Line 3: `Time since last invocation: {minutes:.0f}m (normal cadence: ~{cadence:.0f}m)` — both values rendered as integer minutes (whole-minute precision per the design's worked example showing `120m`).

The block is exactly three lines plus a trailing blank line that separates it from the next block in the assembled header. Total: four lines counting the trailing blank.

### 3. Composition wrapper

```python
def prepend_emergency_block(*, header: str, context: EmergencyContext) -> str
```

Takes a fully-rendered header string from any audience renderer (normal or halt-mode wrapper) and inserts the emergency block immediately after the envelope-open line.

Procedure:
1. Split `header` into lines.
2. Find the first line matching `^=== GUARDRAIL STATE \(invocation [^)]+, [^)]+\) ===$`. If absent, raise `ValueError` (the input is not a valid header). If found, record its index `i`.
3. Insert the emergency block lines (3 lines) after position `i`, followed by a single blank line.
4. Re-join and return.

The wrapper does not interpret or modify any other line of the header. It is intentionally a string-level operation because the audience renderers (04a/b/c) and halt-mode wrappers (05) all return assembled strings; treating their output as opaque keeps the dependency direction one-way.

### 4. Halt-mode + emergency co-occurrence

When both halt mode and emergency invocation are active, the call sequence is:

```python
header = render_pm_header_halt_mode(...)    # produces the halt-mode header
header = prepend_emergency_block(header=header, context=context)   # prepends the emergency block
```

The emergency block lands between envelope-open and the halt-mode banner. The PM (or strategist or analyst) sees:

```
=== GUARDRAIL STATE (invocation {id}, {timestamp}) ===
** EMERGENCY INVOCATION — trigger: regime_jump **
Trigger detail: Regime jump: low-vol → crisis (VIX 12 → 38)
Time since last invocation: 27m (normal cadence: ~120m)

** HALT MODE ACTIVE — daily drawdown 2.6% / 2.5% **
Mode: WATCHLIST ONLY — do not generate trade proposals

[...normal halt-mode header follows...]
```

This ordering matches the design's note that "Emergency invocations and halt mode are independent states that can co-occur. When both are active, agents receive both header blocks; halt-mode restrictions (no OPEN/ADD) take precedence." The emergency block signals urgency; the halt-mode banner signals action vocabulary restrictions. Both are visible.

### 5. Tests

Tests at `tests/risk_guardrails/state_delivery/test_emergency.py`:

- **EmergencyTrigger import sanity:** `from alphamind.risk_guardrails.breach_behavior import EmergencyTrigger` succeeds; all four trigger values present; each maps to a non-empty string. The enum is the same class identity as the breach-behavior canonical declaration (`is` check passes).
- **EmergencyContext validator:** non-positive `minutes_since_last_invocation` or `normal_cadence_minutes` raises `ValueError` (the validator inherits from the canonical breach-behavior model).
- **render_emergency_block — happy path:** for each `EmergencyTrigger` value, the rendered block contains the documented three lines with correct content.
- **render_emergency_block — minute formatting:** non-integer minutes (e.g., `27.4`) round to `27m`; cadence `120.0` renders as `120m`.
- **prepend_emergency_block — happy path:** given a stub header containing the envelope-open line plus dummy content, the wrapper inserts the emergency block at the correct position and adds a blank line before the next block.
- **prepend_emergency_block — missing envelope-open:** input header without the envelope-open line raises `ValueError`.
- **prepend_emergency_block — multiple matches:** input with multiple envelope-open lines (malformed) inserts after the first match and surfaces a warning via the test (or raises `ValueError` — pick one and document; recommend `ValueError` because a header with two envelope-opens is structurally broken).
- **Co-occurrence with halt mode:** stub header from `render_pm_header_halt_mode(...)` produces the documented composition with the emergency block before the halt-mode banner.
- **Determinism:** identical inputs produce byte-identical output across repeated calls.
- **Blank-line discipline:** emergency block ends with exactly one blank line separating it from the next block; the wrapper does not introduce double blanks.

Out of scope:
- Detection of emergency-invocation triggers — the breach-behavior + continuous-monitor work tree owns trigger detection. This story takes a typed `EmergencyContext` input.
- Trigger-detail string construction — the breach-behavior layer constructs human-readable details per trigger type.
- Per-agent behavioral shifts during emergency invocations (analyst minimizes new proposals, strategist prioritizes regime-transition remedies, PM prioritizes engine-originated actions) — those live in agent prompts and output schemas, not in the header.
- Emergency-invocation cooldown logic — the scheduler owns cooldown.
- The relationship between an emergency invocation and a rolling invocation it interrupts — the scheduler handles `max_instances=1` and rolling-invocation cancellation.

## Notes

The emergency block is a string-level prepender rather than another full renderer because the design's spec is "the emergency block goes before the normal header"; the rest of the header is unchanged. A string-level wrapper is the simplest faithful implementation.

Per `feedback_simplify_before_building.md`, the wrapper does not parse the header into structured blocks. It does the smallest line-level operation that satisfies the design (insert after envelope-open). If a future requirement demands structural composition (e.g., emergency block needs to inject into the middle of a specific block), the architecture migrates to a structured composition pass; for v1, the simpler shape is correct.

Per `feedback_no_inventing_component_names.md`, `EmergencyTrigger` and `EmergencyContext` are owned by `breach_behavior` (single source of truth) and imported here unchanged. The function names use the design's `Emergency invocation header` phrasing directly.

Per `feedback_avoid_numeric_anchors.md`, the `normal_cadence_minutes` field carries the scheduler's configured cadence as data; the renderer surfaces it. The 120-minute default in the design's worked example is illustrative, not an embedded threshold.

The wrapper is independent of the audience renderers and the halt-mode wrappers — it operates at the string level. This means stories 04a/b/c and 05 do NOT need to be `done` before story 06 can be drafted, implemented, and tested with stub header inputs. The integration test in story 08 exercises `prepend_emergency_block` against real audience renderer outputs.

Cross-feature dependency callout: when the breach-behavior + continuous-monitor work trees ship trigger detection and `EmergencyContext` construction, the upstream pipeline calls this wrapper. For this story, hand-constructed `EmergencyContext` fixtures are sufficient.

## Acceptance criteria

- [ ] `src/alphamind/risk_guardrails/state_delivery/emergency.py` exists with `render_emergency_block` and `prepend_emergency_block` exposed (re-exported from `state_delivery/__init__.py`).
- [ ] `EmergencyTrigger` and `EmergencyContext` are imported from `alphamind.risk_guardrails.breach_behavior` (the canonical source); no local declarations in state-delivery. The state-delivery `__init__.py` may re-export them as a convenience surface but does not redeclare.
- [ ] Tests verify that `EmergencyContext._require_positive` (inherited from the canonical breach-behavior model) rejects non-positive `minutes_since_last_invocation` or `normal_cadence_minutes`.
- [ ] `render_emergency_block(context)` produces the documented three-line block with correct content for every trigger value.
- [ ] Minute values render with whole-minute precision (`27.4` → `27m`).
- [ ] `prepend_emergency_block(header, context)` inserts the emergency block immediately after the envelope-open line, followed by exactly one blank line.
- [ ] `prepend_emergency_block` raises `ValueError` when the input header lacks an envelope-open line; raises `ValueError` when multiple envelope-open lines are present.
- [ ] Co-occurrence test: applying `prepend_emergency_block` to a halt-mode-wrapped header produces the documented composition (emergency block before halt-mode banner; halt-mode banner before normal header content).
- [ ] Repeated calls with the same inputs produce byte-identical output.
- [ ] No double blank lines; no trailing blank introduced by the wrapper.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
