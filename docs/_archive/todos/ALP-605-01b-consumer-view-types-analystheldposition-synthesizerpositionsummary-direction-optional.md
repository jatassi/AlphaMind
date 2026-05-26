# 01b — Consumer view-types: AnalystHeldPosition / SynthesizerPositionSummary direction → optional

# 01b — Consumer view-types: AnalystHeldPosition / SynthesizerPositionSummary direction → optional

## Goal

Make the two consumer view-types that mirror a held position's direction — `AnalystHeldPosition` and `SynthesizerPositionSummary` — carry a `Direction | None` field, projected via `position_direction()`, so a strategy position carries `None`; and update every renderer of those fields to show the strategy-type label instead of crashing on `None`. This is an end-to-end vertical: the type field, the portfolio-state producer, and the downstream renderers. After this story a strategy held position is delivered to the analyst and synthesizer without a fabricated long/short direction.

## Reading

* `ALP-591` (parent) § Pre-resolved decision (C) — consumer view-types carry optional direction.
* `ALP-604` (story 01a) — the `position_direction(record)` accessor this story projects through.
* `src/alphamind/portfolio_state/consumers/analyst.py` — `_project_held_positions`, builds `AnalystHeldPosition` from open/pending `PositionView`s.
* `src/alphamind/portfolio_state/consumers/synthesizer.py` — `_project_positions`, builds `SynthesizerPositionSummary`.
* The `AnalystHeldPosition` and `SynthesizerPositionSummary` class definitions (grep `class AnalystHeldPosition` / `class SynthesizerPositionSummary`) — the `direction` field being widened.
* `src/alphamind/risk_guardrails/state_delivery/analyst.py` — `_render_held_positions_block`, the `_DIRECTION_DISPLAY[position.direction]` render of `AnalystHeldPosition.direction`.
* `src/alphamind/analysis/synthesizer/` (`portfolio_tools.py`, `adapters.py`) — the renderer(s) of `SynthesizerPositionSummary.direction`.
* `ALP-596` ([ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) story 01e — "Generalize premium_at_risk") — touched analyst / state-delivery schemas. **Re-baseline:** read the merged `state_delivery/` and consumer modules as ground truth.

## Depends on

* `ALP-604` (story 01a, this work tree) — provides `position_direction()`.

## Scope

In scope: the `AnalystHeldPosition` / `SynthesizerPositionSummary` types and their producers and renderers. Tests under `tests/portfolio_state/` and the relevant `tests/risk_guardrails/` / `tests/analysis/` paths.

### 1\. AnalystHeldPosition direction → optional

Widen `AnalystHeldPosition.direction` to `Direction | None`. In `portfolio_state/consumers/analyst.py`, project the field via `position_direction(pos.record)` so a strategy held position yields `direction=None`. In `risk_guardrails/state_delivery/analyst.py` `_render_held_positions_block`, when `direction` is `None` render the position's strategy-type label (`details.strategy_type_label`) in the direction column instead of indexing `_DIRECTION_DISPLAY` with `None`. Equity / options rows render exactly as before.

### 2\. SynthesizerPositionSummary direction → optional

Widen `SynthesizerPositionSummary.direction` to `Direction | None`. In `portfolio_state/consumers/synthesizer.py`, project via `position_direction()`. Update every renderer of `SynthesizerPositionSummary.direction` (in `analysis/synthesizer/`) so a strategy summary renders the strategy-type label rather than `.direction.value` on `None`.

### Out of scope

* Direct `PositionView.direction` renders in the PM / strategist input bundles — story 02d (those render positions directly, not these view-types).
* `AnalystAbandonedOpening` / other envelope-level `direction` fields — not position projections, not in [ALP-591](https://linear.app/alphamind-jatassi/issue/ALP-591/make-position-level-direction-instrument-specific-it-is-a-category) scope.
* The `PositionRecord.direction` field flip — story 03.

## Acceptance criteria

- [ ] `AnalystHeldPosition.direction` and `SynthesizerPositionSummary.direction` are typed `Direction | None`.
- [ ] `consumers/analyst.py` and `consumers/synthesizer.py` project the field through `position_direction()`; a strategy `PositionView` yields a view-type instance with `direction=None`.
- [ ] `state_delivery/analyst.py`'s held-positions block renders a strategy row without error, showing the strategy-type label in the direction column; a test exercises a held strategy position.
- [ ] Every `SynthesizerPositionSummary.direction` renderer handles `None` without error; a test exercises a strategy summary.
- [ ] Equity and single-leg options held-position rendering is unchanged.
- [ ] The relevant `tests/portfolio_state/`, `tests/risk_guardrails/`, `tests/analysis/` paths pass under `uv run pytest <paths> --testmon -n auto`.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, `uv run lint-imports` are clean.

## Verification

Run pytest scoped to the touched test paths plus the lint chain. Confirm by inspection that both view-type `direction` fields are `Direction | None`, that the producers project via `position_direction()`, and that each renderer shows the strategy-type label for a `None`-direction strategy. Strategy rendering is covered by the new tests.
