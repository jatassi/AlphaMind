# 04 — Input bundle assembler

## Goal

Implement the function that composes the user-message text the strategist harness (story 06) sends to the LLM. The bundle is `=== GUARDRAIL STATE ===` block (rendered by the existing state-delivery layer) + a tool-reminder block + a portfolio-state section (full thesis records, position details with derived fields, activity log, pending orders — all strategist-specific) + the synthesizer brief verbatim. Pure function — no I/O, deterministic. Selects between normal-mode and defensive-posture-mode renderers based on the caller-supplied `mode` parameter.

## Reading

* `docs/design/04-decision-layer/strategist.md` § Inputs — the input table (synthesizer brief, full thesis records, position details, activity log, pending orders, abandoned-actions blocks, guardrail state header, abandoned-openings block).
* `prompts/decision/strategist.md` `<inputs>` and `<operating_context>` — the source-order contract (header → tool reminder → synthesizer brief → portfolio state).
* `src/alphamind/decision/analyst/input_bundle.py` — sibling pattern (analyst's bundle is simpler; strategist mirrors the function-signature shape but adds the portfolio-state section).
* `src/alphamind/risk_guardrails/state_delivery/__init__.py` — exports `render_strategist_header`, `render_strategist_header_halt_mode`, `StateDeliveryConfig`. Story 04 calls these directly.
* `src/alphamind/risk_guardrails/state_delivery/strategist.py` — `render_strategist_header` signature; the strategist-specific guardrail header format.
* `src/alphamind/risk_guardrails/state_delivery/halt_mode.py` — `render_strategist_header_halt_mode` signature; the defensive-posture header format.
* `src/alphamind/portfolio_state/consumers/strategist.py` — `StrategistView`, `StrategistPositionView`, `StrategistAbandonedAction`. The bundle reads sub-records.
* `src/alphamind/portfolio_state/records/theses.py` — `ThesisRecord` (entry/target/invalidation components, key_assumptions, prior_status). Renderer reads these.
* `src/alphamind/portfolio_state/records/positions.py` — `PositionRecord`. Renderer reads market value, age, side, instrument-type, etc.
* `src/alphamind/portfolio_state/records/orders.py` — `BracketRecord`, `OrderRecord`. Renderer reads bracket leg prices, pending-order ages and trigger levels.
* `src/alphamind/portfolio_state/records/activity_log.py` — `ActivityLogEntry`, `EventType`, detail records. Renderer formats the three activity-log surfaces.
* `tests/decision/analyst/test_input_bundle.py` — sibling test pattern.
* Parent Issue <issue id="213b1ce9-8210-4b63-8d1b-7957cc9466f2">ALP-116</issue> § Pre-resolved decisions (J) and (K) — derived-field computation list and activity-log section structure.

## Depends on

* None in this work tree (Wave 1 — `render_strategist_header` and `render_strategist_header_halt_mode` are already shipped via state-delivery; types from portfolio-state are shipped).

## Scope

In scope: `src/alphamind/decision/strategist/input_bundle.py`. Tests at `tests/decision/strategist/test_input_bundle.py`.

The module ships TWO public functions plus private helpers. Both are pure, deterministic, and accept all inputs as parameters.

### 1\. `assemble_input_bundle_normal(...)`

Mirrors `render_strategist_header`'s parameter shape. Returns the complete user-message string for a normal-mode invocation.

```python
def assemble_input_bundle_normal(
    *,
    strategist_view: StrategistView,
    invocation_id: str,
    timestamp: datetime,
    options_enabled: bool,
    short_selling_enabled: bool,
    active_sectors: tuple[str, ...],
    state_delivery_config: StateDeliveryConfig,
    sector_resolver: SectorResolver,
    total_portfolio_value_usd: float,
    available_for_new_positions_usd: float,
    current_price_lookup: Callable[[str], float],
    synthesizer_brief_text: str,
    tool_names: tuple[str, ...],
    sector_label_display: dict[str, str] | None = None,
    regime_transition_breaches: tuple[RegimeTransitionBreach, ...] = (),
) -> str:
```

Composition:

1. `header = render_strategist_header(...)` (passes through `strategist_view`, `regime_transition_breaches`, etc.).
2. `tool_reminder = _render_tool_reminder(tool_names, halt_mode=False)` — both `validate_guardrail` and `retrieve_brief` are surfaced in normal mode (parent decision (I)).
3. `portfolio_state_section = _render_portfolio_state_section(strategist_view, current_price_lookup)` — see § 3.
4. Return: `f"{header}\\n\\n{tool_reminder}\\n\\n{portfolio_state_section}\\n\\n=== SYNTHESIZER BRIEF ===\\n{synthesizer_brief_text}"`.

### 2\. `assemble_input_bundle_defensive_posture(...)`

Mirrors `render_strategist_header_halt_mode`'s parameter shape. Adds a `halt_state: HaltState` parameter; otherwise identical signature.

```python
def assemble_input_bundle_defensive_posture(
    *,
    halt_state: HaltState,
    strategist_view: StrategistView,
    invocation_id: str,
    timestamp: datetime,
    options_enabled: bool,
    short_selling_enabled: bool,
    active_sectors: tuple[str, ...],
    state_delivery_config: StateDeliveryConfig,
    sector_resolver: SectorResolver,
    total_portfolio_value_usd: float,
    available_for_new_positions_usd: float,
    current_price_lookup: Callable[[str], float],
    synthesizer_brief_text: str,
    tool_names: tuple[str, ...],
    sector_label_display: dict[str, str] | None = None,
    regime_transition_breaches: tuple[RegimeTransitionBreach, ...] = (),
) -> str:
```

Composition mirrors normal mode but uses `render_strategist_header_halt_mode(...)` for the header. The tool-reminder block surfaces both tools — strategist still calls `validate_guardrail` for `reduce`/`close` actions in defensive_posture (parent decision (I); contrast with analyst's halt mode which suppresses `validate_guardrail`).

### 3\. `_render_portfolio_state_section(strategist_view, current_price_lookup) -> str`

Composes the strategist-specific portfolio-state block — content the guardrail header does NOT carry. Section order:

```
=== PORTFOLIO STATE ===

Aggregate:
  Portfolio P/L: ...  (intraday + cumulative since start of period)
  Drawdown:    daily X% / Y% (zone)
               cumulative A% / B% (tier)
  Net long:    $... (X% of portfolio)
  Gross:       $... (Y% of portfolio)

Per-position records:
  POS-XXX-NNN
    Underlying:    NVDA (sector: semis, instrument: equity, direction: long)
    Size:          200 shares  $172,400  (4.5% of portfolio, delta-adj 4.5%)
    P/L:           +$8,200 since open  (+5.0%, intraday +1.2%)
    Age:           36.4 hours  (placed 2026-04-22T08:00Z)
    Distance:      target +9.4%  /  stop -3.1%  /  R/R at-current 3.0:1
    Bracket:       target $189.00 (limit), stop $167.00 (stop-market)
                   time deadline: 2026-04-25T16:00Z
                   event invalidation: "MSFT guides AI capex lower than consensus"
    Thesis (TH-NVDA-001):
      Summary:    "Hyperscaler capex acceleration drives Q1 revenue beat..."
      Entry rationale: ...
      Target rationale: ...
      Invalidation: ... (per leg)
      Key assumptions:
        - "Microsoft Q1 capex guide ≥ $24B (vs. consensus $22.8B)"
        - "..."
      Prior status: at-risk
    Pending orders for this position:
      ORD-LIMIT-4: entry_limit @ $380, age 36h, distance +5.5% from underlying
    Modification trail (recent):
      [TS] BRACKET_MODIFIED: stop tightened from $165.00 to $167.00 (PM-driven, inv-...)
      ...

  POS-XXX-NNN
    [...]

=== ACTIVITY LOG (intra-invocation) ===
  [TS] EVENT_TYPE: detail summary  (position_id, order_id where applicable)
  [...]
  None  ← when empty

=== ACTIVITY LOG (recent PM decisions) ===
  [TS] verdict approve|modify|reject on envelope ENV-N — rationale...
  [...]
  None  ← when empty
```

Helper functions (private):

* `_render_aggregate_block(strategist_view) -> str` — drawdown, P/L, net-long, gross.
* `_render_position_record(position_view, current_price_lookup) -> str` — one position with derived fields. Computes per parent decision (J): P/L absolute + percentage since open; distance-to-target percentage; distance-to-stop percentage; position age hours; risk/reward at current price (`(target - current) / (current - stop)` for longs, mirrored for shorts).
* `_render_thesis_block(thesis_record) -> str` — summary + components + key assumptions + prior_status.
* `_render_bracket_block(bracket_record) -> str` — target, stop, time, event legs.
* `_render_pending_orders_for_position(orders) -> str`.
* `_render_modification_trail(trail) -> str` — formatted activity-log entries scoped to this position.
* `_render_intra_invocation_changelog(entries) -> str`.
* `_render_pm_decision_log(entries) -> str`.

When a position has no thesis (`StrategistPositionView.thesis is None`), render `Thesis: NONE — pending position` and skip the thesis block. When a position has no bracket (`bracket is None`), render `Bracket: not yet activated`. When `current_price_lookup(ticker)` raises `KeyError`, raise `ValueError` with a clear message including the ticker and position_id (mirrors how `render_pm_header_halt_mode` handles missing prices).

### 4\. `_render_tool_reminder(tool_names, *, halt_mode) -> str`

```
=== AVAILABLE TOOLS ===
- mcp__alphamind_decision_validation__validate_guardrail
- mcp__alphamind_synthesizer_retrieval__retrieve_brief
```

In defensive_posture mode, append a one-line note: "Defensive posture active — `add` is not permitted; `validate_guardrail` is still used for risk-reducing actions touching breach constraints."

### 5\. Integer formatting and unit consistency

Currency to nearest dollar (no decimals) for absolute values ≥ $100; percent fields to one decimal place. Hours to one decimal place. Timestamps as ISO-8601 UTC. Mirror the existing state-delivery primitives (`format_pct` etc.) where compatible to keep visual parity.

### Out of scope

* Anything that requires the typed model from story 03 (e.g., emitting a JSON output template) — the input bundle is purely the user-message text input to the LLM.
* Validation of the SDK's response — story 05b.
* Harness MCP wiring — story 06.
* Runner orchestration — story 07.

## Acceptance criteria

- [ ] `src/alphamind/decision/strategist/input_bundle.py` exports `assemble_input_bundle_normal` and `assemble_input_bundle_defensive_posture` with the signatures above.
- [ ] Both functions are pure (no I/O, no logging, no time-source calls except via parameter).
- [ ] Normal-mode bundle order: header → tool reminder → portfolio state section → synthesizer brief.
- [ ] Defensive-posture bundle order matches, with the halt-mode header from `render_strategist_header_halt_mode`.
- [ ] Per-position record renders all derived fields named in parent decision (J): P/L absolute + percentage since open, distance-to-target %, distance-to-stop %, position age hours, R/R at current price.
- [ ] Per-position record renders the full thesis (summary, entry/target/invalidation rationale, key assumptions, prior_status) when `thesis is not None`.
- [ ] Per-position record renders pending orders scoped to that position (`StrategistPositionView.pending_orders`) and the modification trail.
- [ ] All three activity-log surfaces are rendered: intra-invocation changelog, recent PM decision log, per-position modification trail (the third is inline in each position).
- [ ] `_render_tool_reminder` surfaces both `validate_guardrail` and `retrieve_brief` tool names in both modes.
- [ ] `_render_tool_reminder` adds the "defensive posture" note in defensive_posture mode.
- [ ] Missing current-price lookup raises `ValueError` with ticker + position_id in the message.
- [ ] `tests/decision/strategist/test_input_bundle.py` covers: round-trip a constructed `StrategistView` through both functions; assert section order and headers; assert derived fields render correctly given known inputs; assert missing-price raises `ValueError`; assert empty changelog/PM-log render `None`.
- [ ] `uv run pytest tests/decision/strategist/test_input_bundle.py -n auto` passes.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` clean.

## Verification

```bash
uv run pytest tests/decision/strategist/test_input_bundle.py -n auto
```

Manually inspect the rendered bundle for a constructed `StrategistView` (4 positions, 2 pending orders, sparse activity log) — sections clearly delimited, derived fields readable, source-of-truth correspondences clear.
