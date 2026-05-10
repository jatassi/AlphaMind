# 04a — Restructure `records/` into `records/`, `events/`, `aggregates/` + declare public API

## Goal

Reorganize the `portfolio_state/records/` directory to mirror the design's three-tier classification from `state-persistence.md` (Tier 1 — core entities, Tier 2 — lifecycle/event records, Tier 3 — derived aggregates). Today the directory mixes all three tiers under one `records/` namespace, so the file layout doesn't reflect the design's mental model and consumers can't tell from the import path whether a type is a primary entity, an event payload, or a derived aggregate. Also declare a curated public API surface in each subpackage's `__init__.py` (currently 0 bytes) so consumers import from a stable surface rather than reaching into specific modules. Update all 279+ import sites to the new paths via re-exports — no behavior changes, pure code-organization fix.

Target layout:

```
src/alphamind/portfolio_state/
  records/        # Tier 1 — core mutable entities
    positions.py
    theses.py
    orders.py
    cash.py       (formerly the CashLedger / UnsettledProceedsEntry portion of capital.py)
    __init__.py   (curated public API)
  events/         # Tier 2 — append-only lifecycle records
    activity_log.py  (relocated from records/)
    __init__.py
  aggregates/     # Tier 3 — pre-computed derived aggregates
    risk_budget.py    (RiskBudgetEntry / RiskBudgetConsumption from capital.py)
    risk_parameters.py (ActiveRiskParameterEntry / ActiveRiskParameterSet from capital.py)
    drawdown.py        (DrawdownState from capital.py)
    thesis_quality.py  (relocated from records/)
    __init__.py
```

## Reading

