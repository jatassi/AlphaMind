# 01a — Document strategy direction & credit-risk semantics in position-model.md

# 01a — Document strategy direction & credit-risk semantics in position-model.md

## Goal

Update `docs/design/05-execution-layer/position-model.md` so the design doc states what position-level `direction` means for a multi-leg strategy and how net premium / payoff metrics behave for a net-credit strategy. This is the design-doc deliverable named in the parent acceptance criteria ("`position-model.md` documents what position-level `direction` means for a strategy"). Every other [ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) story cites the updated doc in its Reading list, so this lands first. Doc-only — no code.

## Reading

* `docs/design/05-execution-layer/position-model.md` § Base position, § Strategy position — the doc currently lists `Direction: long or short` as a base-position field and never assigns a strategy a direction.
* [ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) (parent issue) § Resolved design decisions — the three resolutions this story documents into the design doc.
* `src/alphamind/portfolio_state/records/positions.py` — `PositionRecord.direction`, `StrategyPositionDetails` (`net_premium_usd` / `max_profit_usd` / `max_loss_usd` / `breakeven_levels` / `strategy_greeks`), `StrategyLeg.direction` — the as-built shapes the doc describes.

## Depends on

Nothing. First story of the work tree.

## Scope

In scope: `docs/design/05-execution-layer/position-model.md` only.

### 1\. Strategy direction note (§ Strategy position)

Add prose stating that position-level long/short `direction` is **not economically meaningful** for a multi-leg strategy — it is a category error (an iron condor is neither long nor short). A strategy position carries `direction` as an inert `LONG` placeholder; every consumer derives directional sign from per-leg directions, net delta, or net premium and must not branch on the position-level field. Reference [ALP-591](https://linear.app/alphamind-jatassi/issue/ALP-591/make-position-level-direction-instrument-specific-it-is-a-category) as the tracked clean-refactor follow-up (making `direction` instrument-specific / optional).

### 2\. Credit-aware net premium and payoff (§ Strategy position)

Make explicit that **net premium is signed**: positive for a net debit (premium paid), negative for a net credit (premium received). Document that `max_loss_usd` is the strategy's worst-case P/L (a loss — negative, or negative-infinity for unbounded downside), `max_profit_usd` the best case, and that a strategy's **"capital at risk"** is the magnitude of `max_loss_usd`. The existing "Net premium: total debit paid or credit received" line stays — this adds the sign convention and the capital-at-risk definition around it.

### 3\. Base-position direction caveat (§ Base position)

Where the base-position field list gives `Direction: long or short`, add that this field is meaningful only for equity and single-leg options positions; strategy positions carry it as a placeholder (see § Strategy position).

### Out of scope

Code and test changes — each code story owns its own. The `direction` refactor itself is [ALP-591](https://linear.app/alphamind-jatassi/issue/ALP-591/make-position-level-direction-instrument-specific-it-is-a-category).

## Acceptance criteria

- [ ] `position-model.md` § Strategy position states position-level `direction` is a category error / non-meaningful for a strategy, and names per-leg direction / net delta / net premium as the directional-sign sources.
- [ ] § Strategy position documents net premium as signed (debit positive, credit negative) and defines a strategy's capital at risk as the magnitude of `max_loss_usd`.
- [ ] § Base position's `Direction` field note states the field is meaningful only for equity and single-leg options.
- [ ] The doc references [ALP-591](https://linear.app/alphamind-jatassi/issue/ALP-591/make-position-level-direction-instrument-specific-it-is-a-category) as the clean-refactor follow-up.
- [ ] No files outside `docs/` are modified.

## Verification

By inspection — the orchestrator reads the `position-model.md` diff and confirms the three edits land and read coherently. No automated test; doc-only story.
