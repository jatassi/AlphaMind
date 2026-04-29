---
status: in_progress
completed_date:
commit_id:
---

# 03 — Shared rendering primitives

## Goal

Land the small set of pure rendering helpers under `src/alphamind/risk_guardrails/state_delivery/primitives.py` that every audience-specific header renderer (stories 04a–04c), the halt-mode wrapper (story 05), and the emergency-invocation header (story 06) compose. The primitives format individual blocks of the `=== GUARDRAIL STATE ===` text envelope from typed inputs (`RiskBudgetConsumption`, `ActiveRiskParameterSet`, `SectorExposureEntry`, `DirectionalExposure`, `DrawdownState`, etc.) into the documented structured-text shape. No primitive renders a full audience header — that composition lives in stories 04a–04c.

## Reading

- `docs/design/06-risk-guardrails/state-delivery.md` — the full text-format specification; specifically the analyst, strategist, and PM header field layouts (the three full code blocks). Each repeated block (`Capital:`, `Sector headroom:`, `Directional headroom:`, `Options headroom:`, `Hard blocks:`, the `Regime:` line, the `=== GUARDRAIL STATE ===` envelope) is a primitive this story owns
- `docs/design/06-risk-guardrails/breach-behavior.md` § Escalation model — the four zone definitions (NORMAL / WARNING / CRITICAL / BLOCKED) and the per-rule threshold overrides; this story renders zone tags but does not classify zones (the upstream `RiskBudgetEntry.zone` carries the classification)
- `docs/design/06-risk-guardrails/rules-and-limits.md` § Per-rule enforcement summary — the rule registry and per-rule unit semantics that drive the format strings used here
- `src/alphamind/portfolio_state/records/capital.py` — the typed records (`RiskBudgetEntry`, `RiskBudgetConsumption`, `ActiveRiskParameterEntry`, `ActiveRiskParameterSet`, `DrawdownState`, `RegimeLabel`, `RegimeTransitionState`, `RiskZone`, `DrawdownTier`) the primitives consume
- `src/alphamind/portfolio_state/snapshot.py` — `SectorExposureEntry`, `DirectionalExposure`, the master snapshot definitions
- `src/alphamind/portfolio_state/__init__.py` — the existing `PortfolioStateConfig` Pydantic loader pattern these primitives mirror in shape
- `02-package-skeleton-and-config.md` — package layout this story extends

## Depends on

- 02 (package skeleton and `StateDeliveryConfig`)

## Scope

In scope, all under `src/alphamind/risk_guardrails/state_delivery/primitives.py`. Tests at `tests/risk_guardrails/state_delivery/test_primitives.py`.

### 1. Envelope helpers

- **`render_envelope_open(invocation_id: str, timestamp: datetime) -> str`** — produces the literal opening line `=== GUARDRAIL STATE (invocation {id}, {timestamp}) ===`. The `timestamp` is rendered in ISO-8601 with seconds precision and a trailing `Z` for UTC (raise `ValueError` on non-UTC `tzinfo`). The `invocation_id` is passed through verbatim.
- **`render_envelope_close() -> str`** — produces the literal closing line `===` for symmetry.

### 2. Regime line