* `src/alphamind/portfolio_state/records/` — current flat directory mixing all three tiers.
* `docs/design/05-execution-layer/state-persistence.md` § Logical entities — defines the three tiers explicitly.
* `docs/design/05-execution-layer/state-persistence.md` § Tier 1 / Tier 2 / Tier 3 — assigns each entity to a tier; the new file layout mirrors this assignment exactly.
* All importers found by `grep -rln "from alphamind.portfolio_state.records" src/ tests/` — ~50 files import from records; all need updates if the import path changes (mitigated by re-exports — see §3).
* `tests/portfolio_state/records/` — current test files; they remain at `tests/portfolio_state/records/` to mirror existing layout, but a follow-up could relocate them to `tests/portfolio_state/events/` and `tests/portfolio_state/aggregates/`.
* <issue id="2d1cd9b2-12f0-49fc-ae3d-6c64a6733638">ALP-344</issue> (story 02) — moves the four risk-guardrail enums out of [capital.py](http://capital.py) first; this story (04a) handles the structural-tier reorganization of what remains.
* <issue id="31a1a496-91d6-48c5-811f-4fa19ea1f787">ALP-122</issue> parent body § Pre-resolved decision (A) — `portfolio_state.records.*` is the canonical home for typed records; this reorg refines that to three subpackages without moving the records to a different feature.

## Depends on

* <issue id="2d1cd9b2-12f0-49fc-ae3d-6c64a6733638">ALP-344</issue> (this work tree) — story 02 (move risk-guardrail enums) lands first; otherwise the new `records/cash.py` would have to import the four enums from [capital.py](http://capital.py) and create a transitive dependency on a file we're trying to retire.

The other wave-1 / wave-3 stories (additive fields, typed payloads) don't strictly block 04a, but dispatching 04a *after* they land minimizes merge-conflict surface.

## Scope

Source under `src/alphamind/portfolio_state/{records,events,aggregates}/` (file moves + re-exports + new `__init__.py` files). Tests at `tests/portfolio_state/records/` (no test moves; tests import via the new public API surface).

### 1\. Create new subpackages

`src/alphamind/portfolio_state/events/__init__.py` — new file. Initial contents: re-export `ActivityLogEntry`, `EventGroup`, `EventType`, `EventSource`, all detail classes from `events/activity_log.py`.

`src/alphamind/portfolio_state/aggregates/__init__.py` — new file. Re-exports `RiskBudgetEntry`, `RiskBudgetConsumption`, `ActiveRiskParameterEntry`, `ActiveRiskParameterSet`, `DrawdownState`, `ThesisQualityAggregate`, etc.

### 2\. Move files

* `records/activity_log.py` → `events/activity_log.py` (verbatim move).
* `records/thesis_quality.py` → `aggregates/thesis_quality.py` (verbatim move).
* `records/capital.py` — split into:
  * `records/cash.py` — `UnsettledProceedsEntry`, `CashLedger`.
  * `aggregates/drawdown.py` — `DrawdownState`.
  * `aggregates/risk_budget.py` — `RiskBudgetEntry`, `RiskBudgetConsumption`, `_HasRuleId`, `_assert_unique_rule_ids`.
  * `aggregates/risk_parameters.py` — `ActiveRiskParameterEntry`, `ActiveRiskParameterSet`.

### 3\. Backward-compat re-exports in old paths

To avoid breaking 279+ import sites, the old paths re-export from new locations:

`src/alphamind/portfolio_state/records/activity_log.py` (now a thin shim):

```python
"""Backward-compat re-export. Activity log records moved to events/ in story 04a.

Prefer `from alphamind.portfolio_state.events import ...` for new code.
"""

from alphamind.portfolio_state.events.activity_log import *  # noqa: F401, F403

# Explicit re-exports for star-import compat
from alphamind.portfolio_state.events.activity_log import (  # noqa: F401
    ActivityLogEntry,
    EventGroup,
    EventType,
    EventSource,
    # ... all symbols
)
```

Same shim pattern for `records/thesis_quality.py`, `records/capital.py`. The shims emit a one-line module-level `# noqa: F403` and explicit re-exports so that mypy and ruff stay clean.

After the shims land, the operator can opt to migrate importers to the new paths over time; this story does NOT update the 279 importers (that's a separate cleanup task — see project-tracker quick-wins follow-up).

### 4\. Curated `__init__.py` public APIs

`src/alphamind/portfolio_state/records/__init__.py` (currently 0 bytes) — declare:

```python
"""Tier 1 — core mutable entities per state-persistence.md."""

from alphamind.portfolio_state.records.cash import (
    CashLedger,
    UnsettledProceedsEntry,
)
from alphamind.portfolio_state.records.orders import (
    BracketLeg,
    BracketLegEnforcement,
    BracketLegStatus,
    BracketLegType,
    BracketRecord,
    BracketStatus,
    InstrumentSpec,
    OrderClass,         # from story 01i
    OrderDirection,
    OrderDuration,
    OrderRecord,
    OrderRole,
    OrderStatus,
    OrderType,
    PriceParameters,
    PriceTrigger,       # from story 03a
    TimeTrigger,
    EventTrigger,
    PLAnchorSpec,       # from story 03b
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    InstrumentType,
    LiveExecutionEstimate,  # from story 01g
    LocateStatus,
    OptionContractType,
    OptionGreeks,
    OptionsPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
    StrategyLeg,
    StrategyPositionDetails,
)
from alphamind.portfolio_state.records.theses import (
    KeyAssumption,
    RecentThesisResolution,
    SupportingSignal,
    SupportingSignalStatus,
    ThesisComponent,
    ThesisComponentOutcome,
    ThesisComponentType,
    ThesisRecord,
    ThesisRecordStatus,
    ThesisResolutionCategory,
    ThesisStatus,
)

__all__ = [...]  # all the above names sorted alphabetically
```

Same pattern for `events/__init__.py` and `aggregates/__init__.py`.

### Out of scope

* Migrating any of the 279 importers to use the new `events.*` / `aggregates.*` paths — the backward-compat shims handle this; importer migration is a follow-up cleanup task tracked in the project tracker.
* Moving test files to mirror the new layout — keep tests at `tests/portfolio_state/records/` and `tests/portfolio_state/aggregates/` follow-up.
* Renaming any types — only file moves and re-exports. Types keep their existing names.

## Acceptance criteria

- [ ] New subpackages exist: `src/alphamind/portfolio_state/events/__init__.py`, `src/alphamind/portfolio_state/aggregates/__init__.py`.
- [ ] `events/activity_log.py` exists with content verbatim from former `records/activity_log.py`.
- [ ] `aggregates/thesis_quality.py` exists with content verbatim from former `records/thesis_quality.py`.
- [ ] `records/cash.py` exists with `UnsettledProceedsEntry` and `CashLedger`.
- [ ] `aggregates/drawdown.py` exists with `DrawdownState`.
- [ ] `aggregates/risk_budget.py` exists with `RiskBudgetEntry`, `RiskBudgetConsumption`, `_HasRuleId`, `_assert_unique_rule_ids`.
- [ ] `aggregates/risk_parameters.py` exists with `ActiveRiskParameterEntry`, `ActiveRiskParameterSet`.
- [ ] `records/capital.py` is either deleted or kept as a backward-compat shim re-exporting from the four new locations.
- [ ] `records/activity_log.py` is a backward-compat shim re-exporting from `events/activity_log.py`.
- [ ] `records/thesis_quality.py` is a backward-compat shim re-exporting from `aggregates/thesis_quality.py`.
- [ ] `records/__init__.py`, `events/__init__.py`, `aggregates/__init__.py` declare curated `__all__` public APIs covering every type the subpackage exposes.
- [ ] Identity checks pass: `python -c "from alphamind.portfolio_state.records.activity_log import ActivityLogEntry as A; from alphamind.portfolio_state.events.activity_log import ActivityLogEntry as B; print(A is B)"` returns `True`.
- [ ] All existing `uv run pytest -n auto` tests pass — no test breaks because of the moves (the shims preserve all old import paths).
- [ ] No new circular imports introduced.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` passes clean across the whole repo.

## Verification

* Run `uv run pytest -n auto` (full suite) — every existing test passes.
* Spot-check identity for each moved type via the `python -c "..."` pattern above.
* Spot-check the new `__init__.py` files: `python -c "from alphamind.portfolio_state.records import PositionRecord, OrderRecord, ThesisRecord, BracketRecord, CashLedger; print('OK')"`.
* Spot-check old import paths still work: `python -c "from alphamind.portfolio_state.records.capital import RegimeLabel, CashLedger, RiskBudgetConsumption; print('OK')"`.
* Lint clean per CLAUDE.md.