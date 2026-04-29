---
status: in_progress
completed_date:
commit_id:
---

# 04a — Analyst guardrail state header renderer

## Goal

Land the function that produces the complete analyst guardrail state header — the formatted `=== GUARDRAIL STATE ===` text block delivered at the top of the analyst's prompt. Composes the shared rendering primitives (story 03) with three analyst-specific blocks (held positions, abandoned openings, hard blocks). Produces the verbatim text shape documented in [`state-delivery.md § Analyst guardrail state header`](../../../design/06-risk-guardrails/state-delivery.md#analyst-guardrail-state-header).

## Reading

- `docs/design/06-risk-guardrails/state-delivery.md` § Analyst guardrail state header — the authoritative format spec; Held positions, Abandoned openings, and Hard blocks are this story's audience-specific blocks. The block order in the design doc is the rendering order.
- `docs/design/06-risk-guardrails/state-delivery.md` § Design principles — snapshot consistency, token efficiency, progressive detail, feature-flag aware
- `docs/design/04-decision-layer/analyst.md` § Inputs — the analyst's input contract; this header is the sole guardrail-context input
- `docs/design/04-decision-layer/analyst.md` § Pre-submission guardrail validation — explains the role of the headroom context vs. the validation tool
- `docs/design/06-risk-guardrails/rules-and-limits.md` § Dual portfolio profiles — feature-flag closure semantics; when `options_enabled: false` or `short_selling_enabled: false`, corresponding sections omit and the disabled-feature appears in the hard-blocks block
- `src/alphamind/portfolio_state/consumers/analyst.py` — `AnalystView`, `AnalystHeldPosition`, `AnalystAvailableCapital`, `AnalystAbandonedOpening` — the typed view this renderer consumes
- `src/alphamind/portfolio_state/snapshot.py` — `PortfolioStateSnapshot`, `SectorExposureEntry`, `DirectionalExposure` — additional inputs the analyst view does not carry but the header needs (sector / directional / options headroom is computed from `RiskBudgetConsumption`, which is on the snapshot, not the analyst view)
- `src/alphamind/portfolio_state/records/capital.py` — `RiskBudgetConsumption`, `RiskBudgetEntry`, `ActiveRiskParameterSet`, `RiskZone` — typed inputs for the headroom blocks
- `02-package-skeleton-and-config.md` — `state_delivery/analyst.py` is the target module; `StateDeliveryConfig` carries `abandoned_window.lookback_invocations`
- `03-shared-rendering-primitives.md` — `render_envelope_open`, `render_envelope_close`, `render_regime_line`, `render_capital_block`, `render_sector_headroom_block`, `render_directional_headroom_block`, `render_options_headroom_block`, `render_hard_blocks_block`, `format_dollar`, `format_pct` — the primitives this renderer composes

## Depends on

- 02 (package skeleton + config)
- 03 (shared rendering primitives)
- **Cross-feature dependency:** the `rules-and-limits` work tree ships the runtime `RiskBudgetConsumption` and `ActiveRiskParameterSet` populator. The repository's `get_risk_budget_consumption` and `get_active_risk_parameters` are typed Protocols today (returns are well-defined; the production implementation that computes per-rule current/limit/zone from positions + active config lives there). For this story to dispatch, the rules-and-limits work tree's stories that produce a populated `RiskBudgetConsumption` must be `done`. Until then, this story's tests use a hand-constructed `RiskBudgetConsumption` fixture; the dispatch gate is enforced by the orchestrator before a real-snapshot integration test in story 08.

## Scope

In scope, all under `src/alphamind/risk_guardrails/state_delivery/analyst.py`. Tests at `tests/risk_guardrails/state_delivery/test_analyst.py`.

### 1. Public function

```python
def render_analyst_header(
    *,
    analyst_view: AnalystView,
    risk_budget: RiskBudgetConsumption,
    active_risk_parameters: ActiveRiskParameterSet,
    invocation_id: str,
    timestamp: datetime,
    options_enabled: bool,
    short_selling_enabled: bool,
    active_sectors: tuple[str, ...],
    config: StateDeliveryConfig,
    sector_label_display: dict[str, str] | None = None,
) -> str
```

