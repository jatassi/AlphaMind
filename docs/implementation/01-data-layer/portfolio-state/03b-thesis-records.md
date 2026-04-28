---
status: not_started
completed_date:
commit_id:
---

# 03b — Thesis records

## Goal

Define the consumer-facing typed records for the thesis registry (raw state categories 3a–3c — active thesis details, thesis status and health, recent thesis resolutions) at `src/alphamind/portfolio_state/records/theses.py`, with field constraints validated via Pydantic v2 and a corresponding test suite at `tests/portfolio_state/records/test_theses.py`.

## Reading

- `../../../design/01-data-layer/internal/portfolio-state.md` — full feature spec; sections 3a, 3b, and 3c define the delivery contract (active thesis details, status/health per strategist invocation, recent resolution rolling window)
- `../../../design/05-execution-layer/thesis-model.md` — authoritative thesis structure: summary, components, mandatory coverage rules, status classifications, resolution categories, component outcomes, three consumption modes
- `../../../design/05-execution-layer/state-persistence.md` § Theses — persistence-layer fields: `thesis_id`, `position_id`, `status` (active/resolved/cancelled), `resolution_timestamp`, `resolution_category` (four thesis-model categories plus cancelled-never-entered)
- `../../../design/05-execution-layer/orders-and-brackets.md` — bracket leg types (take-profit, price-stop, time-expiration, event-invalidation) that invalidation-rationale components reference
- `02-package-skeleton-and-config.md` — the package layout; `src/alphamind/portfolio_state/records/theses.py` is listed as story 03b's target
- `03c-order-and-bracket-records.md` — declares `BracketLegType` canonically; this story imports it
- `../../../implementation/03-analysis-layer/synthesizer/05b-portfolio-state-read-protocol.md` — sibling pattern: frozen Pydantic v2 records, StrEnum discriminators, in-scope/out-of-scope split, acceptance-criteria style

## Depends on

- 02
- 03c (imports `BracketLegType` from `alphamind.portfolio_state.records.orders`)

## Scope

In scope:

1. **Enums** — all `StrEnum`, members in ALL CAPS:

   - `ThesisStatus`: `ON_TRACK`, `PARTIALLY_REALIZED`, `AT_RISK`, `STALE`, `INVALIDATED` — the five health classifications updated each invocation by the strategist per `thesis-model.md` § Thesis status classifications.
   - `ThesisRecordStatus`: `ACTIVE`, `RESOLVED`, `CANCELLED` — the lifecycle status stored in the persistence layer per `state-persistence.md` § Theses; distinct from health classification above.
   - `ThesisComponentType`: `ENTRY_RATIONALE`, `TARGET_RATIONALE`, `INVALIDATION_RATIONALE` — per `thesis-model.md` § Thesis structure.
   - `ThesisResolutionCategory`: `VALIDATED`, `PROFITABLE_BUT_WRONG`, `INVALIDATED_STOPPED_CORRECTLY`, `INVALIDATED_WRONG_ON_EXIT`, `CANCELLED_NEVER_ENTERED` — the four thesis-level categories from `thesis-model.md` § Resolution plus the persistence-only cancelled case from `state-persistence.md` § Theses.
   - `ThesisComponentOutcome`: `VALIDATED`, `WRONG`, `INCONCLUSIVE` — per `thesis-model.md` § Resolution: component-level then thesis-level.
   - `SupportingSignalStatus`: `PRESENT`, `STRENGTHENED`, `WEAKENED`, `REVERSED` — the four states assessed by the strategist during re-evaluation per `portfolio-state.md` § 3b.

   `BracketLegType` is imported from `alphamind.portfolio_state.records.orders` (story 03c) — it is the structural property of the bracket, not the thesis, so the canonical declaration site is `orders.py`. This story uses it for `ThesisComponent.linked_bracket_leg_type` to reference which leg an invalidation-rationale component addresses.

