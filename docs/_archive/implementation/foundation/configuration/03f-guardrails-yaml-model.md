---
status: done
completed_date: 2026-04-27
commit_id: 04895ac
---

# 03f — `guardrails.yaml` + Pydantic model

## Goal

Land `config/guardrails.yaml` (the rule registry that names every guardrail rule with its enforcement metadata) and a Pydantic model. The file holds metadata only — enforcement tiers, escalation zones, breach-response classification, monitor-between-invocations flag, optional progressive tiers — plus the emergency-invocation trigger registry. Rule **values** live in profile files (story 04a); rule **multipliers** live in regime files (story 04b). Both reference rule IDs from this file.

## Reading

- `docs/design/configuration-management.md` § `guardrails.yaml` — schema and worked example (note: the worked example uses unsuffixed IDs; reconcile per the rule-ID convention notes below)
- `docs/design/06-risk-guardrails/rules-and-limits.md` — full per-rule values, units, and rationale
- `docs/design/06-risk-guardrails/rules-and-limits.md` § Per-rule enforcement summary — authoritative tier-and-monitor mapping for all 18 rules
- `docs/design/06-risk-guardrails/breach-behavior.md` — breach-response classification (`immediate_engine` vs. `deferred_to_pm`); progressive tiers for cumulative drawdown; emergency-invocation trigger list and cooldown
- `src/alphamind/config/models/__init__.py` — re-export pattern

## Depends on

- 02 (models package skeleton)

## Scope

In scope:
- `config/guardrails.yaml` populated with one entry per rule (19 entries) and the `emergency_invocation:` block. Use the **suffixed** rule-ID convention from the per-profile worked example in `configuration-management.md`. Rule IDs:
  - `position_max_size_pct`
  - `position_max_loss_equity_pct`
  - `position_max_loss_options_pct`
  - `sector_concentration_pct`
  - `net_long_pct`
  - `net_short_pct`
  - `gross_exposure_pct`
  - `daily_drawdown_pct`
  - `cumulative_drawdown_pct`
  - `correlation_max`
  - `thesis_dependency_flag_pct`
  - `options_delta_pct`
  - `portfolio_theta_pct_per_day`
  - `portfolio_vega_pct_per_iv_point`
  - `total_short_pct`
  - `single_short_max_pct`
  - `borrow_cost_budget_pct_per_day`
  - `min_cash_reserve_pct`
  - `pending_order_capital_pct`
  (Counts to 19 — `position_max_loss` splits into two rules per the profile's worked example. The "18 rules" framing in `rules-and-limits.md`'s narrative collapses the two; the registry treats them as separate IDs since they have separate values.)
- For each rule, the entry carries:
  - `id` (matching the IDs above)
  - `enforcement_tiers` (subset of `[T1, T2, T3]` from the per-rule enforcement summary table)
  - `escalation_zones` (`{warning: int, critical: int, hard_block: int}` — defaults `{70, 85, 95}`; daily drawdown uses `{60, 80, 90}`; cumulative drawdown uses `{50, 70, 85}`)
  - `breach_response` (`immediate_engine` for the rules `breach-behavior.md` classifies as engine-authoritative, `deferred_to_pm` otherwise)
  - `monitor_between_invocations` (bool, per the "Breach detection between invocations" column in the per-rule enforcement summary)
  - `progressive_tiers` (only on `cumulative_drawdown_pct`, an ordered list of `{trigger_pct, ...}` entries per the design doc worked example)
- An `emergency_invocation:` block with `cooldown_minutes` (int) and `triggers:` (a list whose entries are either bare strings — e.g., `regime_jump`, `margin_call` — or single-key maps with structured parameters — e.g., `{multi_rule_breach_count_min: 3}`, `{daily_drawdown_velocity_pct_in_minutes: {pct: 60, minutes: 30}}`).
- `src/alphamind/config/models/guardrails.py` defining:
  - `EnforcementTier` (StrEnum: `T1`, `T2`, `T3`)
  - `BreachResponse` (StrEnum: `immediate_engine`, `deferred_to_pm`)
  - `EscalationZones` (BaseModel: `warning: int = Field(ge=0, le=100)`, `critical: int = Field(ge=0, le=100)`, `hard_block: int = Field(ge=0, le=100)`)
  - `ProgressiveTier` (BaseModel: `trigger_pct: float = Field(gt=0)`, `max_position_size_pct: float | None = None`, `max_gross_pct: float | None = None`, `full_halt: bool = False`)
  - `RuleEntry` (BaseModel: `id: str`, `enforcement_tiers: list[EnforcementTier] = Field(min_length=1)`, `escalation_zones: EscalationZones`, `breach_response: BreachResponse`, `monitor_between_invocations: bool`, `progressive_tiers: list[ProgressiveTier] | None = None`)
  - `EmergencyInvocation` (BaseModel: `cooldown_minutes: int = Field(ge=0)`, `triggers: list[str | dict[str, Any]] = Field(min_length=1)`)
  - `GuardrailsConfig` (BaseModel: `rules: list[RuleEntry] = Field(min_length=1)`, `emergency_invocation: EmergencyInvocation`)