- `analyst_view` — the projected view from `project_analyst_view(snapshot, ...)`. Carries held positions, available capital, pending orders, abandoned openings, active thesis summaries.
- `risk_budget` — the per-rule consumption snapshot (current/limit/headroom/zone per rule) the renderer reads to populate sector / directional / options headroom blocks and the hard-blocks block.
- `active_risk_parameters` — the active regime / overlay / per-rule values; supplies the `Regime:` line and the `Per-position max size` value the capital block displays.
- `invocation_id`, `timestamp` — passed to `render_envelope_open` for the opening line.
- `options_enabled`, `short_selling_enabled` — feature flags from the active profile; control whether the options-headroom block renders and whether the hard-blocks block emits the `Options: DISABLED` / `Short selling: DISABLED` lines.
- `active_sectors` — ordered tuple of sector keys from the active profile (e.g., `("tech", "semis")` for micro). Used for sector ordering in the sector-headroom block. Sector display labels resolve through `sector_label_display` (e.g., `{"tech": "Tech", "semis": "Semis"}`); when `None`, the renderer capitalizes the key.
- `config` — `StateDeliveryConfig` carrying `abandoned_window.lookback_invocations`. The `abandoned_openings` block shows only entries from the prior invocation per the config knob.

Returns the complete header text, terminated by the closing `===` line. No trailing newline.

### 2. Held positions block

Renders the analyst-specific held-positions dedup block:

```
Held positions (dedup — skip same underlying + direction; strategist owns hold/add/reduce):
  {TICKER}  {direction}  {pct}%  {sector}
  ...
  [or "None" if the book is empty]
```

- One line per `AnalystView.held_positions` entry, in input order (the projection step orders them; this renderer does not re-sort).
- Format per row: ticker (uppercase), direction (`long` / `short`), size as one-decimal percentage with trailing `%`, sector display label.
- Column alignment: ticker left-justified to the longest ticker in the book; direction left-justified to a 5-char width to accommodate `short`. Numeric `pct` right-justified to 5 chars (e.g., ` 4.2%`). Sector label appended without padding.
- Empty `held_positions` produces the literal `  None` indented line.

Helper: `_render_held_positions_block(positions: tuple[AnalystHeldPosition, ...], sector_label_display: dict[str, str] | None) -> str` — pure private function in `analyst.py`. Not exposed via `__init__.py`.

### 3. Abandoned openings block

Renders the analyst's abandoned-OPEN entries from the prior invocation:

```
Abandoned openings from prior invocation (decide on current grounds whether to re-propose):
  {ENV-REC-n}  {direction} {TICKER} {asset_type}  {size}%  — abandoned at {timestamp} ({failure_reason})
  ...
  [or "None" if no OPEN commands were abandoned in the prior invocation]
```

- One line per `AnalystView.abandoned_openings` entry within the lookback window.
- The lookback window is `config.abandoned_window_lookback_invocations` (default `1`). Filtering by invocation requires the `AnalystView` projection to surface the originating invocation per entry — but the current `AnalystView.abandoned_openings: tuple[AnalystAbandonedOpening, ...]` projection in `consumers/analyst.py` does not carry an `originating_invocation_id` field. This story takes the entries as already-filtered upstream (the projection step in `consumers/analyst.py` is responsible for the lookback-window scoping); the renderer trusts the input.
- Format per row: envelope ID, single space, direction, single space, ticker (uppercase), single space, asset type (`equity` / `option` / `strategy`), two spaces, size as one-decimal percentage with trailing `%`, two spaces, em-dash (` — `), `abandoned at `, timestamp in ISO-8601 with seconds precision and `Z` suffix, single space, `(`, failure reason verbatim, `)`.
- Empty `abandoned_openings` produces the literal `  None` indented line.
- Currently the projection layer's `AnalystAbandonedOpening` carries empty `ticker`, `direction=Direction.LONG`, `instrument_type=InstrumentType.EQUITY`, `size_pct=0.0` defaults (`consumers/analyst.py` lines 154–158 — `CommandAbandonedDetail` does not yet surface these fields). The renderer emits whatever the typed view contains. When the upstream detail schema is extended in a follow-up story, the renderer continues to work without change. This story does not re-shape the projection layer; it surfaces the typed fields verbatim.

Helper: `_render_abandoned_openings_block(entries: tuple[AnalystAbandonedOpening, ...]) -> str` — pure private function.

### 4. Composition: full header

`render_analyst_header` produces the following sequence (each block separated by exactly one blank line; no trailing blank lines):

