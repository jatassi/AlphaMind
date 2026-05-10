# 05c — `ThesisComponent.supporting_signals` lifecycle refactor

## Goal

Move `supporting_signals: tuple[SupportingSignal, ...]` off the immutable `ThesisComponent` record and into a per-invocation `ThesisHealthSnapshot` that the strategist re-emits each invocation. Today the strategist re-assesses each component's supporting-signal status (PRESENT / STRENGTHENED / WEAKENED / REVERSED — per `docs/design/01-data-layer/internal/portfolio-state.md` § 3b) every invocation, but the data lives on the immutable `ThesisComponent` — meaning either (a) the strategist re-emits the whole thesis each invocation (heavy), or (b) the supporting-signals data goes stale on the persisted record. The clean fix is to separate entry-time component data (immutable) from per-invocation health assessment (mutable, snapshotted). Adds `ThesisHealthSnapshot` to capture per-invocation supporting-signal status, leaves `ThesisComponent` describing only entry-time data.

## Reading

* `src/alphamind/portfolio_state/records/theses.py:47-51` — `SupportingSignalStatus` enum.
* `src/alphamind/portfolio_state/records/theses.py:63-69` — `SupportingSignal` model.
* `src/alphamind/portfolio_state/records/theses.py:72-87` — `ThesisComponent` with `supporting_signals: tuple[SupportingSignal, ...]` field.
* `docs/design/01-data-layer/internal/portfolio-state.md` § 3b — defines the strategist's re-assessment contract: "Supporting signal status: for each cited signal, whether it's still present, strengthened, weakened, or reversed — assessed by the strategist during re-evaluation."
* `docs/design/04-decision-layer/strategist.md` — the consumer producing the re-assessment per invocation.
* `docs/design/05-execution-layer/thesis-model.md` § Thesis status classifications — describes the strategist's five-state classification, separate from per-signal-status.
* All current consumers reading `component.supporting_signals` — they migrate to read from `health_snapshot.supporting_signals[component.component_id]`.
* <issue id="31a1a496-91d6-48c5-811f-4fa19ea1f787">ALP-122</issue> parent body § Architectural invariants — affirms separation of entry-time vs per-invocation state.

## Depends on

None. This story has no in-tree predecessors and no cross-feature gates. Recommended dispatch order: after wave 1 to minimize concurrent edits to [theses.py](http://theses.py).

## Scope

Source under `src/alphamind/portfolio_state/records/theses.py` (remove field) and `src/alphamind/portfolio_state/views/thesis_health.py` (new file: `ThesisHealthSnapshot` and `ComponentHealthEntry`). Tests at `tests/portfolio_state/records/test_theses.py` (drop supporting_signals tests) and `tests/portfolio_state/views/test_thesis_health.py` (new).

### 1\. Remove `supporting_signals` from `ThesisComponent`

In `src/alphamind/portfolio_state/records/theses.py` `ThesisComponent`, drop:

```python
supporting_signals: tuple[SupportingSignal, ...]
```

Keep `SupportingSignal` and `SupportingSignalStatus` types — they're now used by the new health-snapshot type.

### 2\. Define `ThesisHealthSnapshot`

New file `src/alphamind/portfolio_state/views/thesis_health.py`:

```python
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field
from typing import Annotated

from alphamind.portfolio_state.records.theses import (
    SupportingSignal,
    ThesisStatus,
)


class ComponentHealthEntry(BaseModel):
    """Per-component re-assessment from one strategist invocation.

    The strategist produces one entry per active thesis component at each
    invocation, capturing the current state of cited signals.
    """

    model_config = ConfigDict(frozen=True)

    component_id: str = Field(min_length=1)
    supporting_signals: tuple[SupportingSignal, ...]


class ThesisHealthSnapshot(BaseModel):
    """Per-invocation thesis-health re-assessment from the strategist.

    Produced by the strategist at each invocation; consumed by the PM and
    other downstream agents. Separates entry-time component state (immutable
    on ThesisComponent) from per-invocation re-assessment (mutable across
    invocations, snapshotted here).
    """

    model_config = ConfigDict(frozen=True)

    thesis_id: str = Field(min_length=1)
    invocation_id: str = Field(min_length=1)
    snapshot_timestamp: datetime
    health_status: ThesisStatus
    prior_health_status: ThesisStatus | None
    component_health: tuple[ComponentHealthEntry, ...]

    def health_for_component(self, component_id: str) -> ComponentHealthEntry | None:
        """Return the per-component health entry for component_id, or None."""
        for entry in self.component_health:
            if entry.component_id == component_id:
                return entry
        return None
```

### 3\. Migrate `health_status` and `prior_health_status` off `ThesisRecord`

These two fields on `ThesisRecord` currently capture per-invocation state. Move them to `ThesisHealthSnapshot` (per §2). `ThesisRecord` no longer carries them.

### 4\. Update consumers

Every consumer reading `component.supporting_signals` migrates to read from a `ThesisHealthSnapshot`:

```python
# Before
for signal in component.supporting_signals:
    ...

# After
health_entry = health_snapshot.health_for_component(component.component_id)
if health_entry is not None:
    for signal in health_entry.supporting_signals:
        ...
```

The strategist's input bundle now consumes `ThesisHealthSnapshot` (passed in from prior invocation or initialized empty for first-invocation theses); the strategist's output now produces a fresh `ThesisHealthSnapshot` for next invocation.

### Out of scope

* Persisting `ThesisHealthSnapshot` to DB — that's State persistence (<issue id="beaf98a0-a9fc44a8-ac46-32e50f604345">ALP-119</issue>) Phase 1 work; this story ships the typed shape.
* Changing the strategist's prompt — the prompt already produces health re-assessments; the typed boundary is what changes.

## Acceptance criteria

- [ ] `ThesisComponent.supporting_signals` field is removed.
- [ ] `ThesisRecord.health_status` and `prior_health_status` fields are removed.
- [ ] `ThesisHealthSnapshot` and `ComponentHealthEntry` types exist at `src/alphamind/portfolio_state/views/thesis_health.py`.
- [ ] `health_for_component(component_id)` helper returns the matching entry or None.
- [ ] All consumer sites previously reading `component.supporting_signals` are migrated to `health_snapshot.health_for_component(...)`.
- [ ] Strategist input bundle accepts `ThesisHealthSnapshot` from prior invocation.
- [ ] Strategist output schema includes the new health snapshot.
- [ ] All existing `uv run pytest -n auto` tests pass after consumer migration.
- [ ] Tests at `tests/portfolio_state/views/test_thesis_health.py` cover: (a) ThesisHealthSnapshot construction, (b) frozen, (c) `health_for_component` lookup hit and miss, (d) empty `component_health` tuple acceptable (initial-invocation case).
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` passes clean.

## Verification

* Run `uv run pytest -n auto` (full suite) — every existing test passes plus all new tests.
* Spot-check the migration: `python -c "from alphamind.portfolio_state.records.theses import ThesisComponent; print('supporting_signals' not in ThesisComponent.model_fields)"` should print `True`.
* Spot-check the new view: `python -c "from alphamind.portfolio_state.views.thesis_health import ThesisHealthSnapshot; print(ThesisHealthSnapshot.model_fields.keys())"`.
* Lint clean per CLAUDE.md.