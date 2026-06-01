# 06b — Assembler trailing-window aggregation

## Goal

Replace the `0.0` placeholders on `CashLedger.regt_excess_trailing_30d_usd / 90d_usd / lifetime_usd` with computed aggregates summed over fill-record metadata. Add a `get_regt_excess_aggregates(now)` method to `SqlPortfolioStateRepository` and call it from assembler Step 11. Trailing windows are calendar-day-anchored per parent decision (E). Lifetime sums every fill carrying non-null `regt_attribution_json`.

## Reading

* `docs/design/05-execution-layer/regt-margin-attribution.md` § Aggregation and delivery — confirms aggregates are computed at delivery time by summing across fill-record metadata; no separate aggregate table.
* `docs/design/01-data-layer/internal/portfolio-state.md` § 4a Cash and buying power — destination of the trailing fields.
* `src/alphamind/portfolio_state/records/cash.py:42-57` — `CashLedger` typed record; the three target fields already exist with `_FiniteFloat` annotations.
* `src/alphamind/portfolio_state/assembler.py:600-617` — Step 11 (CashLedger enrichment) where the new aggregates wire in alongside `cash_pct_of_portfolio` and `true_deployable_capital_usd`.
* `src/alphamind/execution/state_persistence/repository/sql_repository.py:209-227` — existing `get_cash_ledger` returning the typed record with `regt_excess_*=0.0` placeholders.
* `src/alphamind/execution/state_persistence/tables/fill_records.py` — `FillRecordRow.processing_timestamp` (date anchor for trailing windows), `processing_status`, `regt_attribution_json`.
* `ALP-126` parent issue § Pre-resolved decision (E) — calendar-day granularity; lifetime starts from fills bearing metadata.

## Depends on

* `ALP-427` (05 — Per-fill attribution orchestrator) — fills won't carry attribution until story 06a (which depends on 05) lands. Aggregation can technically be written before but is functionally meaningful only after fills are populated; dep on 05 keeps the wave structure clean.

## Scope

In scope: `src/alphamind/execution/regt_margin_attribution/aggregates.py` (new), `src/alphamind/execution/state_persistence/repository/sql_repository.py` (new method), `src/alphamind/portfolio_state/assembler.py` (Step 11 enrichment), `tests/execution/state_persistence/test_sql_repository.py` (new method tests), `tests/portfolio_state/test_assembler.py` (assembler integration test).

### 1\. `RegTExcessAggregates` typed record

In `src/alphamind/execution/regt_margin_attribution/aggregates.py`:

```python
@dataclass(frozen=True, slots=True)
class RegTExcessAggregates:
    trailing_30d_usd: float
    trailing_90d_usd: float
    lifetime_usd: float
```

Frozen, slotted, no validation beyond construction. Exposed via `__init__.py`'s `__all__`.

### 2\. Repository method

Add to `SqlPortfolioStateRepository`:

```python
async def get_regt_excess_aggregates(
    self, now: datetime,
) -> RegTExcessAggregates:
    """Sum regt_excess_over_pm across fill-record metadata.

    trailing_30d_usd: fills with processing_timestamp >= now - 30 days.
    trailing_90d_usd: fills with processing_timestamp >= now - 90 days.
    lifetime_usd: every processed fill with non-null regt_attribution_json.

    Fills predating the attribution module (regt_attribution_json IS NULL)
    contribute zero to every window.
    """
```

Implementation: a single SQL query that aggregates over `FillRecordRow` filtered by `processing_status = 'processed' AND regt_attribution_json IS NOT NULL`, JSON-extracting `regt_excess_over_pm`. SQLite supports `json_extract` natively. The query sums three `CASE WHEN` branches (lifetime always; trailing-30d if `processing_timestamp >= cutoff_30d`; trailing-90d if `processing_timestamp >= cutoff_90d`) returning one row with three values — one round-trip, not three.

### 3\. Assembler Step 11 wiring

