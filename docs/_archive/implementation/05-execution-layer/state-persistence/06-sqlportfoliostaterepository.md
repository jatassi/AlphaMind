# 06 — SqlPortfolioStateRepository

## Goal

Ship a concrete `SqlPortfolioStateRepository` implementing the 17-method `PortfolioStateRepository` Protocol against the SQL tables shipped in stories 02b–05. Backed by SQLAlchemy queries, with snapshot-isolation enforcement raising `RepositoryConsistencyError` (already declared at `repository.py`) when a Phase 1 / Phase 2 reordering is detected. Tier 3 derived fields are served per parent issue Pre-resolved decision (D): drawdown from the singleton table, P/L inputs computed on-the-fly via SQL aggregation, thesis quality aggregates as empty default, active risk parameters via injected callable provider. After this story, the assembler at `src/alphamind/portfolio_state/assembler.py` can produce a real `PortfolioStateSnapshot` from durable state — closing the gate for [ALP-310](https://linear.app/alphamind-jatassi/issue/ALP-310) (Decision-layer pipeline composition wiring).

## Reading

* `docs/design/05-execution-layer/state-persistence.md` § Read paths — defines every Protocol method's expected return shape and isolation semantics
* `docs/design/05-execution-layer/state-persistence.md` § Snapshot isolation — defines the six-step sequencing contract `RepositoryConsistencyError` enforces
* `src/alphamind/portfolio_state/repository.py` — the `PortfolioStateRepository` Protocol with all 17 methods, the `RepositoryFixture` value object, and the `StubPortfolioStateRepository` reference implementation. The SQL impl conforms to the same surface.
* `src/alphamind/portfolio_state/assembler.py` § `assemble_snapshot` — the consumer of every repository method; the assembly sequence shows which methods feed which snapshot fields
* `src/alphamind/portfolio_state/snapshot.py` — the `PortfolioStateSnapshot` aggregate the assembler produces; verify the repository return shapes match
* Stories 04a–04e and 05 — the SQL tables this repo queries
* Story 02b — the `invocations` table the repo reads for `get_current_invocation_metadata` and `get_prior_invocation_context`
* Parent issue Pre-resolved decision (D) — the per-field strategy for Tier 3 derived/aggregate methods

## Depends on

* <issue id="1ad6e0b5-0fad-4f8b-ad91-73f2727e6954">ALP-358</issue>, <issue id="1d2ac5c9-147e-4e8d-a8e7-381bacf5bd1c">ALP-359</issue>, <issue id="7049d546-26ff-4d45-a1ac-455c6093e97b">ALP-360</issue>, <issue id="bbb086bc-f1fe-4d16-a05d-298ddbef420c">ALP-361</issue>, <issue id="cef661a7-07a2-42d5-b294-f7a5537b6cfa">ALP-362</issue>, <issue id="781f1de8-96c2-4d06-8916-d8e76d69c716">ALP-363</issue> (this work tree, stories 04a–04e and 05) — every Tier 1 + the fill_records substrate must exist before the repo can read from them.

## Scope

In scope, all under `src/alphamind/execution/state_persistence/repository/`. Tests at `tests/execution/state_persistence/`.

### 1\. `SqlPortfolioStateRepository` class

In `src/alphamind/execution/state_persistence/repository/sql_repository.py`:

```python
class SqlPortfolioStateRepository:
    def __init__(
        self,
        session_factory: sessionmaker[AsyncSession],
        invocation_id: str,
        active_risk_parameters_provider: Callable[[], Awaitable[ActiveRiskParameterSet]],
        config: StatePersistenceConfig,
    ) -> None:
        ...
```

The class implements every method of the Protocol. Each query opens a fresh `AsyncSession` from the factory (the repo does not cache results across reads — the assembler reads each method once per snapshot).

### 2\. Tier 1 methods (read directly from tables)

* `get_open_positions()` — `SELECT * FROM positions WHERE status = 'OPEN'`, codec to tuple of `PositionRecord`
* `get_pending_positions()` — `SELECT * FROM positions WHERE status = 'PENDING'`
* `get_active_theses()` — `SELECT * FROM theses WHERE status = 'ACTIVE'`, JOIN `thesis_components` per parent, codec to tuple of `ThesisRecord`
* `get_recent_thesis_resolutions(lookback_trading_days)` — `SELECT * FROM theses WHERE status = 'RESOLVED' AND resolution_timestamp >= ?`, transformed to `RecentThesisResolution` summary projection (without component-level outcomes initially per design doc)
* `get_cash_ledger()` — `SELECT * FROM cash_ledger WHERE id = 'current'`, codec to `CashLedger`
* `get_pending_orders()` — `SELECT * FROM orders WHERE status IN ('PENDING', 'PARTIALLY_FILLED')`
* `get_brackets_for_positions(position_ids)` — `SELECT * FROM brackets WHERE position_id IN (...)`, JOIN `bracket_legs`

### 3\. Tier 2 methods (read from activity log + invocations)

* `get_intra_invocation_changelog(invocation_id)` — delegates to `read_intra_invocation_changelog` from story 03
* `get_recent_pm_decision_log(sliding_window_invocations)` — delegates to `read_recent_pm_decision_log`
* `get_position_modification_trail(position_ids)` — delegates to `read_position_modification_trail`
* `get_current_invocation_metadata()` — `SELECT phase1_completed_at, start_at FROM invocations WHERE invocation_id = ?` (the stored invocation_id at construction); raises `RepositoryConsistencyError` if `phase1_completed_at IS NULL` (snapshot read before Phase 1 commit)
* `get_prior_invocation_context()` — `SELECT * FROM invocations WHERE invocation_id < ? ORDER BY start_at DESC LIMIT 1` (the most recent prior invocation); returns `PriorInvocationContext` with `prior_active_risk_parameters` reconstructed from the prior invocation's resolved-config snapshot file path

### 4\. Tier 3 methods (mixed strategy)

* `get_drawdown_state()` — `SELECT * FROM drawdown_state WHERE id = 'current'`
* `get_portfolio_pnl_inputs()` — computed on-the-fly via SQL aggregation: `SELECT SUM(realized_pnl_to_date_usd) FROM positions WHERE status = 'CLOSED' GROUP BY ...` plus rolling-window aggregations (1d, 3d, 5d, 20d) over `closed_at`. Returns `PortfolioPnLInputs`.
* `get_thesis_quality_aggregates()` — returns an empty default `ThesisQualityAggregate` (zero counts, None hit rates) until the thesis-quality computation lands in feedback-loop work. Document the placeholder in the function docstring.
* `get_active_risk_parameters()` — `await active_risk_parameters_provider()`; the provider is injected at construction (composition pipeline supplies it)
* `get_risk_budget_consumption()` — computed at read time; for the v1 implementation, return a zero-valued `RiskBudgetConsumption` with a docstring noting the Phase 1 enrichment is in story 07's snapshot-assembly path. (The `assembler.py` handles the actual computation by combining position + cash + active risk params; this method serves as a passthrough surface satisfying the Protocol.)

### 5\. Snapshot isolation enforcement

`RepositoryConsistencyError` raised when:

* `get_current_invocation_metadata()` is called and the invocation row's `phase1_completed_at IS NULL` (Phase 1 has not committed)
* The repo detects a Phase 2 mutation visible during a read (cross-read consistency check via the SQLite snapshot pragma — implementation detail)

### 6\. Public assembler factory

In `src/alphamind/execution/state_persistence/repository/__init__.py`:

```python
def build_sql_portfolio_state_repository(
    *,
    session_factory: sessionmaker[AsyncSession],
    invocation_id: str,
    active_risk_parameters_provider: Callable[[], Awaitable[ActiveRiskParameterSet]],
    config: StatePersistenceConfig,
) -> PortfolioStateRepository:
    """Construct a production SqlPortfolioStateRepository conforming to the Protocol."""
```

### Out of scope

* No write paths — Phase 1 (story 07) and Phase 2 (story 08) own writes.
* No assembler changes — the existing assembler at `src/alphamind/portfolio_state/assembler.py` works against the Protocol; this story just provides a conforming concrete impl.
* No thesis-quality aggregation logic — empty default per Pre-resolved decision (D).

## Acceptance criteria

- [ ] `SqlPortfolioStateRepository` implements all 17 methods of `PortfolioStateRepository`; `isinstance(repo, PortfolioStateRepository)` returns True at runtime via `runtime_checkable`.
- [ ] Each Tier 1 method returns the typed-record shape expected by the Protocol; round-trip parity with `StubPortfolioStateRepository` is property-tested.
- [ ] `get_current_invocation_metadata()` raises `RepositoryConsistencyError` when `phase1_completed_at IS NULL`.
- [ ] `get_prior_invocation_context()` returns `(prior_id, prior_params)` both non-None when a prior invocation exists, and `(None, None)` when this is the first invocation.
- [ ] `get_thesis_quality_aggregates()` returns an empty default `ThesisQualityAggregate` and the docstring names the placeholder.
- [ ] `get_active_risk_parameters()` invokes the injected provider exactly once per call.
- [ ] `assemble_snapshot(repo, ...)` (from `src/alphamind/portfolio_state/assembler.py`) produces a `PortfolioStateSnapshot` when fed a fully-populated SQL DB — end-to-end integration test.
- [ ] `tests/execution/state_persistence/test_sql_repository.py` covers each Protocol method against a populated test DB and asserts return-shape parity with a fixture-built `StubPortfolioStateRepository` over the same data.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` reports clean.
- [ ] `uv run pytest tests/execution/state_persistence/ tests/portfolio_state/ -n auto` passes (broader run verifies no regression on existing portfolio_state tests).

## Verification

Run `uv run pytest tests/execution/state_persistence/test_sql_repository.py -n auto`. Verify the integration test that runs `assemble_snapshot(repo)` end-to-end produces a non-trivial `PortfolioStateSnapshot` with each category populated. Spot-check the parity test that builds the same DB state via SQL inserts and via `RepositoryFixture` and confirms the two repos return equal results.