2. **Value objects** — Pydantic v2 `BaseModel`, `model_config = {"frozen": True}`:

   - `KeyAssumption`: a short structured falsifiable claim.
     - `text: str` — the assumption expressed as a testable statement.
     - `outcome: ThesisComponentOutcome | None` — `None` while parent component is unresolved; populated when the parent `ThesisComponent` receives a `resolution_outcome`.

   - `SupportingSignal`: a named signal and its current strategist-assessed status.
     - `name: str` — signal name (e.g., "unusual options activity," "earnings revision upward").
     - `status: SupportingSignalStatus` — current assessment per `portfolio-state.md` § 3b.

   - `ThesisComponent`: a typed, individually-addressable component linked to a specific order or bracket leg.
     - `component_id: str` — unique, immutable per `state-persistence.md` § Theses.
     - `thesis_id: str` — foreign key to the parent thesis.
     - `component_type: ThesisComponentType`.
     - `linked_bracket_leg_type: BracketLegType | None` — `None` for entry-rationale components that reference the entry order rather than a bracket leg; populated for invalidation-rationale components citing a specific bracket leg.
     - `instrument_reference: str` — ticker for equity positions; `leg_id` from `StrategyLeg` (see `03a-position-records.md`) for strategy positions, identifying which leg this component addresses.
     - `narrative: str` — the specific reasoning: which signals support it, the expected causal chain, why this threshold was chosen.
     - `key_assumptions: tuple[KeyAssumption, ...]` — discrete falsifiable claims this component depends on.
     - `supporting_signals: tuple[SupportingSignal, ...]` — signals cited in the component narrative.
     - `generation_timestamp: datetime` — tz-aware UTC; when this component was written.
     - `resolution_outcome: ThesisComponentOutcome | None` — `None` while the parent thesis is active; populated at position close.
     - `resolution_notes: str | None` — explanation of the component outcome; `None` while active.

   - `ThesisRecord`: the full consumer-facing thesis record, one-to-one with a position.
     - `thesis_id: str` — unique, immutable.
     - `position_id: str` — foreign key; one-to-one with the position.
     - `summary: str` — the one-or-two-sentence overall trade rationale; self-contained for at-a-glance review per `thesis-model.md` § Summary.
     - `components: tuple[ThesisComponent, ...]` — non-empty for any non-CANCELLED thesis; the individually-addressable components enabling the three consumption modes.
     - `status: ThesisRecordStatus` — lifecycle status from the persistence layer.
     - `health_status: ThesisStatus | None` — `None` until the first strategist evaluation; updated each invocation per `portfolio-state.md` § 3b.
     - `prior_health_status: ThesisStatus | None` — the health status the strategist saw in the previous invocation; supports the `prior_status` field referenced by the strategist context package.
     - `generation_timestamp: datetime` — tz-aware UTC; when the thesis was written.
     - `time_expectation_hours: str` — the prose form preserved exactly as generated (e.g., `"4-24h"`, `"24-72h"`); not normalized to a number, per `portfolio-state.md` § 3a and the synthesizer's `ThesisSummary`.
     - `age_hours: float` — hours elapsed since `generation_timestamp`; computed at delivery; non-negative.
     - `expected_resolution_at: datetime` — tz-aware UTC; computed from `generation_timestamp` plus the parsed upper-bound hours from `time_expectation_hours`.
     - `resolution_timestamp: datetime | None` — tz-aware UTC; `None` while active.
     - `resolution_category: ThesisResolutionCategory | None` — `None` while active.
     - `resolution_pnl_usd: float | None` — `None` while active.
     - `entry_fill_gap_usd: float | None` — gap between intended entry parameters and actual fill price per `portfolio-state.md` § 3a; `None` when not yet computed or not applicable.
     - `key_catalyst: str` — concise catalyst description; matches the `key_catalyst` field in the synthesizer's `ThesisSummary`.

   - `RecentThesisResolution`: a compact projection for the rolling-window delivery in category 3c; delivered at summary level by default per the design.
     - `thesis_id: str`.
     - `position_id: str`.
     - `resolution_category: ThesisResolutionCategory`.
     - `component_outcomes: tuple[tuple[str, ThesisComponentOutcome], ...]` — pairs of `(component_id, outcome)` for each resolved component.
     - `resolution_pnl_usd: float`.
     - `active_duration_hours: float` — how long the thesis was active.
     - `expected_duration_hours: float` — the upper-bound hours parsed from `time_expectation_hours` at resolution time.
     - `signal_post_mortem: str | None` — for invalidated theses, which signal or assumption was wrong; `None` for validated or profitable-but-wrong resolutions.

3. **Validation rules** (enforced via Pydantic `model_validator`):

   - `components` non-empty for `ThesisRecordStatus.ACTIVE` and `ThesisRecordStatus.RESOLVED` theses. `CANCELLED` theses may have an empty `components` tuple (position never opened, so no components were required).
   - Mandatory coverage for non-CANCELLED theses: `components` must contain at least one `ThesisComponentType.ENTRY_RATIONALE`, at least one `ThesisComponentType.TARGET_RATIONALE`, and at least one `ThesisComponentType.INVALIDATION_RATIONALE`, per `thesis-model.md` § Mandatory coverage.
   - `status == ACTIVE` ⇒ `resolution_timestamp`, `resolution_category`, and `resolution_pnl_usd` are all `None`.
   - `status == RESOLVED` ⇒ `resolution_timestamp`, `resolution_category`, and `resolution_pnl_usd` are all non-`None`; every component in `components` has `resolution_outcome` non-`None`.
   - `status == CANCELLED` ⇒ `resolution_category` may be `CANCELLED_NEVER_ENTERED` or `None`; no constraint on component resolution outcomes (no components required).
   - `age_hours >= 0`.
   - `summary` non-empty string.
   - `time_expectation_hours` non-empty string (free-form preserved; format is not validated here).

4. **Tests** at `tests/portfolio_state/records/test_theses.py`:
   - Each enum: correct member set, correct string values.
   - Each value object: valid construction; required fields enforced.
   - Each validation rule: at least one passing fixture and one failing fixture that produces `ValidationError` with a meaningful error path:
     - Components non-empty for ACTIVE and RESOLVED; empty tuple accepted for CANCELLED.
     - Mandatory coverage (entry + target + invalidation rationale present) passes for non-CANCELLED with correct components; fails with any type missing.
     - ACTIVE status ⇒ resolution fields `None`; any populated resolution field raises.
     - RESOLVED status ⇒ all resolution fields populated; any `None` raises; unresolved component raises.
     - `age_hours < 0` rejected.
     - Empty `summary` rejected.
     - Empty `time_expectation_hours` rejected.

