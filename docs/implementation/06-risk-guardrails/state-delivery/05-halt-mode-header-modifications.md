---
status: not_started
completed_date:
commit_id:
---

# 05 — Halt-mode header modifications

## Goal

Land the wrappers that transform each audience's normal guardrail state header into its halt-mode variant: the analyst's WATCHLIST mode, the strategist's DEFENSIVE POSTURE mode, the PM's RISK REDUCTION mode. Each variant adds a banner block immediately after the envelope-open line, replaces or annotates specific normal-header blocks per the audience's halt-mode behavioral contract, and preserves the rest of the header. Produces the verbatim text shape documented in [`state-delivery.md § Halt-mode header modifications`](../../../design/06-risk-guardrails/state-delivery.md#halt-mode-header-modifications).

## Reading

- `docs/design/06-risk-guardrails/state-delivery.md` § Halt-mode header modifications — the authoritative specs for the three audience-specific variants. Three subsections: `Analyst — watchlist mode`, `Strategist — defensive posture mode`, `PM — risk reduction mode`. Each names the banner, the action restrictions, and any block changes vs. normal.
- `docs/design/06-risk-guardrails/breach-behavior.md` § Drawdown halt mode — the trigger conditions (daily 100%, cumulative 12% / tier 3); the action vocabulary restrictions (no OPEN/ADD; CLOSE/ADJUST/CANCEL only).
- `docs/design/06-risk-guardrails/breach-behavior.md` § Agent behavior during halt mode — the per-audience behavioral contract reflected in each variant's header.
- `docs/design/06-risk-guardrails/breach-behavior.md` § Cumulative drawdown response — the progressive-tier trigger; tier 3 (12%+) is the full-halt mode. Tiers 1 and 2 are NOT halt modes; the cumulative-tier line in the strategist/PM normal headers covers tiers 1 and 2 already (story 04b/04c).
- `docs/design/configuration-management.md` § `modes/halt.yaml` — the mode-overlay file's behavioral contract (action vocabulary restrictions, output-mode flags) that the resolver applies when halt mode is active. The renderer reads upstream-resolved state, not the YAML directly.
- `02-package-skeleton-and-config.md`, `03-shared-rendering-primitives.md`, `04a-analyst-header-renderer.md`, `04b-strategist-header-renderer.md`, `04c-pm-header-renderer.md` — the foundations this story wraps.
- `../breach-behavior/03-canonical-types-and-enums.md` § 4 — canonical declaration of `HaltState`; imported here, not redeclared.
- `../breach-behavior/05a-halt-state-computation.md` — the producer of the `HaltState` records this story's wrappers consume.

## Depends on

- 02 (package skeleton)
- 03 (shared rendering primitives)
- 04a (analyst renderer to wrap)
- 04b (strategist renderer to wrap)
- 04c (PM renderer to wrap)
- **Cross-feature dependency:** the breach-behavior work tree's `breach_behavior/03-canonical-types-and-enums.md` ships the canonical `HaltState` typed record this story imports. For dispatch, that story must be `done`. Test fixtures construct `HaltState` instances directly via the canonical class.

## Scope

In scope, all under `src/alphamind/risk_guardrails/state_delivery/halt_mode.py`. Tests at `tests/risk_guardrails/state_delivery/test_halt_mode.py`.

### 1. Halt-mode trigger contract

Halt mode is active when EITHER:
- Daily drawdown ≥ 100% of limit (i.e., `drawdown.intraday_drawdown_pct ≥ daily_drawdown_pct` from `active_risk_parameters`), OR
- Cumulative drawdown ≥ tier 3 threshold (`drawdown.cumulative_tier == DrawdownTier.FULL_HALT`).

The renderer's wrappers do not classify — they take a typed `HaltState` input that the upstream pipeline computes and passes through. This decouples trigger detection (breach-behavior layer) from rendering (state-delivery layer).

`HaltState` is the canonical frozen Pydantic v2 model owned by the breach-behavior work tree (declared in `breach_behavior/types.py` per `breach_behavior/03-canonical-types-and-enums.md` § 4). Imported here:

```python
from alphamind.risk_guardrails.breach_behavior import HaltState
```

Canonical fields the wrappers read: `daily_halt_active: bool`, `cumulative_full_halt_active: bool`, `daily_drawdown_pct: float`, `daily_drawdown_limit_pct: float`. The canonical model carries two validators: `_validate_at_least_one_active` (rejects construction unless at least one halt flag is `True`) and `_require_non_negative` (on the two drawdown-pct fields). State-delivery uses both unchanged.

