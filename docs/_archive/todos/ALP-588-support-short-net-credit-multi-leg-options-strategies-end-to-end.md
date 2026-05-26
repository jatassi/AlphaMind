# Support SHORT / net-credit multi-leg options strategies end-to-end

## Motivation

The analyst can specify multi-leg options strategies, but the engine only supports **net-debit** (long-premium) structures end-to-end. Net-**credit** strategies — bear call spreads, bull put spreads, iron condors sold for credit, short strangles/straddles — are a core options tool the analyst is currently missing.

The position-model design doc explicitly contemplates them: `docs/design/05-execution-layer/position-model.md` defines net premium as "total debit paid **or credit received** across legs" and lists "iron condor" as a supported strategy type. There is no documented rationale restricting strategies to debit/long — the LONG-only behavior is an undocumented implementation default, not a design decision (the doc's Strategy-position section never assigns the position a direction at all).

This surfaced during [ALP-582](https://linear.app/alphamind-jatassi/issue/ALP-582/debug-pos-07-pl-inconsistency-19900percent-dollar4895-on-msft-against): the fix there made `compute_strategy_market_value_usd` direction-aware (PR #128), which exposed how much of the rest of the stack still assumes a strategy is long/debit. The goal here is to **enable** net-credit strategies properly across every layer — not to guard them out.

## Design and architecture

* [Position model](<docs/design/05-execution-layer/position-model.md>) — § Strategy position; net premium is "debit paid or credit received"; the supported strategy-type list includes iron condor.
* [Orders and brackets](<docs/design/05-execution-layer/orders-and-brackets.md>) — § P/L-based bracket legs; a strategy bracket references the strategy's net P/L.
* [Reg T margin attribution](<docs/design/05-execution-layer/regt-margin-attribution.md>) — defined-risk strategies require margin equal to max loss, not summed naked legs.
* [Analyst output schema](<docs/design/04-decision-layer/analyst-output-schema.md>) — the `premium_at_risk` field.

## Current state — where the debit / LONG-only assumption lives

`compute_strategy_market_value_usd` is already direction-aware (PR #128) and sums signed leg values correctly — that primitive does not need work. Everything below does.

| Layer | Site | Assumption → effect on a net-credit strategy |
| -- | -- | -- |
| Execution — OPEN write path | `execution/write_paths/phase2/open.py:455` | `_build_pending_position` raises `NotImplementedError` for `StrategyInstrument` — strategy positions cannot be created in production at all today; only the debug-e2e seed creates them. |
| Execution — OPEN write path | `execution/write_paths/phase2/open.py:283` | `_direction_from_instrument` hard-codes `Direction.LONG` for every `StrategyInstrument`. |
| Execution — bracket build | `execution/write_paths/phase2/open.py:669` | TAKE_PROFIT leg wired `GTE` (fires when underlying rises) regardless of the strategy's payoff shape. |
| Decision — PM envelope | `decision/portfolio_manager/submit_envelope/process.py:430` & `:509` | Strategy validation request hard-codes `Direction.LONG`; guardrail sees the wrong direction, overstating `net_long_pct`. |
| Portfolio state — P/L % | `portfolio_state/computations/positions.py:152` | `compute_unrealized_pnl_pct` divides by `cost_basis`; for a credit strategy `cost_basis = net_premium_usd < 0`, so the sign **inverts** — a profitable credit strategy displays as a loss. |
| Portfolio state — assembler | `portfolio_state/assembler.py:412` | `cost_basis = details.net_premium_usd` (negative for a credit). |
| Risk metrics | `scheduler/debug_e2e/seed.py:338` | `max_profit_usd` / `max_loss_usd` are stubbed `0.0` — no production path computes them. `execution/position_model/strategy_payoff.py` already computes them credit-aware, but is disconnected from the write path. |
| Reg T margin | `execution/regt_margin_attribution/regt_margin.py:76` | `_strategy_position_margin` sums per-leg naked margin with no spread offset — a defined-risk credit spread is vastly over-margined. |
| Guardrails — greeks | `risk_guardrails/library_snapshot.py:92` | Portfolio greek sign comes from position-level `direction` (always LONG); a net-short-delta credit strategy contributes the wrong-signed delta. |
| Guardrails — loss zone | `risk_guardrails/state_delivery/primitives.py:408` | Loss-zone classifier reads the inverted `unrealized_pnl_pct`, firing WARNING/CRITICAL on a winning credit strategy. |
| Decision — analyst/PM schema | `decision/analyst/models.py:192`, `risk_guardrails/state_delivery/validation_tool.py:113` | `premium_at_risk` is `gt=0` and debit-centric ("premium paid"); a credit strategy's risk is (strike width − credit), with no natural field. |
| Execution — brackets | `execution/continuous_monitor/bracket_stops/task.py:272` | P/L-target bracket firing is explicitly disabled for strategy positions. |
| Execution — MLEG close | `execution/oms/submit_engine_envelope.py:484` | Close-order leg intent hard-coded `*_to_open` instead of `*_to_close` — a pre-existing bug for any strategy close. |
| Test harness | `scheduler/debug_e2e/seed.py:264` | `_strategy_leg_premiums` explicitly raises on a net credit — no credit strategy is exercised in any end-to-end test. |

## Resolved design decisions

The open questions from the original filing were settled at drafting time (`/draft-user-stories`, 2026-05-19) and baked into the stories; the orchestrator does not re-surface them.

**Position-level direction for a strategy.** Long/short is a category error for a multi-leg strategy — no value of the field is economically meaningful. Removing `direction` from the shared `PositionRecord` (or making it instrument-specific / optional) is a large cross-cutting refactor of a field consumed at hundreds of sites, and is deliberately out of [ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) scope. Within scope: a strategy position keeps `direction = LONG` as an inert placeholder, and the fix is that **no strategy consumer may branch on the position-level direction** — every directional sign is derived from per-leg directions, net delta, or net premium. Story 01a documents this in `position-model.md` as a known category error. The clean refactor is tracked separately as [ALP-591](https://linear.app/alphamind-jatassi/issue/ALP-591/make-position-level-direction-instrument-specific-it-is-a-category).

**premium_at_risk for credit strategies.** The field is redefined in place — same name, new semantics: "capital at risk" = the magnitude of the strategy's max loss (premium paid for a net-debit position; strike-width minus credit for a net-credit position). No new field, no rename. Story 01e applies this across the analyst output schema, the OMS command wire format, the guardrail validation tool, the four design docs that mention it, and the PM prompt.

**Strategy P/L-target bracket evaluation.** Full scope: story 03c re-enables strategy P/L-target bracket evaluation end-to-end. The current code disables it at two layers — `bracket_stops/task.py` short-circuits strategy positions, and `triggers.py` raises `NotImplementedError`.

## Cross-feature dependencies

No cross-feature gates. [ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) is a remediation across already-landed execution / decision / risk-guardrail features, so every dependency is internal to this work tree (see the dependency graph). The stories touch `PositionRecord` / `StrategyPositionDetails` (Position & thesis model), the Phase 1/2 write paths (State persistence), the OMS engine envelope (OMS commands), the Reg T margin module (Reg T margin attribution), the guardrail validation tool + library snapshot (Guardrail enforcement / State delivery), the bracket-stop watcher (Continuous monitor), and the debug-e2e seed (Debug e2e mode). All of those features are `done`; no sibling work tree is being drafted concurrently.

## Pre-resolved configuration decisions

The following were settled at drafting time and baked into the relevant stories. The orchestrator does not surface them at dispatch.

**(A) Payoff-metric computation site.** The OPEN write path builds a strategy position as a zeroed skeleton — legs at zero `contract_count`, `net_premium_usd` / `max_profit_usd` / `max_loss_usd` at zero, empty `breakeven_levels` — matching the existing skeleton convention for equity and single-leg options. Phase 1's strategy entry-fill recomputes those four metrics from the filled legs. Story 01c builds the skeleton; story 02 recomputes.

**(B) Strategy unrealized P/L percentage.** Strategy P/L in USD is already correct (signed market value minus net premium). Only the percentage inverts, because the assembler divides by `net_premium_usd`, which is negative for a credit. The fix: the strategy P/L percentage uses the magnitude of `max_loss_usd` (capital at risk) as the denominator — non-inverting and uniform for debit and credit. Story 03a. A strategy with unbounded max loss reports a zero percentage.

**(C) Strategy greeks are net-signed.** `StrategyPositionDetails.strategy_greeks` carries the strategy's net-signed aggregate — a net-short-delta strategy has a negative `strategy_greeks.delta`. Story 01b ships the `compute_strategy_greeks` aggregation primitive (a contract-weighted average of signed per-leg greeks); story 01d's guardrail consumers read the net-signed value directly and never re-apply the position-level direction.

**(D) MLEG strategy-close leg intent.** A strategy-close engine envelope reverses every leg: a leg opened LONG is sold-to-close, a leg opened SHORT is bought-to-close. Story 01f fixes both the leg `side` and the `position_intent` (currently hard-coded to the open-side `*_to_open`).

**(E) debug-e2e seed path.** The seed builds the net-credit strategy position row directly, bypassing the OPEN and Phase 1 write paths exactly as it does for every other synthetic position. The seeded strategy is exercised on the read side — P/L, margin, exposure, greeks, bracket evaluation — through the 12-phase pipeline. The OPEN and Phase 1 write paths for strategies are covered by stories 01c and 02's own tests, not by debug-e2e.

**(F) Strategy bracket ownership.** Story 01c creates the strategy position only; it does not own the strategy bracket. Story 03c owns the full strategy-bracket vertical — the OPEN-path bracket build (a P/L-anchored take-profit leg), `actual_entry_price` anchoring, and the `bracket_stops` / `triggers.py` evaluation. 03c is `blockedBy` 01c because both edit `open.py`.

## Dependency graph

```
Wave 1  --  01a  01b  01c  01d  01e  01f   (parallel; no blockers)
Wave 2  --  02                             (blockedBy 01b)
Wave 3  --  03a  03b  03c                  (parallel)
Wave 4  --  04                             (integration)

Edges (blockedBy):
  02   <-- 01b
  03a  <-- 02
  03b  <-- 02
  03c  <-- 01c, 02
  04   <-- 01b, 01d, 03a, 03b, 03c
```

Story map:

* **01a** — Document strategy direction & credit-risk semantics in position-model.md
* **01b** — Strategy net-premium & net-greeks aggregation primitives
* **01c** — OPEN write path: create strategy positions
* **01d** — Guardrail layer: strategy directional exposure & greeks from legs
* **01e** — Generalize premium_at_risk to capital-at-risk semantics
* **01f** — MLEG strategy-close leg intent
* **02** — Phase 1 strategy entry-fill payoff recompute
* **03a** — Strategy unrealized P/L percentage (non-inverting denominator)
* **03b** — Reg T margin: defined-risk strategy capped-loss
* **03c** — Strategy P/L-target bracket: build + evaluation
* **04** — debug-e2e: seed a net-credit strategy end-to-end

## Notes for the orchestrator

* **Architectural invariant — no strategy consumer branches on position-level direction.** Per the resolved decision above, a strategy's `direction` is an inert `LONG` placeholder. Fail verification on any story that reads `position.direction` to make a directional decision for a `StrategyPositionDetails` position — the sign must come from per-leg directions, net delta, or net premium.
* **Sign-convention invariants the stories must keep consistent:** `net_premium_usd` is debit-positive / credit-negative; `max_loss_usd` is a loss (negative, or negative-infinity for unbounded downside); `max_profit_usd` is a profit (positive, or positive-infinity); `strategy_greeks` is net-signed. The `strategy_payoff.py` module already follows these — new stories must not introduce a competing convention.
* **Model selection:** story 01a is doc-only and 01f is a small sign-correctness fix — both are Sonnet candidates. Every other story turns on sign-convention or payoff-geometry judgment — default to Opus.
* **Cumulative state:** `net_premium_usd` / `max_profit_usd` / `max_loss_usd` are populated by story 02; stories 03a, 03b, and 03c all consume them. Land 02 before that wave.
* **Surfacing conditions** (pause and ask the operator rather than improvise):
  * Story 01c — the guardrail validation metadata's `greeks` for a strategy is documented as the *average* across legs, not a net. 01c must not populate `strategy_greeks` from it. If a story finds the validation library actually produces a usable net strategy greek, surface before relying on it.
  * Story 01d — if the guardrail-evaluation library already honors per-leg directions when projecting strategy exposure, the `net_long_pct` overstatement is a false alarm; the fix degrades to an inspection note. Surface that finding rather than forcing a change.
  * Production population of live strategy greeks (continuous-monitor refresh) may be a pre-existing gap not covered by any story here. If a story finds the monitor does not refresh strategy greeks, surface it as a candidate follow-up rather than expanding scope.

## Acceptance criteria

* A net-credit strategy (e.g. an iron condor sold for credit) can be proposed by the analyst, created as a production position via the OPEN write path, and flows through P/L, margin, exposure, and brackets with correct signs and values.
* Strategy unrealized P/L and P/L % are correct for a negative net premium — a profitable credit strategy never displays as a loss, and the loss-zone classifier never fires on a winning one.
* `max_profit_usd` / `max_loss_usd` are populated for live strategy positions (credit and debit) by wiring in the existing `strategy_payoff.py` computations — not stubbed `0.0`.
* Reg T margin for a defined-risk credit spread reflects the spread's capped loss, not summed naked-leg margin.
* The debug-e2e harness seeds at least one net-credit strategy and exercises it end-to-end.
* `position-model.md` documents what position-level `direction` means for a strategy.

## Out-of-scope follow-ups

* **Remove position-level direction from the shared position record (**[ALP-591](https://linear.app/alphamind-jatassi/issue/ALP-591/make-position-level-direction-instrument-specific-it-is-a-category)**).** The genuinely-clean fix for the strategy direction category error — making `direction` instrument-specific or optional rather than a required field on the shared `PositionRecord` — is a large cross-cutting refactor and is intentionally not part of [ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end). Filed as a standalone issue, [ALP-591](https://linear.app/alphamind-jatassi/issue/ALP-591/make-position-level-direction-instrument-specific-it-is-a-category), outside this work tree.
* **Production strategy-greeks refresh.** If the continuous monitor does not currently recompute `strategy_greeks` net-signed on its greeks-refresh cadence, that is a pre-existing gap; [ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) makes the read paths correct and the debug-e2e seed populates net-signed greeks, but production population is a candidate follow-up.

## Related

[ALP-582](https://linear.app/alphamind-jatassi/issue/ALP-582/debug-pos-07-pl-inconsistency-19900percent-dollar4895-on-msft-against) (the strategy P/L bug whose fix exposed this); [ALP-591](https://linear.app/alphamind-jatassi/issue/ALP-591/make-position-level-direction-instrument-specific-it-is-a-category) (the clean direction-refactor follow-up); PR #128 (direction-aware strategy market value); PR #129 (debug-e2e seed strategy leg premiums).
