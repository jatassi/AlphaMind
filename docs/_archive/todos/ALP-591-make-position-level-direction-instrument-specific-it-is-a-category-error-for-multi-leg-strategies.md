# Make position-level `direction` instrument-specific — it is a category error for multi-leg strategies

Make position-level `direction` honestly optional on the shared `PositionRecord` — `Direction | None`, `None` for multi-leg strategies — retiring the category error where a required field carries an inert `LONG` placeholder no strategy consumer can meaningfully read.

## Problem

`PositionRecord.direction` is a required field (`direction: Direction`, LONG | SHORT) on the shared position record consumed by every instrument type — equity, single-leg options, and multi-leg strategies. For a multi-leg strategy, position-level long/short is a **category error**: a strategy's economics are fully described by its per-leg directions, net delta, net premium, and signed market value, and no single value of `direction` is meaningful (an iron condor is neither long nor short).

The OPEN write path hard-codes `direction = Direction.LONG` for every strategy as an inert placeholder (`execution/write_paths/phase2/open.py` `_direction_from_instrument`). The result is a required field that lies for one instrument type.

## Why this is a separate issue

Removing or reshaping `direction` on `PositionRecord` is a cross-cutting refactor — the field is read across the portfolio-state, risk-guardrails, execution, and decision layers. [ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) ("Support SHORT / net-credit multi-leg options strategies end-to-end") deliberately scoped it out to keep credit-strategy enablement tractable: [ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) makes every strategy consumer derive directional sign from per-leg data rather than the position-level field, documents the field as a known category error in `position-model.md`, but leaves the placeholder in place. This issue tracks the clean fix.

## Design and architecture

