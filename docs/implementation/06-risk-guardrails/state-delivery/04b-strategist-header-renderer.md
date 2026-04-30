---
status: in_progress
completed_date:
commit_id:
---

# 04b — Strategist guardrail state header renderer

## Goal

Land the function that produces the complete strategist guardrail state header — the formatted `=== GUARDRAIL STATE ===` text block delivered at the top of the strategist's prompt. Composes the shared rendering primitives (story 03) with five strategist-specific blocks: per-position constraint proximity, sector exposure breakdown per position, drawdown state, regime-transition breaches, abandoned openings + abandoned position actions. Produces the verbatim text shape documented in [`state-delivery.md § Strategist guardrail state header`](../../../design/06-risk-guardrails/state-delivery.md#strategist-guardrail-state-header).

## Reading

- `docs/design/06-risk-guardrails/state-delivery.md` § Strategist guardrail state header — the authoritative format spec; the five strategist-specific blocks listed above are this story's audience surface. The block order in the design is the rendering order.
- `docs/design/06-risk-guardrails/state-delivery.md` § Why position-level detail — the rationale for per-position proximity and per-position sector breakdown; informs the column shape
- `docs/design/06-risk-guardrails/breach-behavior.md` § Escalation model — zone classification driving the per-position `[⚠ WARNING]` flag
- `docs/design/06-risk-guardrails/breach-behavior.md` § Cumulative drawdown response — the progressive-tier display below the daily/cumulative drawdown rows
- `docs/design/06-risk-guardrails/regime-adaptation.md` § Position handling when tightening creates breaches — the regime-transition breaches block surfaces these
- `docs/design/04-decision-layer/strategist.md` § Inputs — the strategist's input contract
- `src/alphamind/portfolio_state/consumers/strategist.py` — `StrategistView`, `StrategistPositionView`, `StrategistAbandonedAction` — the typed view this renderer consumes; also re-exports `AnalystAbandonedOpening` for the strategist's portfolio-awareness abandoned-openings block
- `src/alphamind/portfolio_state/snapshot.py` — `SectorExposureEntry`, `DirectionalExposure`, `PortfolioStateSnapshot` — additional inputs
- `src/alphamind/portfolio_state/records/capital.py` — `RiskBudgetConsumption`, `RiskBudgetEntry`, `ActiveRiskParameterSet`, `DrawdownState`, `DrawdownTier`, `RiskZone` — typed inputs for headroom + drawdown blocks
- `src/alphamind/portfolio_state/records/positions.py` — `PositionRecord`, `Direction`, `InstrumentType` — for per-position rendering
- `02-package-skeleton-and-config.md` — `state_delivery/strategist.py` is the target module; `StateDeliveryConfig` carries `abandoned_window.lookback_invocations`
- `03-shared-rendering-primitives.md` — primitives reused
- `04a-analyst-header-renderer.md` — sibling pattern for header-renderer composition; the shared shape is intentional
- `../regime-adaptation/02-package-skeleton-and-types.md` § 2c — canonical declaration of `RegimeTransitionBreach` (frozen dataclass with `position_id`, `rule_id`, `rule_label`, `current_value`, `new_limit_value`, `overage`, `unit`); imported here, not redeclared
- `../regime-adaptation/07-regime-transition-breach-detector.md` — the producer of the records this renderer consumes

## Depends on

- 02 (package skeleton + config)
- 03 (shared rendering primitives)
- **Cross-feature dependency:** same as 04a — the rules-and-limits work tree ships the populated `RiskBudgetConsumption`. For dispatch, that work tree's stories that produce a populated `RiskBudgetConsumption` and `ActiveRiskParameterSet` must be `done`; the renderer's tests use hand-constructed fixtures.
- **Cross-feature dependency:** the regime-adaptation work tree's `regime_adaptation/02-package-skeleton-and-types.md` ships the canonical `RegimeTransitionBreach` typed record this renderer imports. For dispatch, that story must be `done`. Test fixtures construct `RegimeTransitionBreach` instances directly via the canonical class.

## Scope

In scope, all under `src/alphamind/risk_guardrails/state_delivery/strategist.py`. Tests at `tests/risk_guardrails/state_delivery/test_strategist.py`.

