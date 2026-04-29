---
status: in_progress
completed_date:
commit_id:
---

# 04a — Regime adaptation state persistence

## Goal

Land the SQLAlchemy table, Alembic migration, and read/write repository functions for the per-invocation `RegimeAdaptationState` records the orchestrator (story 09) writes and reads back. Forward-only append semantics matching `DistillationRegimeState`'s pattern: one row per invocation, primary-keyed on `as_of`, latest-wins read accessor for the orchestrator's "what was the prior state?" question.

## Reading

- `docs/design/06-risk-guardrails/regime-adaptation.md` § Transition mechanics — the loosening interpolation needs the prior persisted state to compute "invocations since transition"; the persistence layer is what carries that across invocations
- `docs/design/06-risk-guardrails/regime-adaptation.md` § Transition logging — every transition is logged as a risk/guardrail activity log event with previous regime, new regime, direction, and parameter snapshots; this story's persistence is one half of that record (the per-invocation state); the audit log emission (story 09) is the other half
- `docs/design/01-data-layer/internal/portfolio-state.md` § 4d — the `ActiveRiskParameterSet` typed record this feature populates carries `transition_state` and `transition_invocations_remaining`; both must persist across invocations to keep their values stable in the snapshot
- `src/alphamind/persistence/models.py` § `DistillationRegimeState` — the sibling pattern this table mirrors: forward-only, primary-key `as_of`, indexed for "latest" lookups
- `src/alphamind/persistence/migrations/versions/71d9125161ee_add_distillation_state_tables.py` — the sibling Alembic migration pattern; same skeleton applies here
- `src/alphamind/distillation/regime.py` § `_select_most_recent_regime_row`, `current_regime_label`, `refresh_regime_state` — the canonical read/write pattern this story mirrors
- `02-package-skeleton-and-types.md` — `RegimeAdaptationState` typed record the repository persists; `persistence.py` is the target module
- `tests/distillation/test_regime.py` — sibling test conventions for SQLite session fixtures

## Depends on

- 02 (package skeleton + types)

## Scope

In scope: schema additions to `src/alphamind/persistence/models.py`, an Alembic migration under `src/alphamind/persistence/migrations/versions/`, and the repository module at `src/alphamind/risk_guardrails/regime_adaptation/persistence.py`. Tests at `tests/risk_guardrails/regime_adaptation/test_persistence.py` and (for the migration smoke test) `tests/persistence/test_migrations.py` if a sibling migration test exists; otherwise extend the existing migration test surface in place.

### 1. SQLAlchemy table

In `src/alphamind/persistence/models.py`, add `RegimeAdaptationStateRow` near the existing `DistillationRegimeState`:

```python
class RegimeAdaptationStateRow(Base):
    """Forward-only per-invocation regime adaptation state."""

    __tablename__ = "regime_adaptation_state"

    as_of: Mapped[str] = mapped_column(Text, primary_key=True)
    invocation_id: Mapped[str] = mapped_column(Text)
    active_regime: Mapped[str] = mapped_column(Text)
    prior_regime: Mapped[str | None] = mapped_column(Text, nullable=True)
    transition_state: Mapped[str] = mapped_column(Text)
    transition_invocations_remaining: Mapped[int] = mapped_column(Integer)
    transition_started_invocation_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    transition_origin_regime: Mapped[str | None] = mapped_column(Text, nullable=True)
    active_overlays_csv: Mapped[str] = mapped_column(Text)  # comma-separated overlay names; "" when empty
    distillation_regime_label: Mapped[str] = mapped_column(Text)
    distillation_vix_level: Mapped[float] = mapped_column(Float)
    regime_skip_emergency: Mapped[int] = mapped_column(Integer)  # 0 or 1; SQLite-compatible bool
    ingested_at: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        _check_in(
            "active_regime",
            _GUARDRAIL_REGIMES,
            "ck_regime_adaptation_state_active_regime",
        ),
        _check_in(
            "transition_state",
            _GUARDRAIL_TRANSITION_STATES,
            "ck_regime_adaptation_state_transition_state",
        ),
        # prior_regime / transition_origin_regime use the same vocabulary;
        # CHECK constraints are conditional (apply when not NULL) per the
        # SQLite CASE/WHEN idiom — see Notes for rationale.
        CheckConstraint(
            "prior_regime IS NULL OR prior_regime IN "
            "('low_vol', 'normal', 'elevated', 'crisis')",
            name="ck_regime_adaptation_state_prior_regime",
        ),
        CheckConstraint(
            "transition_origin_regime IS NULL OR transition_origin_regime IN "
            "('low_vol', 'normal', 'elevated', 'crisis')",
            name="ck_regime_adaptation_state_origin_regime",
        ),
        CheckConstraint(
            "regime_skip_emergency IN (0, 1)",
            name="ck_regime_adaptation_state_skip_emergency",
        ),
        # Mirror the typed-record invariant: STABLE -> remaining == 0.
        CheckConstraint(
            "(transition_state = 'STABLE' AND transition_invocations_remaining = 0) "
            "OR transition_state != 'STABLE'",
            name="ck_regime_adaptation_state_stable_zero_remaining",
        ),
        # Mirror the typed-record invariant: LOOSENING -> origin and started populated.
        CheckConstraint(
            "transition_state != 'LOOSENING' OR "
            "(transition_origin_regime IS NOT NULL AND "
            " transition_started_invocation_id IS NOT NULL)",
            name="ck_regime_adaptation_state_loosening_populated",
        ),
    )
```