When halt mode is not active, the caller does not construct a `HaltState` — they call the audience's normal renderer directly. The wrappers in this story are invoked only when halt mode is active.

### 2. Analyst — watchlist mode wrapper

Per the design's `Analyst — watchlist mode` subsection:

```
=== GUARDRAIL STATE (invocation {id}, {timestamp}) ===
** HALT MODE ACTIVE — daily drawdown {current}% / {limit}% **
Mode: WATCHLIST ONLY — do not generate trade proposals

Capital:
  New positions: BLOCKED (halt active)
  ...
```

- The banner block consists of two lines: the `** HALT MODE ACTIVE — ... **` line and the `Mode: WATCHLIST ONLY — do not generate trade proposals` line.
- The `Capital:` block in the normal header is REPLACED with a halt-mode-specific capital block. New shape:
  ```
  Capital:
    New positions: BLOCKED (halt active)
    Per-position max size: not applicable (halt mode)
  ```
  The `Available for new positions: ...` row of the normal capital block is replaced with `New positions: BLOCKED (halt active)`. The `Per-position max size: ...` row is replaced with `Per-position max size: not applicable (halt mode)`.
- All other normal-header blocks (regime line, sector headroom, directional, options, held positions, abandoned openings, hard blocks) are preserved verbatim. The analyst still sees headroom and held-positions context — the design's halt-mode behavior is "watchlist mode generates no trade proposals", but the agent reasons about what *would* be tradeable post-halt; surfacing the headroom context informs that reasoning.

Function signature:

```python
def render_analyst_header_halt_mode(
    *,
    halt_state: HaltState,
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

Same parameters as `render_analyst_header` plus `halt_state`. The wrapper is NOT implemented as a post-processing pass over the normal renderer's output; instead, it composes the same primitives in the same order with the two block-level substitutions (banner inserted; capital block replaced). This avoids fragile string replacement and keeps both renderers comparable line-by-line.

Implementation pattern (private helper):

```python
def _render_halt_mode_banner(halt_state: HaltState) -> str:
    pct = format_pct(halt_state.daily_drawdown_pct)
    limit = format_pct(halt_state.daily_drawdown_limit_pct)
    return f"** HALT MODE ACTIVE — daily drawdown {pct}% / {limit}% **"
```

The mode-line (`Mode: WATCHLIST ONLY — ...`) is audience-specific; each audience renderer assembles its own composition with the shared banner.

### 3. Strategist — defensive posture mode wrapper

Per the design's `Strategist — defensive posture mode` subsection:

```
=== GUARDRAIL STATE (invocation {id}, {timestamp}) ===
** HALT MODE ACTIVE — daily drawdown {current}% / {limit}% **
Mode: DEFENSIVE POSTURE — focus on risk reduction for existing positions

Position-level constraint proximity:
  ...
```

- Banner block: same `** HALT MODE ACTIVE ... **` line + `Mode: DEFENSIVE POSTURE — focus on risk reduction for existing positions` line.
- The strategist's normal header blocks are PRESERVED — the strategist receives full distillation and research context to assess whether theses hold under halt conditions. The header itself does not gain or lose any block; only the banner is prepended.
- However, the design's `Strategist — defensive posture mode` mentions the strategist's behavioral shift (per-position emphasis on deteriorating theses, stops to tighten, closes for weakened positions). The behavior shift is encoded in the strategist's prompt and output schema (defensive_posture mode flag — see `strategist.md`), not in the header itself. The header's job is to surface the active-mode signal (the banner) and let the rest of the prompt machinery handle the behavioral contract.

Function signature:

```python
def render_strategist_header_halt_mode(
    *,
    halt_state: HaltState,
    strategist_view: StrategistView,
    invocation_id: str,
    timestamp: datetime,
    options_enabled: bool,
    short_selling_enabled: bool,
    active_sectors: tuple[str, ...],
    config: StateDeliveryConfig,
    sector_label_display: dict[str, str] | None = None,
    sector_resolver: Callable[[PositionRecord], str | None],
    total_portfolio_value_usd: float,
    available_for_new_positions_usd: float,
    regime_transition_breaches: tuple[RegimeTransitionBreach, ...] = (),
) -> str
```

Same parameters as `render_strategist_header` plus `halt_state`.

### 4. PM — risk reduction mode wrapper

Per the design's `PM — risk reduction mode` subsection:

```
=== GUARDRAIL STATE (invocation {id}, {timestamp}) ===
** HALT MODE ACTIVE — daily drawdown {current}% / {limit}% **
Available actions: CLOSE, ADJUST, CANCEL only
Blocked actions: OPEN, ADD