- A model validator on `GuardrailsConfig` enforcing:
  - Every `rules[*].id` is unique within the list (no duplicates)
  - Every `rules[*].id` matches `^[a-z][a-z0-9_]*$`
  - For each rule: `escalation_zones.warning < critical < hard_block`
  - On `cumulative_drawdown_pct`: `progressive_tiers` is non-null, has ≥ 1 entry, and the entries are monotonically increasing in `trigger_pct`
  - On every other rule: `progressive_tiers` is null
- Re-export `GuardrailsConfig`, `RuleEntry`, `EnforcementTier`, `BreachResponse`, `EscalationZones`, `ProgressiveTier`, `EmergencyInvocation` from `models/__init__.py`.
- Unit tests covering: shipped `config/guardrails.yaml` parses cleanly; duplicate rule IDs raise; `escalation_zones.warning >= critical` raises; missing `progressive_tiers` on `cumulative_drawdown_pct` raises; non-monotonic `progressive_tiers` raises; an unknown enforcement tier (e.g., `T4`) raises.

Out of scope:
- Cross-reference — every rule ID referenced by a profile's `rule_values` exists here, every regime's `multipliers` covers every rule (story 06a).
- Semantic invariants on values — the registry has no values; values live in profiles.
- Wiring `GuardrailsConfig` into the loader aggregate (story 08).

## Notes

**Rule-ID convention.** The design doc has an internal inconsistency: `guardrails.yaml`'s worked example uses unsuffixed IDs (`position_max_size`), while the per-profile `rule_values` worked example uses suffixed keys (`position_max_size_pct`). The cross-reference invariant ("every rule ID referenced by a profile's `rule_values` exists in `guardrails.yaml`") only holds if both forms agree. This story locks the **suffixed** convention because: (a) the suffix carries the unit, and unit-bearing IDs are what every profile already uses; (b) `position_max_loss` splits into `_equity_pct` and `_options_pct` — two separate values, two separate IDs — and only the suffixed form expresses both. Subsequent stories (04a profiles, 04b regimes, 06a cross-reference) consume the suffixed IDs from this file.

**Per-rule classification.** The eight `immediate_engine` rules per `breach-behavior.md`:
- `position_max_loss_equity_pct`, `position_max_loss_options_pct` (engine cuts on bracket breach)
- `daily_drawdown_pct`, `cumulative_drawdown_pct` (engine halts)
- `single_short_max_pct` (engine partial-trim per `breach-behavior.md`)

The remaining rules are `deferred_to_pm`. Confirm against `breach-behavior.md` § Per-rule classification before writing the file; the spec is the source of truth.

**`monitor_between_invocations`** flag — true for the rules whose breach can fire from market movement alone (drawdown, sector concentration, exposure, options delta, total short, single short max), false for command-time-only rules (per-position max size, pending order capital) and accrual-based rules (borrow cost, theta, vega).

**Emergency-invocation triggers.** Use a heterogeneous list — bare strings for parameterless triggers and single-key maps for parameterized ones, exactly as the design doc shows. Pydantic accepts `list[str | dict[str, Any]]` natively. Do not normalize the heterogeneous list into a typed union — operators read this file by hand and the loose form is intentional.

**`thesis_dependency_flag_pct`** is the rule ID for the catalyst-failure exposure flag described in `rules-and-limits.md § Thesis-dependency risk flag`. It is `T1+T2` (advisory + PM judgment, not engine), `deferred_to_pm`, no progressive tiers, monitor_between_invocations false.

Use `model_config = ConfigDict(frozen=True)` on every model.

## Acceptance criteria

- [ ] `config/guardrails.yaml` exists, declares 19 rule entries (one per ID listed in Scope) and an `emergency_invocation:` block.
- [ ] Each rule entry carries `id`, `enforcement_tiers`, `escalation_zones`, `breach_response`, `monitor_between_invocations`.
- [ ] Only `cumulative_drawdown_pct` carries a `progressive_tiers` field.
- [ ] `config/guardrails.yaml` parses cleanly via `yaml.safe_load` and validates against `GuardrailsConfig`.
- [ ] `src/alphamind/config/models/guardrails.py` defines all seven models/enums listed in Scope.
- [ ] `models/__init__.py` re-exports the seven names.
- [ ] A unit test asserts the shipped `config/guardrails.yaml` parses and exposes 19 rule IDs matching the canonical set.
- [ ] A unit test asserts every immediate-engine rule's `breach_response == "immediate_engine"`.
- [ ] A unit test asserts duplicate `rules[*].id` raises `ValidationError`.
- [ ] A unit test asserts `escalation_zones: {warning: 85, critical: 70, hard_block: 95}` raises `ValidationError`.
- [ ] A unit test asserts `progressive_tiers` declared on `position_max_size_pct` raises `ValidationError`.
- [ ] A unit test asserts a missing `progressive_tiers` on `cumulative_drawdown_pct` raises `ValidationError`.
- [ ] A unit test asserts `progressive_tiers` with non-monotonic `trigger_pct` raises `ValidationError`.
- [ ] A unit test asserts an enforcement tier value of `T4` raises `ValidationError`.
- [ ] A unit test asserts an empty `emergency_invocation.triggers` list raises `ValidationError`.
- [ ] All Pydantic models declare `model_config = ConfigDict(frozen=True)`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