1. `render_envelope_open(invocation_id, timestamp)`
2. `render_regime_line(active_risk_parameters)`
3. *blank line*
4. `render_capital_block(...)` — composed from `analyst_view.available_capital` + `_render_regime_label_display(active_risk_parameters.regime_label)`
5. *blank line*
6. `render_sector_headroom_block(sector_entries, sector_label_resolver=...)` — `sector_entries` = `risk_budget.entries` filtered to `rule_id` starting with `sector_concentration_` AND whose suffix matches an `active_sectors` key (in `active_sectors` order). The resolver takes `rule_id` and looks up the display label from `sector_label_display`.
7. *blank line*
8. `render_directional_headroom_block(net_long, net_short, gross)` — `net_long` is `risk_budget.entry_by_rule_id("net_long_pct")`; `net_short` is `risk_budget.entry_by_rule_id("net_short_pct")` when `short_selling_enabled` else `None`; `gross` is `risk_budget.entry_by_rule_id("gross_exposure_pct")`. Missing entries (any of the three returning `None` when expected) raise `ValueError` — upstream config drift is surfaced.
9. *blank line — only if the options block renders*
10. `render_options_headroom_block(delta, theta, vega)` — pulled by rule_id `options_delta_pct`, `portfolio_theta_pct_per_day`, `portfolio_vega_pct_per_iv_point`. When `options_enabled` is `False`, all three lookups should return `None` (feature-flag closure means the rules are absent from `risk_budget.entries`); the primitive then returns `None` and the block is omitted.
11. *blank line*
12. `_render_held_positions_block(analyst_view.held_positions, sector_label_display)`
13. *blank line*
14. `_render_abandoned_openings_block(analyst_view.abandoned_openings)`
15. *blank line — only if the hard-blocks block renders*
16. `render_hard_blocks_block(breaching_entries=risk_budget.breaching_entries(), options_enabled=options_enabled, short_selling_enabled=short_selling_enabled)` — uses the analyst/strategist phrasing default `"Hard blocks (do NOT recommend):"`. When the primitive returns `None`, this slot is omitted (no blank line).
17. `render_envelope_close()`

The final string passes through `"\n".join(parts)` with `parts` excluding any block that returned `None` and any blank-line slot adjacent to an omitted block. The result has no trailing newline.

### 5. Tests

Tests at `tests/risk_guardrails/state_delivery/test_analyst.py`:

- **Happy path — micro profile (options + shorts disabled, 2 sectors, 5 held positions, 1 abandoned opening, 1 critical-zone breach):** the rendered header equals a fixture string, line-by-line, including the omitted options block, the omitted `Net short:` row, the disabled-feature lines in the hard-blocks block, and the per-rule guidance line.
- **Happy path — full-system profile (options + shorts enabled, 4 sectors, 12 held positions, 0 abandoned openings, 0 breaches):** the rendered header includes the options block, the `Net short:` row, the held-positions list with column alignment across 12 entries; the hard-blocks block is omitted.
- **Empty book:** `analyst_view.held_positions = ()` produces `  None` in the held-positions block; everything else renders normally.
- **Empty abandoned:** `analyst_view.abandoned_openings = ()` produces `  None` in the abandoned-openings block.
- **Determinism:** rendering the same inputs twice produces byte-identical output.
- **Snapshot consistency:** the renderer reads only the typed inputs; mutating the snapshot after `project_analyst_view` does not affect the rendered header (frozen-Pydantic invariant; trivially passes if the renderer touches no global state).
- **Feature-flag closure invariants:**
  - `options_enabled=False` AND any options rule present in `risk_budget.entries` raises `ValueError` (upstream config drift).
  - `short_selling_enabled=False` AND `risk_budget.entry_by_rule_id("net_short_pct") is not None` raises `ValueError`.
  - These invariants surface drift between the active profile's feature flags and the populated `risk_budget`. The renderer fails closed rather than silently masking inconsistency.
- **Missing required rule:** when `risk_budget.entry_by_rule_id("net_long_pct")` is `None` (or `gross_exposure_pct`, or any sector in `active_sectors` is missing from `risk_budget.entries`), raise `ValueError` with the rule_id in the message.
- **Sector ordering:** sector rows render in `active_sectors` order, not alphabetical and not `risk_budget.entries` order.
- **Hard-blocks `(none)` shape:** confirm that the design's "no hard blocks, no disabled features" path produces a header without the `Hard blocks` block at all (not an empty block) — the entire slot is dropped, no blank-line residue.
- **Blank-line discipline:** no two consecutive blank lines anywhere in the output; no trailing blank line; the closing `===` is immediately preceded by a non-blank line.

Out of scope:
- The strategist or PM header (stories 04b, 04c).
- The halt-mode wrapper (story 05).
- The emergency invocation header (story 06).
- The validation tool (story 07).
- A no-emoji rendering mode for terminals that can't display the WARNING/CRITICAL emoji codepoints — the design specifies the emoji; downstream rendering is the consumer's concern.
- Wiring the renderer into the analyst's actual prompt-assembly pipeline — that lands in the analyst-agent runtime story, not here.