Module-level enum-vocabulary constants in `models.py` adjacent to the existing `_REGIME_LABELS`:

```python
_GUARDRAIL_REGIMES = ("low_vol", "normal", "elevated", "crisis")
_GUARDRAIL_TRANSITION_STATES = ("STABLE", "TIGHTENING", "LOOSENING")
```

Compile-time guards mirroring the distillation pattern:

```python
assert {member.value for member in Regime} == set(_GUARDRAIL_REGIMES)
assert {member.value for member in RegimeTransitionState} == set(_GUARDRAIL_TRANSITION_STATES)
```

(`Regime` imported from `alphamind.config.models.regimes`; `RegimeTransitionState` from `alphamind.portfolio_state.records.capital`.)

### 2. Alembic migration

A new revision file under `src/alphamind/persistence/migrations/versions/`:

```python
"""add regime adaptation state table

Revision ID: <generated>
Revises: <head at story dispatch time>
Create Date: <generated>
"""

from alembic import op
import sqlalchemy as sa


def upgrade() -> None:
    op.create_table(
        "regime_adaptation_state",
        sa.Column("as_of", sa.Text(), nullable=False, primary_key=True),
        sa.Column("invocation_id", sa.Text(), nullable=False),
        sa.Column("active_regime", sa.Text(), nullable=False),
        sa.Column("prior_regime", sa.Text(), nullable=True),
        sa.Column("transition_state", sa.Text(), nullable=False),
        sa.Column("transition_invocations_remaining", sa.Integer(), nullable=False),
        sa.Column("transition_started_invocation_id", sa.Text(), nullable=True),
        sa.Column("transition_origin_regime", sa.Text(), nullable=True),
        sa.Column("active_overlays_csv", sa.Text(), nullable=False),
        sa.Column("distillation_regime_label", sa.Text(), nullable=False),
        sa.Column("distillation_vix_level", sa.Float(), nullable=False),
        sa.Column("regime_skip_emergency", sa.Integer(), nullable=False),
        sa.Column("ingested_at", sa.Text(), nullable=False),
        sa.CheckConstraint(...),  # one per CHECK above
    )
    op.create_index(
        "ix_regime_adaptation_state_as_of_desc",
        "regime_adaptation_state",
        ["as_of"],
    )


def downgrade() -> None:
    op.drop_index("ix_regime_adaptation_state_as_of_desc", "regime_adaptation_state")
    op.drop_table("regime_adaptation_state")
```

The migration's `revises` field points at the current Alembic head. The subagent runs `alembic heads` (or equivalent) at story start to determine the value.

### 3. Repository module — `persistence.py`

`src/alphamind/risk_guardrails/regime_adaptation/persistence.py` exposes:

```python
def select_most_recent_state(session: Session) -> RegimeAdaptationState | None:
    """Return the most recently persisted state, or None on bootstrap."""

def insert_state(session: Session, state: RegimeAdaptationState) -> None:
    """Append the state row. Wraps in the framework's fail-closed transaction wrapper.

    Raises IntegrityError if a row with the same as_of already exists — the
    orchestrator must not double-write within an invocation.
    """

def state_to_row(state: RegimeAdaptationState, *, ingested_at: str) -> RegimeAdaptationStateRow:
    """Adapter — typed record to ORM row. Pure."""

def row_to_state(row: RegimeAdaptationStateRow) -> RegimeAdaptationState:
    """Adapter — ORM row to typed record. Pure."""
```

Implementation requirements:

- `select_most_recent_state` orders by `as_of` descending and limits to one — the indexed read pattern.
- `insert_state` wraps the `session.add` + commit cycle in `alphamind.distillation.baselines._refresh_transaction` (or the equivalent fail-closed helper) so a partial write rolls back.
- `state_to_row` serializes `active_overlays: tuple[Overlay, ...]` to a comma-separated string in alphabetical order. Empty tuple → empty string. The encoding is bijective with the typed record's tuple via sorted-membership comparison.
- `row_to_state` deserializes by splitting on `,` and filtering empty strings; constructs `Overlay` enum members from the string values; sorts the resulting tuple alphabetically before passing to `RegimeAdaptationState`.
- Both adapters validate that the SQL row's `transition_state` deserializes to a valid `RegimeTransitionState` member and that `active_regime` / `prior_regime` / `transition_origin_regime` deserialize to valid `Regime` members. A vocabulary mismatch raises `ValueError` with the offending field and value.

### 4. Tests — `test_persistence.py`

- **Round-trip:** construct a `RegimeAdaptationState`, `insert_state`, `select_most_recent_state`, assert the round-tripped record equals the original (field-by-field).
- **Round-trip with empty overlays:** `active_overlays=()` round-trips to `()`.
- **Round-trip with multiple overlays:** `active_overlays=(Overlay.pre_event, Overlay.stress)` round-trips to a tuple sorted alphabetically.
- **Round-trip during loosening:** `transition_state=LOOSENING`, `transition_invocations_remaining=2`, `transition_origin_regime=Regime.elevated`, `transition_started_invocation_id="INV-2026-04-28-1100"` round-trips bit-for-bit.
- **Round-trip during tightening:** `transition_state=TIGHTENING`, `transition_invocations_remaining=0`, `transition_origin_regime=None`, `transition_started_invocation_id=None`. Verifies the Optional fields persist as NULL and resurface as None.
- **Bootstrap read:** `select_most_recent_state` on an empty table returns `None` (not an exception).
- **Latest-wins:** insert three states with monotonically increasing `as_of` values; `select_most_recent_state` returns the third.
- **Duplicate-`as_of` insert raises `IntegrityError`:** insert a state, then insert a second state with the same `as_of`; SQLAlchemy raises an `IntegrityError` and the read still returns the first.
- **CHECK constraint on `active_regime`:** insert via raw SQL with `active_regime='invalid'`; the database raises an error (verifies the CHECK landed in the migration).
- **CHECK constraint on `transition_state`:** same shape, `transition_state='INVALID'` raises.
- **CHECK constraint on STABLE-zero-remaining:** insert with `transition_state='STABLE'` and `transition_invocations_remaining=2` raises a CHECK violation.
- **CHECK constraint on LOOSENING-populated:** insert with `transition_state='LOOSENING'` and `transition_origin_regime=NULL` raises a CHECK violation.
- **Overlay vocabulary corruption:** insert via raw SQL with `active_overlays_csv='invalid_overlay'`; `select_most_recent_state` raises `ValueError` naming the offending overlay value (the read-side adapter catches the corruption).
- **Migration upgrades and downgrades cleanly:** upgrade from the prior head creates the table; downgrade drops it; assert via `inspect(engine).get_table_names()`.

Out of scope:

- Writing the *audit log* entries the orchestrator emits — those land alongside the activity-log table when the execution layer ships it. This story persists only the per-invocation state record.
- Backfilling historical regime adaptation state from `DistillationRegimeState` rows — bootstrap is `None` for the regime-adaptation table; no migration of distillation history.
- Modifying `DistillationRegimeState` — the two tables are independent. Distillation owns its label vocabulary; this story uses the guardrail-side vocabulary.