Pending orders review:
  {list of any limit orders placed before halt, with current distance from fill price}
  ...
```

- Banner block: the `** HALT MODE ACTIVE ... **` line + `Available actions: CLOSE, ADJUST, CANCEL only` line + `Blocked actions: OPEN, ADD` line. Three lines instead of two — the PM's variant explicitly enumerates the action vocabulary because the PM is the agent issuing commands.
- A new `Pending orders review:` block is inserted between the banner and the rest of the normal PM header. Per the design's PM section, this block surfaces the pre-halt limit orders so the PM can decide cancel/maintain. The block format:
  ```
  Pending orders review:
    {ORD-id}: {direction} {ticker} @ {limit_price} — current distance: {distance_pct}% (placed {age_hours}h ago)
    ...
    [or "None" if no pending orders pre-halt]
  ```
  - Reads from `pm_view.positions[*].pending_orders` (each `StrategistPositionView.pending_orders` flattened) plus any portfolio-level pending orders surfaced via the snapshot. The wrapper takes `pending_orders: tuple[OrderRecord, ...]` as an additional parameter to keep dependency injection explicit.
  - Per-row: order ID, `:`, single space, direction, single space, ticker, ` @ `, limit price (formatted with `$`), ` — current distance: `, percentage distance from current price (one decimal place, signed), `% (placed `, age in hours (one decimal place), `h ago)`.
  - The "current distance from fill price" requires the current price. The wrapper takes a `current_price_lookup: Callable[[str], float]` parameter (a callable that resolves a ticker to its current price); the snapshot's `CurrentPriceProvider` Protocol can be passed through. Missing prices raise `ValueError`.
  - Empty pending-orders → `  None` line.
- The cross-constraint-impact block is REPLACED with a degenerate variant scoped to risk-reducing actions. New shape:
  ```
  Cross-constraint impact summary:
    Scoped to risk-reducing actions only (CLOSE, ADJUST, CANCEL).
    {per-rule lines from cross_constraint_impact, filtered to rules where any pending action would touch them}
  ```
  - The wrapper takes the same `cross_constraint_impact: CrossConstraintImpact` typed input as the normal PM renderer; the upstream pre-processor is responsible for scoping its computation to risk-reducing actions when halt mode is active.
  - When `cross_constraint_impact.per_rule` is empty, the `(per-rule lines ...)` portion is replaced with `  No pending risk-reducing actions; no projected impact.`.
- All other normal-header blocks (regime line, capital block, sector/directional/options headroom, per-position proximity, sector breakdown, drawdown context, regime-transition breaches, recent engine actions, active regime overrides, correlation, dependency-risk-flag, hard blocks) are PRESERVED.
- The hard-blocks block during halt mode often surfaces additional disabled-action lines; per the design, the PM's halt-mode hard-blocks block also includes:
  ```
  OPEN: BLOCKED (halt mode)
  ADD: BLOCKED (halt mode)
  ```
  - The wrapper passes a `per_action_blocks: dict[str, str] = {"OPEN": "BLOCKED (halt mode)", "ADD": "BLOCKED (halt mode)"}` argument to `render_hard_blocks_block` — but `render_hard_blocks_block` (story 03) does not currently accept per-action blocks. Two options:
    - Option A: extend `render_hard_blocks_block` in story 03 to accept an optional `additional_lines: tuple[str, ...]` parameter that appends extra indented lines before the disabled-feature lines. Add this in story 03's scope (low cost; trivial to extend).
    - Option B: in this story, post-render the hard-blocks block to inject the additional lines via a private helper. Avoids retroactive scope addition to story 03.
  - Resolution: Option B (post-render injection). This story declares a private helper `_inject_halt_mode_hard_block_lines(rendered_hard_blocks_block: str | None, halt_state: HaltState) -> str` that takes the primitive's output and appends the `OPEN: BLOCKED (halt mode)` and `ADD: BLOCKED (halt mode)` lines before the closing line of the hard-blocks block. When the primitive returns `None` (no breaches, no disabled features), the helper synthesizes the block from scratch with just the halt-mode action lines.

Function signature:

```python
def render_pm_header_halt_mode(
    *,
    halt_state: HaltState,
    pm_view: PortfolioManagerView,
    invocation_id: str,
    timestamp: datetime,
    options_enabled: bool,
    short_selling_enabled: bool,
    active_sectors: tuple[str, ...],
    config: StateDeliveryConfig,
    sector_label_display: dict[str, str] | None = None,
    sector_resolver: Callable[[PositionRecord], str | None],
    total_portfolio_value_usd: float,
    available_for_new_positions_usd: float,
    cross_constraint_impact: CrossConstraintImpact,
    pending_orders: tuple[OrderRecord, ...],
    current_price_lookup: Callable[[str], float],
    regime_transition_breaches: tuple[RegimeTransitionBreach, ...] = (),
    active_regime_overrides: tuple[RegimeOverride, ...] = (),
    correlation_state: CorrelationState | None = None,
    dependency_risk_flag: DependencyRiskFlag | None = None,
) -> str
```

Same parameters as `render_pm_header` plus `halt_state`, `pending_orders`, `current_price_lookup`.

### 5. Tests

Tests at `tests/risk_guardrails/state_delivery/test_halt_mode.py`:

- **HaltState validator:** constructing a `HaltState` with both `daily_halt_active=False` and `cumulative_full_halt_active=False` raises `ValueError`.
- **Analyst halt-mode happy path:** banner renders verbatim; capital block is replaced (no `Available for new positions: $...`); other blocks (regime, sector headroom, directional, held positions, abandoned, hard blocks) match the normal renderer's output for the same inputs (regression by line-by-line comparison after stripping the banner + capital block).
- **Strategist halt-mode happy path:** banner renders verbatim; all other blocks preserved.
- **PM halt-mode happy path:** banner renders three lines; `Pending orders review:` block inserted with the documented row format; cross-constraint-impact block is replaced with the scoped variant; hard-blocks block has `OPEN: BLOCKED (halt mode)` and `ADD: BLOCKED (halt mode)` lines appended; all other blocks preserved.
- **PM halt-mode pending orders empty:** `Pending orders review:` block contains `  None` line.
- **PM halt-mode pending orders missing price:** `current_price_lookup` raising `KeyError` propagates as `ValueError` from the wrapper with the missing ticker in the message.
- **PM halt-mode cross-constraint impact empty:** scoped block emits `No pending risk-reducing actions; no projected impact.` line.
- **PM halt-mode hard-blocks block scenarios:**
  - No breaches, both flags True (normal renderer would omit) → wrapper emits a hard-blocks block with just `OPEN: BLOCKED (halt mode)` and `ADD: BLOCKED (halt mode)`.
  - Breaches present + both flags True → wrapper appends halt-mode action lines to the rendered breaches.
  - Breaches absent + `options_enabled=False` → wrapper emits the disabled-feature line followed by halt-mode action lines.
- **Determinism:** same inputs (including same `halt_state`) produce byte-identical output across repeated calls.
- **Banner uses daily-drawdown trigger:** `halt_state.daily_drawdown_pct` and `daily_drawdown_limit_pct` render in the banner; the wrapper does not infer these from `pm_view.drawdown` or `active_risk_parameters` (decoupled per the typed-input principle).
- **Cumulative-only halt:** when `daily_halt_active=False` and `cumulative_full_halt_active=True`, the banner still renders the daily drawdown row (the banner template is daily-pct-based; cumulative-trigger is signaled via `Mode:` line wording — but the design's worked example shows the banner with `daily drawdown` text regardless. Resolution: the design's banner template is daily-pct-format unconditionally; when only cumulative is active, the daily-pct values reflect the current daily drawdown — which may be 0% if cumulative tier 3 was reached gradually. The renderer trusts the typed input).
- **Blank-line discipline:** no two consecutive blank lines; no trailing blank.

Out of scope:
- Detection of halt-mode trigger conditions — the breach-behavior work tree owns trigger detection; this story takes a typed `HaltState` input.
- Modifying the strategist's per-position output shape (defensive_posture mode flag) — that lives in the strategist's prompt and output schema, not in the state-delivery header.
- Modifying the analyst's output shape (watchlist mode — lighter-weight entries) — same; lives in the analyst's prompt and output schema.
- The PM's actual command-vocabulary enforcement — the engine's T3 check rejects OPEN/ADD commands during halt mode regardless of what the header says; the header is a behavioral cue, not the enforcement point.
- Halt-mode + emergency-invocation co-occurrence handling — story 06 owns the emergency invocation header; the integration test in story 08 exercises both together.

## Notes

The three halt-mode wrappers share the banner helper and the canonical `HaltState` value object (imported from `breach_behavior`) but are otherwise distinct compositions. Per `feedback_simplify_before_building.md`, the wrappers do not factor common scaffolding through a shared "`render_halt_mode_audience` switch" — each audience's halt-mode contract has different block-level substitutions, and a switch would obscure those differences. Three separate functions, one per audience, mirror the design's three subsections.

The post-render injection pattern (`_inject_halt_mode_hard_block_lines`) is a localized exception to the otherwise-pure composition style. It exists because adding `additional_lines` to `render_hard_blocks_block` retroactively modifies story 03's contract, which violates the orchestrator's "do not modify story files" boundary unless cross-feature coordination is explicit. This story owns the injection helper; if multiple wrappers need similar primitive extensions in the future, the cleaner refactor is to extend the primitive (in a new story or a coordinated edit) rather than spread injection helpers across wrappers.

Per `feedback_no_inventing_component_names.md`, the wrapper function names use the design's mode names verbatim (`watchlist mode`, `defensive posture mode`, `risk reduction mode`). The Python identifiers translate to `render_analyst_header_halt_mode` / `render_strategist_header_halt_mode` / `render_pm_header_halt_mode` — the suffix `_halt_mode` is the disambiguator from the normal renderer. The mode names appear in the banner body verbatim.

Per `feedback_avoid_numeric_anchors.md`, the `HaltState` carries the daily drawdown numbers as data; the wrappers render them. No threshold values (100% of daily, 12% cumulative) appear in this story's code — the breach-behavior work tree owns those.

Per the design's halt-mode behavioral spec, the upstream pipeline (data ingestion, distillation, research, analyst, strategist) continues to run during halt mode; only the per-agent behavior shifts via mode flags. State-delivery's header job is to make the mode active and visible to each agent; the agent's prompt and output schema enforce the behavioral shift.

Cross-feature dependency callout: when the breach-behavior work tree ships halt-mode trigger detection, the upstream pipeline computes `HaltState` and passes it to whichever audience renderer is in play. For this story, hand-constructed `HaltState` fixtures are sufficient.

## Acceptance criteria

- [ ] `src/alphamind/risk_guardrails/state_delivery/halt_mode.py` exists with `render_analyst_header_halt_mode`, `render_strategist_header_halt_mode`, `render_pm_header_halt_mode` exposed (re-exported from `state_delivery/__init__.py`).
- [ ] `HaltState` is imported from `alphamind.risk_guardrails.breach_behavior` (the canonical source); no local declaration in state-delivery.
- [ ] `_render_halt_mode_banner(halt_state)` produces the documented `** HALT MODE ACTIVE ... **` line.
- [ ] Analyst halt-mode wrapper: banner inserted after envelope-open; capital block replaced with `New positions: BLOCKED (halt active)` and `Per-position max size: not applicable (halt mode)` rows; all other blocks preserved verbatim from `render_analyst_header`.
- [ ] Strategist halt-mode wrapper: banner with `Mode: DEFENSIVE POSTURE — focus on risk reduction for existing positions` line inserted; all other blocks preserved verbatim from `render_strategist_header`.
- [ ] PM halt-mode wrapper: banner with `Available actions: CLOSE, ADJUST, CANCEL only` and `Blocked actions: OPEN, ADD` lines; `Pending orders review:` block inserted with documented row format; cross-constraint-impact block replaced with scoped variant; hard-blocks block has `OPEN: BLOCKED (halt mode)` and `ADD: BLOCKED (halt mode)` lines appended.
- [ ] PM halt-mode pending orders empty → `  None` line; missing current price → `ValueError`.
- [ ] PM halt-mode hard-blocks no-breaches no-disabled-features case → block emitted with just halt-mode action lines (not omitted).
- [ ] All three wrappers' inputs and signatures match the documented keyword-only parameter lists.
- [ ] Constructing `HaltState(daily_halt_active=False, cumulative_full_halt_active=False, ...)` raises `ValueError` (validator inherited from the canonical breach-behavior model).
- [ ] No two consecutive blank lines; no trailing blank; closing `===` is immediately preceded by a non-blank line for each wrapper's output.
- [ ] Repeated calls with the same inputs produce byte-identical output for each wrapper.
- [ ] Regression: line-by-line comparison between halt-mode wrapper output and normal renderer output (with banner/replacements stripped) confirms no unintended block changes.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