Out of scope:

- Persistence schema for theses (state-persistence story set).
- Strategist's per-component status classification logic (decision-layer / strategist story set).
- Component-level retrieval tool for the PM (decision-layer / portfolio-manager story set).
- Thesis registry repository reads (story 04b).
- Cross-thesis dependency mapping (distillation-layer derived metrics 9a/9b — separate work tree).
- "Active theses summary" projection convergence: the synthesizer's `ThesisSummary` at `src/alphamind/analysis/synthesizer/portfolio_state.py` is consumed as-is; story 07 lands the canonical projection types and the synthesizer-side type can converge later.
- Running thesis accuracy aggregate (category 3c trailing validation rate) — a rolling computation, not a record type; belongs in `thesis_quality.py` (story 03f).

## Notes

Per `feedback_per_producer_schema.md`, components are individually addressable as separate entities within a thesis — this is the structural enabler for the three consumption modes (programmatic, targeted LLM, full thesis) described in `thesis-model.md` § Three consumption modes. A flat-narrative thesis evaluated holistically every time does not scale; individual component addressability is the design's token-efficiency lever.

The thesis model is authoritative in `docs/design/05-execution-layer/thesis-model.md`; this story's record types align with it. Cross-tree convergence with future state-persistence DB schema (when the thesis component child-entity table lands) is a future concern.

The `health_status` and `prior_health_status` distinction supports the strategist's per-invocation classification loop per `portfolio-state.md` § 3b. The strategist reads the current `health_status`, assesses new market context, produces an updated classification, and that updated classification becomes the next invocation's `health_status` (and the outgoing `health_status` becomes `prior_health_status`). This story declares the read-side fields only; the strategist's write path is a decision-layer concern.

Per `feedback_no_inventing_component_names.md`, all type names, enum members, and field names are sourced directly from `thesis-model.md`, `state-persistence.md`, `portfolio-state.md`, and `orders-and-brackets.md`. No novel names are introduced.

`RecentThesisResolution` is a compact projection for the rolling-window delivery in category 3c. Full `ThesisRecord` records for resolved theses remain available via the repository (story 04b) for deeper queries; this projection is what the snapshot delivers by default, per the design's "delivered at summary level" for category 3c.

`BracketLegType` is canonically declared in `alphamind.portfolio_state.records.orders` (story 03c). The thesis model imports it to identify which bracket leg an invalidation-rationale component addresses — creating the bidirectional traceability described in `state-persistence.md` § Brackets — without duplicating the enum.

## Acceptance criteria

- [ ] `ThesisStatus` has exactly `ON_TRACK`, `PARTIALLY_REALIZED`, `AT_RISK`, `STALE`, `INVALIDATED`.
- [ ] `ThesisRecordStatus` has exactly `ACTIVE`, `RESOLVED`, `CANCELLED`.
- [ ] `ThesisComponentType` has exactly `ENTRY_RATIONALE`, `TARGET_RATIONALE`, `INVALIDATION_RATIONALE`.
- [ ] `ThesisResolutionCategory` has exactly `VALIDATED`, `PROFITABLE_BUT_WRONG`, `INVALIDATED_STOPPED_CORRECTLY`, `INVALIDATED_WRONG_ON_EXIT`, `CANCELLED_NEVER_ENTERED`.
- [ ] `ThesisComponentOutcome` has exactly `VALIDATED`, `WRONG`, `INCONCLUSIVE`.
- [ ] `SupportingSignalStatus` has exactly `PRESENT`, `STRENGTHENED`, `WEAKENED`, `REVERSED`.
- [ ] `BracketLegType` is imported from `alphamind.portfolio_state.records.orders`; not redeclared.
- [ ] `KeyAssumption`, `SupportingSignal`, `ThesisComponent`, `ThesisRecord`, and `RecentThesisResolution` are frozen Pydantic v2 models with the documented field sets.
- [ ] Components non-empty rule enforced for `ACTIVE` and `RESOLVED` theses; empty tuple accepted for `CANCELLED` — each branch has a passing and failing test.
- [ ] Mandatory coverage rule (entry + target + invalidation rationale present) enforced for non-CANCELLED theses — passing test with all three present; failing tests for each missing type.
- [ ] `ACTIVE` status ⇒ `resolution_timestamp`, `resolution_category`, `resolution_pnl_usd` all `None` — passing and failing tests.
- [ ] `RESOLVED` status ⇒ all three resolution fields non-`None` and every component `resolution_outcome` non-`None` — passing and failing tests for each sub-rule.
- [ ] `age_hours < 0` rejected — has a passing and failing test.
- [ ] Empty `summary` rejected — has a passing and failing test.
- [ ] Empty `time_expectation_hours` rejected — has a passing and failing test.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