## Notes

The `active_overlays_csv` Text column is the simplest forward-compatible representation in SQLite. The known vocabulary is two members (`pre_event`, `stress`), so a JSON column or junction table would be over-structuring. The adapter sorts alphabetically on write and on read, which makes equality comparisons stable across persistence cycles.

Per `feedback_no_inventing_component_names.md`, the table name `regime_adaptation_state` mirrors `distillation_regime_state`'s convention: `<feature>_state` for forward-only per-invocation snapshots. The repository function names `select_most_recent_state` / `insert_state` mirror `_select_most_recent_regime_row` / `refresh_regime_state` (with `refresh` softened to `insert` because this table doesn't hold rolling baselines — it's an append-only history).

The `regime_skip_emergency` column stores `0`/`1` rather than a boolean — SQLite has no native `BOOLEAN` type and the existing `DistillationCompositeState.alert_active` column uses the same encoding. The CHECK constraint locks the value space to two members.

The Alembic migration's `revises` value depends on the current Alembic head at story dispatch time. Since other work trees may land migrations in parallel (e.g., the rules-and-limits or guardrail-evaluation tracks if those introduce schema), the subagent must run `alembic heads` to find the current value rather than hard-coding it. If two parallel work trees both point at the same head, the merge resolution is to set this story's revision to chain off the *other* story's revision.

The SQLite CHECK constraint syntax for "field is NULL OR field IN (vocabulary)" uses raw SQL because `_check_in` only generates the simple form. The two `CheckConstraint(...)` calls for `prior_regime` and `transition_origin_regime` spell out the SQL string explicitly; this is one of the few places it is appropriate to bypass `_check_in`.

Per `feedback_per_producer_schema.md`, this table is one schema with one writer (the orchestrator). The CHECK constraints encode the same invariants the typed record `__post_init__` enforces; both layers fail closed independently. The defensive duplication is intentional — a future direct-SQL writer (e.g., a backfill script) would still face the constraints.

The repository module does not expose a "list all states" or "states between t1 and t2" function. Those queries are useful for the feedback-loop dashboard but are not yet needed; add when a consumer materializes per `feedback_simplify_before_building.md`.

## Acceptance criteria

- [ ] `src/alphamind/persistence/models.py` defines `RegimeAdaptationStateRow` with the documented columns and CHECK constraints.
- [ ] `src/alphamind/persistence/models.py` defines `_GUARDRAIL_REGIMES` and `_GUARDRAIL_TRANSITION_STATES` module-level constants matching the `Regime` and `RegimeTransitionState` enum vocabularies.
- [ ] Compile-time `assert` statements in `models.py` confirm the enum-vocabulary alignment.
- [ ] An Alembic migration under `src/alphamind/persistence/migrations/versions/` creates the `regime_adaptation_state` table with the documented columns, CHECK constraints, and the `as_of` index.
- [ ] `src/alphamind/risk_guardrails/regime_adaptation/persistence.py` exists and defines `select_most_recent_state`, `insert_state`, `state_to_row`, `row_to_state`.
- [ ] `select_most_recent_state`, `insert_state` are re-exported from `src/alphamind/risk_guardrails/regime_adaptation/__init__.py`.
- [ ] Round-trip tests assert byte-identical `RegimeAdaptationState` recovery for STABLE, TIGHTENING, and LOOSENING states (with all combinations of populated / NULL Optional fields).
- [ ] `select_most_recent_state` returns `None` on an empty table.
- [ ] `select_most_recent_state` returns the latest row when multiple rows are present.
- [ ] Duplicate-`as_of` insert raises `IntegrityError`.
- [ ] CHECK constraints reject `active_regime='invalid'`, `transition_state='INVALID'`, `STABLE` with non-zero remaining, and `LOOSENING` with NULL origin.
- [ ] Overlay vocabulary corruption (raw-SQL inserted invalid overlay name in `active_overlays_csv`) surfaces as `ValueError` from the read-side adapter.
- [ ] The migration upgrades and downgrades cleanly against an empty SQLite database.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