In `src/alphamind/portfolio_state/assembler.py`, modify Step 11:

* Call `repository.get_regt_excess_aggregates(now)` alongside the other reads (group it into the Step 2 batched-fetch wave if possible; otherwise call it once before Step 11 enrichment).
* In the `cash_ledger_raw.model_copy(update={...})` call at Step 11, populate the three `regt_excess_trailing_*_usd` fields from the aggregates result.

### 4\. Keep `SqlPortfolioStateRepository.get_cash_ledger` working

The existing `get_cash_ledger` call site passes `regt_excess_trailing_*=0.0` as placeholders to `cash_ledger_record_from_row`. Leave those placeholders in place — they keep direct callers of `get_cash_ledger` (e.g., the breach-behavior layer) working unchanged. The assembler's Step 11 overwrites them with the real aggregates for snapshot consumers.

### 5\. Tests

`tests/execution/state_persistence/test_sql_repository.py`:

* `test_get_regt_excess_aggregates_empty_table_returns_zeros` — no fills → `(0.0, 0.0, 0.0)`.
* `test_get_regt_excess_aggregates_null_attribution_contributes_zero` — fills with `regt_attribution_json IS NULL` are excluded.
* `test_get_regt_excess_aggregates_unprocessed_fill_excluded` — fills with `processing_status='unprocessed'` excluded even with non-null attribution (defense-in-depth; production shouldn't produce this state).
* `test_get_regt_excess_aggregates_trailing_30d_window_calendar_days` — three fills: at `now-1d`, `now-31d`, `now-91d`. Trailing-30d contains the first; trailing-90d contains the first two; lifetime contains all three.
* `test_get_regt_excess_aggregates_sums_regt_excess_over_pm` — seed fills with known `regt_excess_over_pm` values; assert sums correct to `1e-9` tolerance.

`tests/portfolio_state/test_assembler.py`:

* `test_step_11_populates_regt_excess_trailing_fields_from_aggregates` — assembler integration test using a seeded fixture with known fills; assert the returned `enriched_cash` has the expected `regt_excess_trailing_30d_usd / 90d_usd / lifetime_usd` values.
* `test_step_11_zero_aggregates_when_no_fills` — empty fills table → all three fields `0.0`.

### Out of scope

* Display in the command center — downstream ([ALP-128](<https://linear.app/alphamind-jatassi/issue/ALP-128>) coordination only).
* Caching the aggregates across invocations — design does not call for it; recompute per invocation.
* Per-symbol or per-class-group rollup of the excess — design specifies portfolio-aggregate only.

## Acceptance criteria

- [ ] `RegTExcessAggregates` is defined in `src/alphamind/execution/regt_margin_attribution/aggregates.py` as a frozen, slotted dataclass.
- [ ] `SqlPortfolioStateRepository.get_regt_excess_aggregates(now)` returns the three calendar-day-anchored sums.
- [ ] The repository method uses one SQL query (no three-round-trip implementation).
- [ ] Fills with `regt_attribution_json IS NULL` contribute zero to every window.
- [ ] Fills with `processing_status != 'processed'` are excluded.
- [ ] Trailing-30d window includes fills with `processing_timestamp >= now − 30 calendar days`.
- [ ] Lifetime window sums every processed fill with non-null attribution.
- [ ] Assembler Step 11 calls `get_regt_excess_aggregates` and populates the three `CashLedger.regt_excess_trailing_*_usd` fields in the enriched record.
- [ ] All new tests in Scope §5 pass under `uv run pytest tests/ -n auto`.
- [ ] Existing assembler / repository tests still pass.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy` clean.

## Verification

Run `uv run pytest tests/execution/state_persistence/test_sql_repository.py tests/portfolio_state/test_assembler.py -n auto`. Inspect the assembler Step 11 to confirm: (a) one new method call; (b) three new fields in the `model_copy` update dict; (c) no other Step changes.