## Notes

`render_analyst_header` is a pure function. It reads only its arguments; it has no module-level state, no I/O, no logging side-effects. The audience renderers in 04a/b/c follow this convention so the test suite is deterministic and the renderer can be invoked from any context (production, paper, replay, validation tool) without surprise.

The design's `Held positions` block uses `(dedup — skip same underlying + direction; strategist owns hold/add/reduce)` as its parenthetical heading — verbatim. Do not rewrite for brevity. The strategist's analogous block has different wording; story 04b owns that.

Per `feedback_no_inventing_component_names.md`, the primary public function name `render_analyst_header` and the helper names `_render_held_positions_block` / `_render_abandoned_openings_block` use the design doc's section names directly — `Held positions`, `Abandoned openings from prior invocation`. No new component or block names introduced.

Per `feedback_simplify_before_building.md`, the held-positions and abandoned-openings helpers live as private functions inside `analyst.py` rather than getting promoted to `primitives.py`. They are analyst-only; promoting them would create one-caller primitives. If the strategist or PM later needs the same shape, the helper migrates to `primitives.py` then — not preemptively.

The feature-flag-drift `ValueError`s catch the situation where the active profile says `options_enabled: false` but `risk_budget.entries` still contains options rules (or vice versa). This indicates upstream config-loading or risk-budget-population drift — a structural error worth surfacing rather than silently masking. The configuration foundation's semantic self-test enforces the same closure (`feature-flag closure` invariant in `configuration-management.md`); the renderer's runtime check is the second line of defense.

Per the user's `feedback_per_producer_schema.md`, each audience has its own per-audience renderer file rather than one monolithic `render_header(audience, ...)` switch. The three renderers (analyst, strategist, PM) share primitives but encode their own composition rules per file — making the per-audience contract self-contained for inspection and edit.

The `sector_label_display` argument allows the analyst's prompt-assembly stage to inject a profile-specific display map (e.g., `{"tech": "Tech", "semis": "Semis"}`). The default-when-`None` capitalization behavior keeps tests simple without forcing the caller to provide a map for trivial cases.

Per `feedback_avoid_numeric_anchors.md`, the renderer does not classify zones, compute headroom, or pick warning/critical thresholds. It reads `RiskBudgetEntry.zone` and renders the corresponding tag via `render_zone_tag`. All numeric thresholds live upstream in `config/guardrails.yaml` and the rules-and-limits library.

Cross-feature dependency callout: when the rules-and-limits work tree ships the production `get_risk_budget_consumption` implementation, this renderer's tests can move from hand-constructed `RiskBudgetConsumption` fixtures to a real-snapshot integration test (story 08 owns that integration). For this story, hand-constructed fixtures are sufficient and faster — the renderer's contract is in this story's acceptance criteria, not in a downstream integration's behavior.

## Acceptance criteria

- [ ] `src/alphamind/risk_guardrails/state_delivery/analyst.py` exists with `render_analyst_header` exposed (re-exported from `state_delivery/__init__.py`).
- [ ] The function signature matches the documented keyword-only parameter list.
- [ ] On a hand-constructed micro-profile fixture (options + shorts disabled, 2 sectors, 5 held, 1 abandoned, 1 critical breach), the rendered output equals the fixture string line-by-line, including the omitted options block, the disabled-feature lines, and the per-rule guidance.
- [ ] On a hand-constructed full-system fixture (options + shorts enabled, 4 sectors, 12 held, 0 abandoned, 0 breaches), the rendered output includes the options block, the `Net short:` row, and the column-aligned held-positions list; the hard-blocks block is omitted.
- [ ] Empty `held_positions` produces a `  None` line in the held-positions block; empty `abandoned_openings` produces a `  None` line in the abandoned-openings block.
- [ ] Sector rows render in `active_sectors` order, not alphabetical and not `risk_budget.entries` order.
- [ ] `options_enabled=False` AND any options rule present in `risk_budget.entries` raises `ValueError` mentioning the offending rule_id.
- [ ] `short_selling_enabled=False` AND `risk_budget.entry_by_rule_id("net_short_pct") is not None` raises `ValueError` mentioning `net_short_pct`.
- [ ] Missing `net_long_pct`, `gross_exposure_pct`, or any expected sector entry in `risk_budget.entries` raises `ValueError` with the missing rule_id in the message.
- [ ] No two consecutive blank lines in the rendered output; no trailing blank line; the closing `===` is immediately preceded by a non-blank line.
- [ ] Repeated calls with the same inputs produce byte-identical output.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