### 1. Public function

```python
def render_strategist_header(
    *,
    strategist_view: StrategistView,
    invocation_id: str,
    timestamp: datetime,
    options_enabled: bool,
    short_selling_enabled: bool,
    active_sectors: tuple[str, ...],
    config: StateDeliveryConfig,
    sector_label_display: dict[str, str] | None = None,
    sector_resolver: Callable[[PositionRecord], str | None],
    regime_transition_breaches: tuple[RegimeTransitionBreach, ...] = (),
) -> str
```

- `strategist_view` — projected view from `project_strategist_view(snapshot)`. Carries per-position bundles (position + thesis + bracket + pending orders + modification trail), portfolio P/L, drawdown state, sector exposure rollup, directional exposure, risk budget, active risk parameters, intra-invocation changelog, recent PM decision log, abandoned openings (analyst-originated), abandoned actions (strategist-originated).
- `invocation_id`, `timestamp` — passed to `render_envelope_open`.
- `options_enabled`, `short_selling_enabled`, `active_sectors`, `sector_label_display` — same contract as 04a.
- `config` — `StateDeliveryConfig` carrying `abandoned_window.lookback_invocations`. The abandoned-* sections show only entries from the prior invocation per the config knob; the projection step is responsible for the upstream filter.
- `sector_resolver` — `Callable[[PositionRecord], str | None]` (already a type alias from `portfolio_state.computations.exposure`). Maps a position to its sector key for the per-position sector-breakdown block. Mirrors the resolver used at projection time.
- `regime_transition_breaches` — tuple of `RegimeTransitionBreach` records; empty default. When empty, the `Regime-transition breaches (if any):` block is omitted entirely (no header, no `(none)` line — matches the design's `(if any)` parenthetical).

`RegimeTransitionBreach` is the canonical frozen dataclass owned by the regime-adaptation work tree (declared in `regime_adaptation/types.py` per `regime-adaptation/02-package-skeleton-and-types.md` § 2c). Imported here:

```python
from alphamind.risk_guardrails.regime_adaptation import RegimeTransitionBreach
```

Canonical fields the renderer reads: `position_id`, `rule_id`, `rule_label`, `current_value` (the position's contribution), `new_limit_value` (the post-tightening limit), `overage` (`current_value - new_limit_value`), and `unit` (mirrors `RiskBudgetEntry.unit`, e.g., `"% of portfolio"`).

The breaches input is provided by the caller — the regime-adaptation work tree owns detection (per `regime-adaptation.md` § Strategist and PM context for regime-transition breaches and `regime_adaptation/07-regime-transition-breach-detector.md`). Here, the renderer takes the typed input and emits the documented format.

Returns the complete header text, terminated by the closing `===` line. No trailing newline.

### 2. Per-position constraint proximity block

```
Position-level constraint proximity:
  POS-NVDA-001: 4.2% of portfolio (max 5.0%) — P/L: -18% of cost (max loss: -30%) [⚠ WARNING]
  POS-AMD-002:  2.1% of portfolio (max 5.0%) — P/L: +5% of cost
  POS-JPM-003:  3.8% of portfolio (max 5.0%) — P/L: -2% of cost
  ...
```

- One row per `StrategistView.positions[i]` open or pending position, in the projection's order.
- Format per row:
  - Position ID, left-justified to the longest position-id width.
  - `:`, two spaces.
  - `position_weight_pct` (right-justified to 4-char width with one decimal place), trailing `%`.
  - ` of portfolio (max `, the regime-active per-position max (`per_position_max_size_pct` from `active_risk_parameters`), one decimal place, `%)`.
  - ` — P/L: `, signed unrealized P/L percentage from `pos.unrealized_pnl_pct` (sign always shown, one decimal place), `% of cost` (the field's semantics are P/L as a percentage of cost basis per `PositionRecord.unrealized_pnl_pct`).
  - When the position is an equity or strategy with a defined max-loss percentage from `active_risk_parameters` (`position_max_loss_equity_pct` / `position_max_loss_options_pct`), append ` (max loss: -{max_loss_pct}%)` with the loss-as-negative convention.
  - Trailing zone tag from `render_zone_tag(zone)` enclosed in square brackets, where `zone` is computed from the position's proximity to its per-position max via `_classify_position_zone(position_weight_pct, max_pct)`. Zone tag omitted entirely when zone is `RiskZone.NORMAL` (compresses normal rows per design's "normal-headroom rules compressed" principle).
- The per-position max-loss field is omitted for asset types without a configured max-loss rule under the active profile (rare; defensive against future asset types). The renderer reads `active_risk_parameters.entries` by rule_id; if absent for the position's instrument type, the suffix is dropped.
- Empty `strategist_view.positions` produces a `  None` line.

Helper `_classify_position_zone(value: float, limit: float) -> RiskZone` — returns the documented escalation zone (NORMAL <70%, WARNING 70–85%, CRITICAL 85–95%, BLOCKED ≥95%) from the breach-behavior escalation table. The thresholds 70/85/95 are the universal default; the per-rule overrides for daily/cumulative drawdown live in the rule registry, not here. `_classify_position_zone` uses the universal defaults; the per-position max-size rule has no per-rule override, so this is correct.

Helper `_render_position_proximity_block(...)` — pure private function; takes the positions tuple, active risk parameters, and returns the formatted block.

### 3. Sector exposure breakdown per position

```
Sector exposure breakdown (per position):
  Tech (18.3% / 25.0%):
    POS-NVDA-001: 4.2% (delta-adj)
    POS-AAPL-004: 3.1% (delta-adj)
    POS-MSFT-005: 2.8% (delta-adj) [options, delta 0.45]   [omitted if options_enabled: false]
    ...
  Semis (12.1% / 25.0%):
    ...
```

- One sector-group per active sector (in `active_sectors` order). The group header uses the display label, the sector's net-long delta-adjusted percentage, and the sector concentration limit from `active_risk_parameters.entry_by_rule_id("sector_concentration_pct")` (or sector-specific rule_id if differentiated; the rule registry currently uses one sector_concentration_pct per profile, applied uniformly across active sectors per the rule's regime multiplier semantics).
- Within each group, one row per position whose sector matches (resolved via `sector_resolver(pos)`), in input order.
- Per-row format: position ID, `:`, single space, `position_weight_pct` (one decimal place), `%`, ` (delta-adj)`. For options positions (`pos.instrument_type == InstrumentType.OPTION`), append ` [options, delta {delta:.2f}]` where `delta` is `pos.options_details.greeks.delta` (per-contract delta — the per-position effective delta is captured in `pos.delta_adjusted_exposure_usd`, but for the strategist's display the per-contract delta is the more recognizable quantity). For strategy positions, use `pos.strategy_details.strategy_greeks.delta`. If `options_enabled` is `False`, options positions cannot exist (feature-flag closure); the renderer raises `ValueError` if it encounters one.
- A sector with zero positions produces only its group header followed by an indented `(no positions)` line. (Matches the design's intent of always showing the sector header with limit context, even when empty.)
- A position whose `sector_resolver` returns `None` is not assigned to any group; it is rendered in a trailing `Unclassified:` group at the end of the block. Positions in `Unclassified` are rare (every position in the universe should have a sector); when present, they signal upstream classification gaps and are visible to the strategist for awareness.

Helper `_render_sector_breakdown_block(...)` — pure private function.

### 4. Drawdown state block

```
Drawdown state:
  Daily:      {current}% / {limit}% [{zone}]
  Cumulative: {current}% / {limit}% [{zone}]
  [If cumulative drawdown response active: current tier and restrictions in effect]
```

- Two rows always present (daily and cumulative).
- `current` for daily = `strategist_view.drawdown.current_drawdown_pct` (note: the snapshot's `DrawdownState.current_drawdown_pct` is cumulative; daily comes from `intraday_drawdown_pct`). Read the design's units carefully and pull from the right field — `intraday_drawdown_pct` is the daily measure (worst point today vs. opening equity); `current_drawdown_pct` is the cumulative measure (from HWM). The renderer asserts both are defined; if either is `None` raise `ValueError`.
- `limit` for daily = `strategist_view.active_risk_parameters.entry_by_rule_id("daily_drawdown_pct").value`; for cumulative = `cumulative_drawdown_pct`. Missing entries raise `ValueError`.
- `zone` for daily = `strategist_view.drawdown.daily_zone`; for cumulative = `strategist_view.drawdown.cumulative_zone`. Each rendered via `render_zone_tag`.
- Optional cumulative-tier line: when `strategist_view.drawdown.cumulative_tier is not None`, append a third indented line:
  ```
    Cumulative tier: {tier} — {restrictions text}
  ```
  - `{tier}` = the enum value rendered as `"constrained"` / `"heavily constrained"` / `"full halt"` per `DrawdownTier` mapping (`CONSTRAINED` → `"constrained"`, `HEAVILY_CONSTRAINED` → `"heavily constrained"`, `FULL_HALT` → `"full halt"`).
  - `{restrictions text}` = a short hardcoded string per tier from the design's progressive-response table:
    - `CONSTRAINED` → `"max position size 3%, max gross 80%, positions w/ unrealized loss > 10% flagged"`
    - `HEAVILY_CONSTRAINED` → `"max position size 2%, max gross 60%, positions w/ unrealized loss > 15% flagged"`
    - `FULL_HALT` → `"no new positions; orderly reductions only"`

Helper `_render_drawdown_state_block(...)` — pure private function.

### 5. Regime-transition breaches block

```
Regime-transition breaches (if any):
  POS-NVDA-001: 4.2% exceeds elevated regime limit of 3.5% — overage 0.7%
  Sector concentration (Tech): 28.0% exceeds elevated regime limit of 20.0% — overage 8.0%
  ...
```

The first row shows a per-position breach; the second shows an aggregate breach.

- Header line + one row per `RegimeTransitionBreach` entry.
- The regime label in the row matches `active_risk_parameters.regime_label`'s display string (`elevated`, `crisis`, etc.).
- The renderer emits two distinct row shapes based on `breach.position_id`:
  - **Per-position rule (`position_id` is set):** `{position_id}: {current_value:.1f}% exceeds {regime} regime limit of {new_limit_value:.1f}% — overage {overage:.1f}%`. Matches the design example verbatim. For breaches on non-`position_max_size_pct` per-position rules (e.g., `single_short_max_pct`), the row appends ` [{rule_label}]` so the strategist can disambiguate.
  - **Aggregate rule (`position_id` is None):** `{rule_label}: {current_value:.1f}% exceeds {regime} regime limit of {new_limit_value:.1f}% — overage {overage:.1f}%`. The position ID slot is replaced with the rule's display label. The strategist consults the `Sector exposure breakdown (per position)` block (rendered earlier in the same header) to identify which positions contribute to the breach.
- The trailing `%` is rendered literally; `breach.unit` is read for invariant checking (the renderer raises `ValueError` if `unit` is not a percentage form) but does not appear in the output for either row shape — both row formats use bare `%`.
- When `regime_transition_breaches` is empty, the entire block (header included) is omitted — the design's `(if any)` parenthetical means absence is silent, not announced.

Helper `_render_regime_transition_breaches_block(...)` — pure private function.

### 6. Abandoned openings + abandoned actions blocks

Two separate blocks per the design:

**Abandoned openings (portfolio awareness; analyst owns re-evaluation):**

```
Abandoned openings from prior invocation (portfolio awareness; analyst owns re-evaluation):
  {ENV-REC-n}  {direction} {TICKER} {asset_type}  {size}%  — abandoned at {timestamp} ({failure_reason})
  ...
  [or "None" if no OPEN commands were abandoned in the prior invocation]
```

Same row format as the analyst's abandoned-openings block (story 04a) — only the parenthetical heading differs. Helper `_render_strategist_abandoned_openings_block(...)`.

**Abandoned position actions (decide on current grounds whether to re-propose):**

```
Abandoned position actions from prior invocation (decide on current grounds whether to re-propose):
  {ENV-SA-n}: {ADD|ADJUST|CLOSE} on {POS-ID} — abandoned at {timestamp} ({failure_reason})
  {ENV-SA-ORD-n}: {CANCEL|modify} on {ORD-ID} — abandoned at {timestamp} ({failure_reason})
  ...
  [or "None" if no position-action commands were abandoned in the prior invocation]
```

- One row per `StrategistAbandonedAction` entry.
- Format key: when `command_type ∈ {ADD, ADJUST, CLOSE}`, the row uses `{ENV-SA-n}: {COMMAND} on {position_id}`; when `command_type == "CANCEL"`, the row uses `{ENV-SA-ORD-n}: CANCEL on {order_id}`. The renderer reads `entry.position_id` for the first three and `entry.order_id` for CANCEL; missing fields for the expected command type raise `ValueError`.
- Currently `consumers/strategist.py` defaults `command_type = "ADD"` because `CommandAbandonedDetail` does not surface command_type (lines 130–133 of `consumers/strategist.py`). The renderer emits whatever the projection passes through. When the upstream detail schema is extended in a follow-up story, the renderer continues to work without change.
- Empty `abandoned_actions` produces a `  None` line.

Helper `_render_strategist_abandoned_actions_block(...)`.

### 7. Composition: full strategist header

`render_strategist_header` produces (one blank line between blocks; no consecutive blanks; no trailing blank):

1. `render_envelope_open(invocation_id, timestamp)`
2. `render_regime_line(strategist_view.active_risk_parameters)`
3. *blank*
4. `render_capital_block(...)` — built from `strategist_view.cash_ledger` (stub: the strategist view does not surface available capital separately; pull from `risk_budget` per-position-max-size entry and the snapshot's `CashLedger.true_deployable_capital_usd` carried via the broader snapshot — the renderer takes `total_portfolio_value_usd: float` and `available_for_new_positions_usd: float` as additional keyword arguments to keep dependency direction clean). Updated signature note: `render_strategist_header` accepts `total_portfolio_value_usd: float` and `available_for_new_positions_usd: float` keyword arguments alongside `strategist_view`. The caller sources these from `snapshot.cash_ledger.true_deployable_capital_usd` and a portfolio-value computation upstream.
5. *blank*
6. `render_sector_headroom_block(...)` — same composition as 04a: filter `strategist_view.risk_budget.entries` to `sector_concentration_*` rules and emit in `active_sectors` order.
7. *blank*
8. `render_directional_headroom_block(...)` — same as 04a.
9. *blank — only if options block renders*
10. `render_options_headroom_block(...)` — same as 04a.
11. *blank*
12. `_render_position_proximity_block(...)`
13. *blank*
14. `_render_sector_breakdown_block(...)`
15. *blank*
16. `_render_drawdown_state_block(...)`
17. *blank — only if breaches present*
18. `_render_regime_transition_breaches_block(regime_transition_breaches, regime_label_display=...)` — when empty, omitted.
19. *blank*
20. `_render_strategist_abandoned_openings_block(strategist_view.abandoned_openings)`
21. *blank*
22. `_render_strategist_abandoned_actions_block(strategist_view.abandoned_actions)`
23. *blank — only if hard-blocks block renders*
24. `render_hard_blocks_block(breaching_entries=..., options_enabled=..., short_selling_enabled=...)` — uses the analyst/strategist phrasing default `"Hard blocks (do NOT recommend):"`.
25. `render_envelope_close()`

### 8. Tests

Tests at `tests/risk_guardrails/state_delivery/test_strategist.py`:

- **Happy path — full-system profile (4 sectors, 8 positions across 4 sectors with mix of equity + 2 options, 1 regime-transition breach on POS-NVDA-001, 1 abandoned opening, 1 abandoned action of each type, drawdown at CONSTRAINED tier):** rendered output equals the fixture string line-by-line.
- **Happy path — micro profile (no options, no shorts, 5 positions all in tech/semis, no breaches, no abandoned, normal drawdown):** rendered output matches; the regime-transition block, abandoned blocks (None lines), drawdown tier line, and hard-blocks block are all absent or `None`-filled per spec.
- **Empty positions:** `strategist_view.positions = ()` produces `  None` in the per-position proximity block and `(no positions)` lines in every sector group.
- **Cumulative tier rendering:** for each `DrawdownTier` enum value (`CONSTRAINED`, `HEAVILY_CONSTRAINED`, `FULL_HALT`), the cumulative-tier line uses the documented restrictions text.
- **Sector breakdown unclassified group:** a position whose `sector_resolver` returns `None` lands in a trailing `Unclassified:` group; positions with classified sectors do not.
- **Position zone tag compression:** a position with weight < 70% of max produces no trailing zone tag; ≥70% produces `[⚠ WARNING]`; ≥85% produces `[🔴 CRITICAL]`; ≥95% produces `[BLOCKED]`.
- **Daily drawdown source field:** the renderer reads `intraday_drawdown_pct` for daily and `current_drawdown_pct` for cumulative; mismatching the field surfaces in the rendered output.
- **Regime transition block omitted when empty:** `regime_transition_breaches=()` produces no `Regime-transition breaches` block whatsoever (no header, no `(none)`); the surrounding blank-line discipline holds.
- **Per-position breach row format:** a `RegimeTransitionBreach(position_id="POS-NVDA-001", rule_id="position_max_size_pct", current_value=4.2, new_limit_value=3.5, overage=0.7, ...)` renders to `POS-NVDA-001: 4.2% exceeds elevated regime limit of 3.5% — overage 0.7%` (matching the design example verbatim, no rule-label suffix for `position_max_size_pct`).
- **Aggregate breach row format:** a `RegimeTransitionBreach(position_id=None, rule_id="sector_concentration_tech", rule_label="Sector concentration (Tech)", current_value=28.0, new_limit_value=20.0, overage=8.0, ...)` renders to `Sector concentration (Tech): 28.0% exceeds elevated regime limit of 20.0% — overage 8.0%` (the rule label fills the position-id slot).
- **Per-position non-`position_max_size_pct` breach:** a per-position breach on `single_short_max_pct` renders with a trailing `[Single short max size]` rule-label suffix to disambiguate.
- **Mixed breach types in one block:** a tuple containing per-position and aggregate breaches renders both row formats in input order; no spurious blank lines between them.
- **Determinism:** same inputs, byte-identical output across repeated calls.
- **Feature-flag closure invariants:** same as 04a (options drift raises `ValueError`; short_selling drift raises `ValueError`; encountering an options position when `options_enabled=False` raises `ValueError`).
- **Missing required rule:** missing `daily_drawdown_pct`, `cumulative_drawdown_pct`, `position_max_size_pct`, `net_long_pct`, `gross_exposure_pct`, or any expected sector entry raises `ValueError` with the rule_id in the message.
- **Abandoned action command_type routing:** `ADD`/`ADJUST`/`CLOSE` rows render against `position_id`; `CANCEL` rows render against `order_id`; missing the expected field raises `ValueError`.
- **Blank-line discipline:** no two consecutive blank lines; no trailing blank; each omitted block does not leave a blank-line residue.

Out of scope:
- The analyst or PM header (stories 04a, 04c).
- Halt-mode wrapper (story 05).
- Emergency invocation header (story 06).
- The validation tool (story 07).
- The thesis-status display per position (`on-track`/`at-risk`/`stale`/etc.) — the strategist owns thesis-status classification in its output; the header does not surface it. The strategist's per-position bundle (`StrategistPositionView`) carries the thesis but the design's header format does not include a status column. The renderer omits it.
- The pending-orders summary block — pending orders are rendered as part of the strategist's input bundle (via `pending_orders` in `StrategistPositionView`), but the design's *guardrail state header* format does not include a pending-orders block. Rendering pending orders is a future story or owned by the strategist's prompt-assembly stage.

## Notes

The strategist header is the longest of the three audience headers (300–600 token budget per the design). The block ordering matters for the strategist's reasoning flow: regime → capital → headroom (universal blocks) → per-position proximity → per-sector breakdown → drawdown → regime-transition breaches → abandoned blocks → hard blocks. The renderer's composition mirrors this exactly.

Per the design's "Why position-level detail" rationale, the per-position proximity and sector breakdown blocks are what the strategist needs to propose intelligent remedies, trims, and adjustments. These blocks are the structurally distinguishing feature vs. the analyst header; the rendering primitives in story 03 deliberately do not own them because they are strategist-only.

The `regime_transition_breaches` argument exists because the regime-adaptation work tree owns breach detection — at story-write time, that detection layer is not yet implemented. The strategist header renderer accepts the typed input from any source (production, test fixture, replay-harness scenario). When the regime-adaptation work tree ships, the upstream piping passes the detected breaches to this renderer; no change here.

Per `feedback_no_inventing_component_names.md`, `RegimeTransitionBreach` is owned by `regime_adaptation` (single source of truth) and imported here unchanged. The PM renderer (story 04c) imports from the same source. No state-delivery-local declaration; no shared `state_delivery/types.py` for this type.

Per `feedback_no_inventing_component_names.md`, the helper names mirror the design's section names directly (`Position-level constraint proximity`, `Sector exposure breakdown (per position)`, `Drawdown state`, `Regime-transition breaches`, etc.). No new conceptual names introduced.

The cumulative-tier restrictions text is hardcoded per tier per the breach-behavior design's progressive-response table. This is a static mapping (three strings), not config — making it operator-tunable would create a configuration surface for cosmetic text that the design owns. If the design changes the restrictions, the renderer changes correspondingly.

Per `feedback_avoid_numeric_anchors.md`, the per-position zone classification uses the universal escalation thresholds (70/85/95) that come from the breach-behavior design — those are the documented universal defaults and are not "soft targets" or numeric anchors for LLM reasoning. The classifier returns a typed `RiskZone`; the renderer reads the typed zone and emits the corresponding tag. The thresholds appear once in `_classify_position_zone` and nowhere else.

Cross-feature dependency callout: same as 04a — once the rules-and-limits work tree ships the production `RiskBudgetConsumption` populator, this renderer's tests can move from hand-constructed fixtures to a real-snapshot integration test in story 08.

## Acceptance criteria

- [ ] `src/alphamind/risk_guardrails/state_delivery/strategist.py` exists with `render_strategist_header` exposed (re-exported from `state_delivery/__init__.py`).
- [ ] `RegimeTransitionBreach` is imported from `alphamind.risk_guardrails.regime_adaptation` (the canonical source); no local declaration in state-delivery.
- [ ] The function signature matches the documented keyword-only parameter list, including `total_portfolio_value_usd`, `available_for_new_positions_usd`, `sector_resolver`, and `regime_transition_breaches` defaulting to `()`.
- [ ] Full-system fixture (4 sectors, 8 positions, 1 regime-transition breach, 1 abandoned action of each type, CONSTRAINED tier) renders to a fixture string line-by-line.
- [ ] Micro fixture (no options, no shorts, no breaches, no abandoned, normal drawdown) renders to a fixture string with all conditional blocks omitted or `None`-filled per spec.
- [ ] Per-position rows omit the trailing zone tag for normal-zone positions and emit the correct tag for WARNING/CRITICAL/BLOCKED zones.
- [ ] Cumulative-tier line uses the documented restrictions text for each `DrawdownTier` enum value.
- [ ] Sector breakdown groups one position with no resolved sector under a trailing `Unclassified:` group.
- [ ] Empty `regime_transition_breaches` omits the entire block (no header line, no `(none)` line, no blank-line residue).
- [ ] Daily drawdown reads from `intraday_drawdown_pct`; cumulative drawdown reads from `current_drawdown_pct`.
- [ ] Abandoned action `command_type ∈ {ADD, ADJUST, CLOSE}` rows render against `position_id`; `CANCEL` rows render against `order_id`; missing required field raises `ValueError`.
- [ ] Feature-flag closure: encountering an options position when `options_enabled=False` raises `ValueError`; encountering `net_short_pct` in `risk_budget` when `short_selling_enabled=False` raises `ValueError`.
- [ ] Missing `daily_drawdown_pct`, `cumulative_drawdown_pct`, `position_max_size_pct`, `net_long_pct`, or `gross_exposure_pct` from `active_risk_parameters` / `risk_budget` raises `ValueError`.
- [ ] No two consecutive blank lines; no trailing blank line; the closing `===` is immediately preceded by a non-blank line.
- [ ] Repeated calls with the same inputs produce byte-identical output.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
