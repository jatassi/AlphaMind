# 01a — position_direction() accessor + portfolio-state computation reads

# 01a — position_direction() accessor + portfolio-state computation reads

## Goal

Introduce `position_direction()` — the canonical accessor for a position's directional sign — and migrate every `PositionRecord` / `PositionView` direction read inside `portfolio_state/` (the assembler and the `computations/` modules) onto it or off direction entirely. The accessor returns a `Direction` for an equity / single-leg options position and `None` for a multi-leg strategy. This is the foundational story: stories 01b and 02a–02d all consume `position_direction()`, and story 03's field flip relies on every consumer routing through it. No persisted-field or codec change here — `PositionRecord.direction` stays a non-optional `Direction` until story 03.

## Reading

* `docs/design/05-execution-layer/position-model.md` § Strategy position (the [ALP-592](https://linear.app/alphamind-jatassi/issue/ALP-592/01a-document-strategy-direction-and-credit-risk-semantics-in-position) placeholder note) and § Base position — orientation on what direction means per instrument.
* `ALP-591` (parent) § Pre-resolved decisions (A), (B) — the accessor contract and the deferred field flip.
* `src/alphamind/portfolio_state/records/positions.py` — `PositionRecord`, `Direction`, the `EquityPositionDetails` / `OptionsPositionDetails` / `StrategyPositionDetails` payloads, the `instrument_type` property. The accessor lands here.
* `src/alphamind/portfolio_state/views/positions.py` — `PositionView` and its `direction` pass-through property (stays until story 03).
* `src/alphamind/portfolio_state/assembler.py` — `_build_position_view`; the `compute_unrealized_pnl_usd` / `compute_distance_to_target_usd` / `compute_distance_to_stop_usd` calls passing `position.direction`.
* `src/alphamind/portfolio_state/computations/positions.py` — `compute_market_value_usd`, `compute_delta_adjusted_exposure_usd` (both already `raise` for a strategy payload).
* `src/alphamind/portfolio_state/computations/exposure.py` — `compute_sector_exposure`, the long/short delta-adjusted-exposure bucket split.
* `ALP-599` ([ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) story 03a — "Strategy unrealized P/L percentage") — reshapes `assembler.py`'s strategy P/L path. **Re-baseline:** read the merged `assembler.py` as ground truth before implementing.

## Depends on

* `ALP-588` (parent feature) — the whole [ALP-591](https://linear.app/alphamind-jatassi/issue/ALP-591/make-position-level-direction-instrument-specific-it-is-a-category) tree is hard-gated on it; [ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) reshapes `assembler.py` and the portfolio-state computations. Re-read the merged code before implementing.

## Scope

In scope, under `src/alphamind/portfolio_state/`. Tests under `tests/portfolio_state/`.

### 1\. The `position_direction()` accessor

Add to `records/positions.py` a function `position_direction(record: PositionRecord) -> Direction | None` that returns `None` when `record.details` is a `StrategyPositionDetails`, else `record.direction`. Its docstring states the contract: equity and single-leg options positions are long or short; a multi-leg strategy is neither, and this is the one accessor for position-level direction — consumers must not read `PositionRecord.direction` / `PositionView.direction` directly.

The accessor must be correct **before** story 03's field flip: pre-flip, `record.direction` is still a non-optional `Direction` and a strategy carries the inert `LONG` placeholder; the `isinstance` check yields `None` for a strategy regardless. Consumers holding a `PositionView` call `position_direction(view.record)`.

### 2\. Migrate portfolio-state direction reads

Every `PositionRecord` / `PositionView` direction read in `portfolio_state/` either routes through `position_direction()` or is eliminated where a better signed value already exists:

* `assembler.py` `_build_position_view` — the calls passing `position.direction` into `compute_unrealized_pnl_usd` / `compute_distance_to_target_usd` / `compute_distance_to_stop_usd`. A strategy must not pass a position-level direction into a direction-keyed computation; route the strategy case through its own P/L / distance path (re-baseline against [ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) story 03a's merged split) or, where a computation genuinely needs a `Direction`, take it from `position_direction()` and assert the non-strategy narrowing.
* `computations/positions.py` `compute_market_value_usd`, `compute_delta_adjusted_exposure_usd` — both already `raise` for a `StrategyPositionDetails` payload before the direction read; route the read through `position_direction()` and assert non-`None` (the strategy case is already unreachable).
* `computations/exposure.py` `compute_sector_exposure` — the long/short bucket split currently keys on `pos.direction`. Bucket instead by the sign of the already-signed `delta_adjusted_exposure_usd` on the `PositionView` — this is uniform across all instrument types and removes the direction read entirely.

### 3\. `_check_equity_direction_fields` stays as-is

`records/positions.py` `_check_equity_direction_fields` reads `self.direction` but returns early for any non-`EquityPositionDetails` payload, so it is provably equity-only. Leave it untouched — story 03 adjusts it for the field flip.

### Out of scope

* The `PositionRecord.direction` field flip, the codec, and the SQL migration — story 03.
* The `PositionView.direction` property removal — story 03 (it must survive while 02a–02d migrate their layers).
* `AnalystHeldPosition` / `SynthesizerPositionSummary` direction fields — story 01b.
* Direction reads in risk-guardrails / execution / decision — stories 02a–02d.

## Acceptance criteria

- [ ] `position_direction` is defined in `portfolio_state/records/positions.py`, returns `None` for a strategy `PositionRecord` and the record's `Direction` for an equity / single-leg options `PositionRecord`, and is importable.
- [ ] A unit test constructs a strategy `PositionRecord` and asserts `position_direction()` returns `None`; constructs equity and options `PositionRecord`s and asserts it returns the record's direction.
- [ ] No site in `portfolio_state/assembler.py` or `portfolio_state/computations/` reads `PositionRecord.direction` / `PositionView.direction` directly — every read routes through `position_direction()` or is replaced by a signed-value read.
- [ ] `compute_sector_exposure` buckets a position by the sign of its delta-adjusted exposure; a test with a net-short-delta strategy confirms it lands in the short bucket.
- [ ] `compute_market_value_usd` / `compute_delta_adjusted_exposure_usd` behavior is unchanged for equity and options positions.
- [ ] `tests/portfolio_state/` passes under `uv run pytest tests/portfolio_state/ --testmon -n auto`.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, `uv run lint-imports` are clean.

## Verification

Run `uv run pytest tests/portfolio_state/ --testmon -n auto` and the lint chain. Confirm by inspection that `position_direction()` is the only position-direction read path left in the migrated files (or the read was replaced by a signed-value read) and that the strategy case of `compute_sector_exposure` buckets by net-delta sign. The accessor's strategy-`None` behavior is covered by the new unit test.