- **`render_regime_line(active: ActiveRiskParameterSet) -> str`** — produces `Regime: {label} [CHANGED since last invocation | unchanged]`. `{label}` derives from `active.regime_label` lowercased and human-rendered (e.g., `RegimeLabel.LOW_VOL` → `"low-vol compression"`, `NORMAL` → `"normal"`, `ELEVATED` → `"elevated"`, `CRISIS` → `"crisis"` per the design's regime classification mapping in [regime-adaptation.md](../../../design/06-risk-guardrails/regime-adaptation.md)). The bracketed flag follows `active.parameter_change_flag` directly: `[CHANGED since last invocation]` when `True`, `[unchanged]` when `False`. The mapping from `RegimeLabel` to display string is a module-level constant `_REGIME_LABEL_DISPLAY: dict[RegimeLabel, str]`.

### 3. Zone tag

- **`render_zone_tag(zone: RiskZone) -> str`** — returns the trailing-bracket display token used in headroom rows. Mapping (module-level constant `_ZONE_TAG_DISPLAY: dict[RiskZone, str]`):
  - `RiskZone.NORMAL` → `"NORMAL"`
  - `RiskZone.WARNING` → `"⚠ WARNING"`
  - `RiskZone.CRITICAL` → `"🔴 CRITICAL"`
  - `RiskZone.BLOCKED` → `"BLOCKED"`
- The two emoji codepoints (`⚠` U+26A0, `🔴` U+1F534) appear verbatim in the source string and in test fixtures. Source files are UTF-8 (no BOM) per repository convention.

### 4. Capital block

- **`render_capital_block(*, available_for_new_positions_usd: float, available_for_new_positions_pct: float, per_position_max_usd: float, per_position_max_pct: float, regime_label_display: str) -> str`** — produces the three-line `Capital:` block:
  ```
  Capital:
    Available for new positions: ${amount} ({pct}% of portfolio)
    Per-position max size: ${amount} ({pct}% of portfolio, {regime} regime)
  ```
  - Dollar amounts render with a leading `$`, comma thousands separator, and zero decimal places (e.g., `$1,500`).
  - Percentages render to one decimal place (e.g., `60.0`).
  - The `regime_label_display` is the same display string the regime line uses; the renderer takes it as an argument so the audience renderer can pass through a single source of truth.

### 5. Sector headroom block

- **`render_sector_headroom_block(entries: tuple[RiskBudgetEntry, ...], *, sector_label_resolver: Callable[[str], str]) -> str`** — produces the labeled `Sector headroom (delta-adjusted):` block. Input `entries` is the slice of `RiskBudgetConsumption.entries` whose `rule_id` starts with `sector_concentration_` (one entry per active sector). Output:
  ```
  Sector headroom (delta-adjusted):
    Tech:       {current}% / {limit}% — room: {remaining}% [zone tag]
    Semis:      {current}% / {limit}% — room: {remaining}% [zone tag]
    ...
  ```
  - Sector labels are resolved via the `sector_label_resolver` (audience renderer passes `lambda rule_id: rule_id.removeprefix("sector_concentration_").capitalize()` or a config-injected map).
  - Sector labels are right-padded to the maximum label width seen in the input so the `:` aligns. If the active profile has only `[tech, semis]`, padding accommodates the longer of those two.
  - Numeric values render to one decimal place.
  - Each row ends with the zone tag from `render_zone_tag(entry.zone)` enclosed in square brackets.
  - Empty `entries` produces only the header line followed by an indented `(none)` line — no spurious empty block.
  - Entries are emitted in input order; the audience renderer sorts upstream if it wants alphabetical or per-profile order.

### 6. Directional headroom block

- **`render_directional_headroom_block(net_long: RiskBudgetEntry, net_short: RiskBudgetEntry | None, gross: RiskBudgetEntry) -> str`** — produces:
  ```
  Directional headroom:
    Net long:  {current}% / {limit}% — room: {remaining}%
    Net short: {current}% / {limit}% — room: {remaining}%
    Gross:     {current}% / {limit}% — room: {remaining}%
  ```
  - The `Net short` row is omitted entirely when `net_short` is `None` (primary portfolio, `short_selling_enabled: false`).
  - Numeric values render to one decimal place.
  - No zone tag — the design's directional block does not surface zone tags inline; PM and strategist see zone status via the per-position constraint proximity block.

### 7. Options headroom block

- **`render_options_headroom_block(delta: RiskBudgetEntry | None, theta: RiskBudgetEntry | None, vega: RiskBudgetEntry | None) -> str | None`** — produces:
  ```
  Options headroom:
    Delta exposure: {current}% / {limit}% — room: {remaining}%
    Theta:          {current}% / {limit}%/day
    Vega:           {current}% / {limit}%/pt
  ```
  - When all three arguments are `None` (primary portfolio, `options_enabled: false`), returns `None` — caller omits the block per the feature-flag-aware closure rule in `state-delivery.md`.
  - When some-but-not-all are `None`, raises `ValueError` — partial options-rule presence indicates upstream config drift the renderer surfaces rather than silently masks.

### 8. Hard-blocks block

- **`render_hard_blocks_block(*, breaching_entries: tuple[RiskBudgetEntry, ...], options_enabled: bool, short_selling_enabled: bool, header_label: str = "Hard blocks (do NOT recommend):") -> str`** — produces:
  ```
  {header_label}
    {one line per breaching entry, with rule label, current, and limit}
    [if options_enabled is False] Options: DISABLED for this portfolio
    [if short_selling_enabled is False] Short selling: DISABLED for this portfolio
  ```
  - `breaching_entries` should be `RiskBudgetConsumption.entries_by_zone(RiskZone.BLOCKED)` — the caller passes already-filtered entries.
  - Per-rule lines use the entry's `rule_label` and current/limit values, e.g., `"Semis sector at 24.2% / 25.0% limit — no new semi longs"`. The exact suffix (`"— no new semi longs"`) is rule-specific guidance; the primitive emits a generic shape `"{rule_label} at {current}% / {limit}% limit"` and the audience renderer can append rule-specific guidance via an optional `per_rule_guidance: dict[str, str]` argument that maps `rule_id` to a trailing clause.
  - The `header_label` defaults to the analyst/strategist phrasing; the PM renderer overrides it to `"Hard blocks (do NOT issue commands violating):"` per the design's PM-specific block heading.
  - When `breaching_entries` is empty AND both feature flags are `True`, the function returns `None` (caller omits the block); otherwise the block always renders, even if only the disabled-feature lines apply.

### 9. Module-level helpers

- **`format_dollar(value: float) -> str`** — `${value:,.0f}` with leading `$`. Negative values render as `-${abs:,.0f}` (no parentheses). Used by the capital block and any future dollar-rendering primitive.
- **`format_pct(value: float) -> str`** — `{value:.1f}` with no trailing `%` (callers append `%` to control units like `%/day`, `%/pt`). Used by every headroom block.
- The two helpers are exported from `primitives.py` for reuse in audience renderers and the validation-tool stories' user-facing strings.

### 10. Tests

Each primitive gets a corresponding test under `tests/risk_guardrails/state_delivery/test_primitives.py`. For each:

- **Happy path:** a fully-populated input produces the documented output verbatim. Use tuple comparisons over multiline strings (`assert rendered.splitlines() == expected_lines`) for diff-friendly failures.
- **Determinism:** rendering the same input twice produces byte-identical output.
- **Edge cases per primitive:**
  - `render_envelope_open` — non-UTC `tzinfo` raises `ValueError`; naive datetime raises `ValueError`.
  - `render_regime_line` — every `RegimeLabel` value maps to a non-empty display string (parametrized over the enum); `parameter_change_flag` toggles between `[CHANGED since last invocation]` and `[unchanged]`.
  - `render_zone_tag` — every `RiskZone` value maps to a non-empty tag; the emoji codepoints survive a roundtrip through `encode("utf-8").decode("utf-8")`.
  - `render_capital_block` — large values render with comma separators (`$1,500,000`); zero values render cleanly (`$0`).
  - `render_sector_headroom_block` — empty input produces the documented `(none)` line; mixed-zone inputs render each row with the correct tag; label padding aligns the `:` across the longest label.
  - `render_directional_headroom_block` — `net_short=None` omits the `Net short:` row entirely (no blank line).
  - `render_options_headroom_block` — all-None returns `None`; partial-None raises `ValueError`.
  - `render_hard_blocks_block` — empty breaches + both flags True returns `None`; only-flags-disabled produces the disabled-feature lines without a per-rule line; per-rule entries emit in input order; the optional `per_rule_guidance` map appends suffixes correctly.
  - `format_dollar` / `format_pct` — boundary values (zero, very large, negative).

Out of scope:
- Audience-specific header composition (stories 04a, 04b, 04c).
- The held-positions block, abandoned-openings block, abandoned-actions block, per-position constraint proximity block, sector-exposure-breakdown-per-position block, drawdown-state block, regime-transition-breaches block, recent-engine-originated-actions block, active-regime-overrides block, correlation-state block, dependency-risk-flag block, cross-constraint-impact-summary block — each lives in the audience renderer that owns it (analyst story 04a for the analyst-only blocks; strategist story 04b for the strategist's per-position blocks; PM story 04c for the PM-only blocks). A primitive that renders only one audience's block belongs there, not here.
- Halt-mode header modifications (story 05).
- Emergency invocation header (story 06).
- The validation tool (story 07).

## Notes

The primitives are pure functions with no side effects, no I/O, no module-level state. Every input is explicit; every output is a plain string (or `None` when the design specifies omission). This keeps the audience renderers in stories 04a–04c trivially testable — each composes a short sequence of primitive calls.

Per `feedback_simplify_before_building.md`, primitives are added only when at least two audience renderers will consume them. The Capital, Sector, Directional, Options, and Hard-blocks blocks all appear in three audiences (analyst, strategist, PM); the Regime line and envelope helpers are universal. Audience-only blocks (`Held positions`, `Per-position proximity`, `Cross-constraint impact summary`, etc.) live in their owning audience renderer rather than here, because pulling them into this module creates a primitive that one and only one caller uses — a layering cost without a layering benefit.

The unit format for percentages is `{value:.1f}` plus literal `%` appended by the caller. This lets `Theta` render as `0.15%/day` and `Vega` as `1.0%/pt` without the primitive needing per-unit knowledge. Each primitive that emits percentages composes `format_pct(value)` plus the literal unit suffix.

Per `feedback_avoid_numeric_anchors.md`, no primitive embeds zone-threshold percentages (70/85/95) or per-rule limit values. Those live upstream in `RiskBudgetEntry.zone` and `RiskBudgetEntry.limit_value`. The renderer's job is to display them, not classify them.

The emoji codepoints in the WARNING and CRITICAL zone tags are required by the design doc. UTF-8 source files render them natively in modern terminals and in the LLM context window. If a downstream renderer needs to strip emoji for a constrained terminal output, that's an audience-renderer-side choice and the primitive does not gain a no-emoji mode here — adding one preemptively contradicts the simplify-before-building feedback.

Per `feedback_no_decision_trails.md`, the format strings in this module are stated authoritatively — no comments narrating "the previous format was X" or "we chose Y over Z". The rendering is what it is; the design doc carries the rationale.

Per `feedback_no_inventing_component_names.md`, every typed input mirrors a name from `portfolio_state.records.capital` (`RiskBudgetEntry`, `ActiveRiskParameterSet`, `RiskZone`, etc.) or `portfolio_state.snapshot` (`SectorExposureEntry`, `DirectionalExposure`). The primitives do not declare new typed records; they project existing ones into text.

Cross-feature dependency: this story does not depend on the `rules-and-limits` or `guardrail-evaluation` work trees. It only depends on the existing `portfolio_state.records.capital` types, which are already shipped. Audience renderers (stories 04*) inherit dependencies on those upstream work trees through the `RiskBudgetConsumption` populate path; this primitives layer is a pure consumer of the typed records and is implementable today.

## Acceptance criteria

- [ ] `src/alphamind/risk_guardrails/state_delivery/primitives.py` exists.
- [ ] `render_envelope_open(invocation_id, timestamp)` produces the documented opening line; raises `ValueError` on non-UTC or naive `timestamp`.
- [ ] `render_envelope_close()` produces the literal `===` closing line.
- [ ] `render_regime_line(active)` produces the documented `Regime:` line with the correct display string for every `RegimeLabel` enum value and the correct `[CHANGED since last invocation | unchanged]` flag.
- [ ] `render_zone_tag(zone)` returns the documented tag for every `RiskZone` enum value, including the `⚠` and `🔴` emoji codepoints.
- [ ] `render_capital_block(...)` produces the documented three-line `Capital:` block with comma-separated dollars and one-decimal percentages.
- [ ] `render_sector_headroom_block(entries, sector_label_resolver=...)` produces the documented block with right-padded labels, per-row zone tags, input-order rows, and a `(none)` line on empty input.
- [ ] `render_directional_headroom_block(net_long, net_short, gross)` produces the documented block; when `net_short=None`, the `Net short:` row is omitted.
- [ ] `render_options_headroom_block(delta, theta, vega)` produces the documented block; returns `None` when all three are `None`; raises `ValueError` on partial-None inputs.
- [ ] `render_hard_blocks_block(...)` produces the documented block; returns `None` when breaches are empty and both feature flags are `True`; the optional `per_rule_guidance` map appends per-rule suffixes; the `header_label` argument controls the heading text for analyst/strategist vs. PM phrasing.
- [ ] `format_dollar(value)` and `format_pct(value)` are exported and behave as documented for boundary values (zero, large, negative).
- [ ] All primitives are pure: identical inputs produce byte-identical outputs across repeated calls.
- [ ] Each primitive has at least one happy-path test, one determinism test, and tests for every documented edge case.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