* [Position model](<docs/design/05-execution-layer/position-model.md>) — § Base position, § Strategy position. § Strategy position currently carries the [ALP-592](https://linear.app/alphamind-jatassi/issue/ALP-592/01a-document-strategy-direction-and-credit-risk-semantics-in-position) placeholder note; story 03 rewrites it to the resolved optional-direction shape.
* [ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) — the in-flight feature this cleans up after; its "Resolved design decisions § Position-level direction" documents the placeholder [ALP-591](https://linear.app/alphamind-jatassi/issue/ALP-591/make-position-level-direction-instrument-specific-it-is-a-category) retires.

## Cross-feature dependencies

The entire [ALP-591](https://linear.app/alphamind-jatassi/issue/ALP-591/make-position-level-direction-instrument-specific-it-is-a-category) tree is **hard-gated on** [ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end). [ALP-591](https://linear.app/alphamind-jatassi/issue/ALP-591/make-position-level-direction-instrument-specific-it-is-a-category) is a clean-up of code [ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) is actively reshaping; dispatching before [ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) lands would build every story against a stale baseline.

* **Story 01a (this work tree)** is `blockedBy` [ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) (the parent feature). Before dispatching story 01a, verify [ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) is `Done` and its PR merged to `main`. As of drafting, [ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) wave 1 (01a–01f) is `Done`; wave 2 (02, 03a–03c, 04) is `Todo`.
* **Coordination:** [ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) rewrites nearly every file in this tree's scope — `open.py`, `phase1.py`, `regt_margin.py`, `library_snapshot.py`, `submit_engine_envelope.py`, `bracket_stops/*`, `debug_e2e/seed.py`, the portfolio-state assembler. Every [ALP-591](https://linear.app/alphamind-jatassi/issue/ALP-591/make-position-level-direction-instrument-specific-it-is-a-category) story must re-baseline against the post-[ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) merged code (see Notes for the orchestrator).

## Pre-resolved configuration decisions

These choices were settled at drafting time and baked into the relevant stories. The orchestrator does not surface them at dispatch.

**(A) Storage shape — optional field, not instrument-specific relocation.** `PositionRecord.direction` becomes `Direction | None`; a `__post_init__` validator ties `direction is None` to a `StrategyPositionDetails` payload (strategy implies `None`; equity / single-leg options imply non-`None`). `direction` is not moved onto `EquityPositionDetails` / `OptionsPositionDetails` — the validator gives the same illegal-state-unrepresentable guarantee without touching \~87 details-constructor sites. Story 03.

**(B) Canonical accessor.** A single accessor `position_direction(record)` returning `Direction | None` lives in `portfolio_state/records/positions.py` — a `Direction` for equity / single-leg options, `None` for a strategy. Every consumer reads direction through it; the `PositionView.direction` pass-through property is removed. Story 01a introduces it; story 03 removes the property.

**(C) Consumer view-types carry optional direction.** The consumer view-types that mirror a position's direction — `AnalystHeldPosition`, `SynthesizerPositionSummary` — take a `Direction | None` field; a strategy projects `None` and its renderers show the strategy-type label. Story 01b owns this end-to-end (field, producer, renderers).

**(D) Persisted column stays nullable, not relocated.** `positions.direction` remains a top-level SQL column, made `NULL`-able; strategy rows persist `NULL`. A batch `ALTER` migration plus a one-time `UPDATE positions SET direction = NULL WHERE instrument_type = 'STRATEGY'` backfill — no move into `details_json`. Story 03.

**(E) Proposal-side category error is a separate follow-on.** The identical category error on the guardrail-evaluation library-input types (`ProposedDelta.direction`, `ExistingPosition.direction`) and the decision-layer request instruments is out of scope, tracked as [ALP-603](https://linear.app/alphamind-jatassi/issue/ALP-603/strategy-direction-category-error-on-guardrail-evaluation-proposal). [ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) story 01d already made those consumers leg-derived.

**(F) No per-feature verify story.** `scripts/verify_debug_e2e.py` covers the pipeline; [ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) story 04 seeds a net-credit strategy end-to-end. The `position-model.md` update folds into story 03.

## Dependency graph

```
ALP-588   (in-flight feature — entire tree gated on it)
   |
  01a      accessor + portfolio-state computation reads
   |
   +--- parallel wave, all blockedBy 01a:
   |       01b  consumer view-types (AnalystHeldPosition, ...)
   |       02a  risk-guardrails direction reads
   |       02b  execution write-path direction reads
   |       02c  execution monitor / OMS / margin / CA reads
   |       02d  decision-layer direction reads
   |
  03       flip the field + codec + SQL migration + doc

Edges (blockedBy):
  01a  <-- ALP-588
  01b  <-- 01a
  02a  <-- 01a
  02b  <-- 01a
  02c  <-- 01a
  02d  <-- 01a
  03   <-- 01b, 02a, 02b, 02c, 02d
```

## Notes for the orchestrator

* **Hard gate —** [ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) **must be** `Done` **and merged to** `main` before dispatching story 01a. [ALP-591](https://linear.app/alphamind-jatassi/issue/ALP-591/make-position-level-direction-instrument-specific-it-is-a-category) is a clean-up of code [ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) is actively reshaping.
* **Re-baseline every story against post-**[ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) **as-built.** Each story's Reading list names the [ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) story that touched its files. The implementer must read the current merged code as ground truth — the Scope sections describe the pre-[ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) shape only as orientation, not as a contract. [ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end)'s implementers are themselves correcting premises mid-flight (e.g. [ALP-597](https://linear.app/alphamind-jatassi/issue/ALP-597/01f-mleg-strategy-close-leg-intent)'s "premise correction").
* **Architectural invariant —** `position_direction()` **is the sole accessor.** Fail verification on any story (01b / 02\*) that leaves a direct `.direction` read on a `PositionRecord` / `PositionView` outside `position_direction` itself and the persistence codec. Story 03's field flip relies on every consumer already routing through the accessor — a missed site reddens `mypy` at story 03.
* **Model selection:** every story turns on instrument-narrowing judgment — default to Opus throughout.
* **Story 03 final verification runs the full suite without** `--testmon` — the record-shape change is collection-affecting.
* **Surfacing conditions:** if a story finds a `PositionRecord` / `PositionView` direction read the drafting audit missed (the audit covered the pre-[ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) tree), migrate it through `position_direction()` in the same story and note it. If story 03's `mypy` run reddens a consumer site a wave-2 story should have migrated, that is an incomplete wave-2 story — fix inline.

## Acceptance criteria

* A strategy `PositionRecord` carries `direction = None`; equity and single-leg options positions retain a non-`None` `Direction` with no behavior change.
* No consumer reads a position-level direction for a strategy position; `position_direction()` is the one accessor and returns `None` for strategies.
* `positions.direction` is a nullable column; existing strategy rows are backfilled to `NULL`; the codec round-trips `None` ⇄ `NULL`.
* `position-model.md` § Strategy position / § Base position reflect the resolved optional-direction shape.
* Full test suite green under `uv run pytest -n auto` (run without `--testmon` — collection-affecting change).

## Related

[ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) — works around this constraint; see its "Resolved design decisions § Position-level direction". [ALP-592](https://linear.app/alphamind-jatassi/issue/ALP-592/01a-document-strategy-direction-and-credit-risk-semantics-in-position) — the [ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) doc story whose placeholder note story 03 supersedes. [ALP-603](https://linear.app/alphamind-jatassi/issue/ALP-603/strategy-direction-category-error-on-guardrail-evaluation-proposal) — the agreed proposal-side follow-on.
