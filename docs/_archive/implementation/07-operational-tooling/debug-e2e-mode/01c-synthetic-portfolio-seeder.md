# 01c — Synthetic portfolio seeder

## Goal

Implement `scheduler/debug_e2e/seed.py` with `wipe_and_seed(*, session, now, db_path, portfolio)`. Wipes the 9 tables enumerated in design § 6.3, then seeds the synthetic portfolio (positions + theses + 1 cash_ledger row) via the canonical SQLAlchemy ORM models. Guarded by a DB-path suffix check that refuses to run unless the resolved path ends with `-debug-e2e.db`. Independent of stories 01a / 01b — depends only on the existing state-persistence substrate ([ALP-119](<https://linear.app/alphamind-jatassi/issue/ALP-119>), Done) and the synthetic portfolio data (which 02b ships; this story accepts the portfolio as a parameter typed `Any` for now).

## Reading

* `docs/design/debug-e2e-mode.md` § 6.3 (Wipe-everything seed vs. tagged-rows-only seed) — wipe-table list, DB-path guard rationale
* `src/alphamind/state/tables/` — ORM models for `positions`, `position_theses`, `position_fills`, `brackets`, `bracket_legs`, `cash_ledger`, `activity_log`, `invocations`, `process_lifetimes`
* `src/alphamind/portfolio_state/records/positions.py` — `PositionRecord` shape the seed needs to materialize
* `src/alphamind/portfolio_state/records/theses.py` — `ThesisRecord` shape
* Parent issue `ALP-493` Pre-resolved decision § (C) — DB-path suffix-check shape
* Parent issue `ALP-493` Pre-resolved decision § (L) — explicit wipe-table list

## Depends on

* None — wave 1. Independent of 01a / 01b. The state-persistence substrate is already done.

## Scope

In scope: `src/alphamind/scheduler/debug_e2e/__init__.py` (empty placeholder) and `src/alphamind/scheduler/debug_e2e/seed.py`. Tests at `tests/scheduler/debug_e2e/test_seed.py`.

### 1\. Module skeleton

Create `src/alphamind/scheduler/debug_e2e/__init__.py` as an empty placeholder. Story 03 ships the public re-exports (`configure_debug_e2e`, `DebugE2ESettings`); leaving `__init__.py` empty here avoids merge conflicts on the re-export block.

### 2\. `wipe_and_seed`

```python
from typing import Any

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession


async def wipe_and_seed(
    *,
    session: AsyncSession,
    now: datetime,
    db_path: str,
    portfolio: Any,  # SyntheticPortfolio (story 02b); typed at the configure_debug_e2e seam in story 03
) -> None:
    """Wipe the 9 state-persistence tables and seed the synthetic portfolio.

    Refuses to run unless ``db_path`` ends with ``-debug-e2e.db`` (per
    parent Issue's Pre-resolved decision § (C)). Raises ``RuntimeError``
    with the refusing path on guard violation.
    """
    if not db_path.endswith("-debug-e2e.db"):
        msg = f"refusing to wipe non-debug DB path {db_path!r}"
        raise RuntimeError(msg)

    # Wipe in FK-safe order (children before parents).
    for table in (
        ActivityLog, BracketLegs, Brackets, PositionFills,
        PositionTheses, Positions, CashLedger, Invocations, ProcessLifetimes,
    ):
        await session.execute(delete(table))

    # Seed positions, theses, cash_ledger.
    for index, synthetic in enumerate(portfolio.positions):
        position_row = _build_position_row(synthetic, position_index=index, now=now)
        session.add(position_row)

    for synthetic_thesis in portfolio.theses:
        thesis_row = _build_thesis_row(synthetic_thesis, now=now)
        session.add(thesis_row)

    cash_row = _build_cash_ledger_row(portfolio.starting_cash_usd, now=now)
    session.add(cash_row)

    await session.commit()
```

The `_build_position_row` / `_build_thesis_row` / `_build_cash_ledger_row` helpers translate the `SyntheticEquity` / `SyntheticOption` / `SyntheticStrategy` / `SyntheticThesis` dataclasses into the corresponding ORM rows. Helpers live in the same file; read the as-built ORM models to determine required fields.

`expiration_offset_days` on `SyntheticOption` / `SyntheticStrategy` is added to `now` to compute the actual expiration date stamped on each row.

### Out of scope

* Calling `wipe_and_seed` from the CLI — story 04 owns the integration.
* The `SyntheticPortfolio` data type itself (story 02b ships it; this story accepts it as `Any`).
* Wiring `process_lifetimes` after wipe — the CLI helper in story 04 calls `record_process_lifetime` after `wipe_and_seed` completes; this seed only wipes the table per the design contract.
* Seeding `options_chains` — deferred per parent Issue Pre-resolved decision § (H).

## Acceptance criteria

- [ ] `src/alphamind/scheduler/debug_e2e/__init__.py` exists (empty placeholder; story 03 re-exports the public surface here).
- [ ] `src/alphamind/scheduler/debug_e2e/seed.py` exposes `wipe_and_seed(*, session, now, db_path, portfolio)` per the signature above.
- [ ] `wipe_and_seed` raises `RuntimeError` when `db_path` does not end with `-debug-e2e.db`; the error message includes the refusing path quoted.
- [ ] After `wipe_and_seed` against an in-memory SQLite with the state-persistence schema applied + a minimal fixture `SyntheticPortfolio` shape, the 9 named tables are wiped clean of pre-existing rows AND `positions` has one row per portfolio position AND `position_theses` has one row per portfolio thesis AND `cash_ledger` has exactly one row with `starting_cash_usd` populated.
- [ ] Wipe ordering is FK-safe — children (`activity_log`, `bracket_legs`, `brackets`, `position_fills`, `position_theses`) before parents (`positions`, `cash_ledger`, `invocations`, `process_lifetimes`).
- [ ] `tests/scheduler/debug_e2e/test_seed.py` covers (a) guard refuses non-debug path with raised error, (b) wipe leaves tables empty against pre-populated fixture, (c) seed inserts the expected position/thesis/cash rows from a fixture `SyntheticPortfolio` literal.
- [ ] `uv run pytest tests/scheduler/debug_e2e/ -n auto` passes; full linter chain clean.

## Verification

`uv run pytest tests/scheduler/debug_e2e/test_seed.py -n auto` passes. Linter chain clean.