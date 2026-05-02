---
status: done
completed_date: 2026-04-27
commit_id: 654dab1
---

# 04b — Repository protocol & stub

## Goal

Declare `PortfolioStateRepository`, a `typing.Protocol` (runtime-checkable) the snapshot assembler
depends on, at `src/alphamind/portfolio_state/repository.py`. Production wires the protocol against
the OMS database (state-persistence story set, not yet implemented). This story provides the Protocol
plus a `StubPortfolioStateRepository` for tests plus structured exception types plus the small helper
value objects the assembler needs for invocation scaffolding and portfolio-pnl rollup.

## Reading

- `../../../design/01-data-layer/internal/portfolio-state.md` — the six raw state categories the
  Repository surfaces; method-per-category granularity follows the design's structural hierarchy
- `../../../design/05-execution-layer/state-persistence.md` § Read paths § Invocation snapshot —
  the read-isolation contract ("the snapshot must include every Phase 1 fill-integration mutation
  and must not include any Phase 2 command-execution mutation")
- `02-package-skeleton-and-config.md` — package layout; `src/alphamind/portfolio_state/repository.py`
  is this story's target
- `03a-position-records.md` — `PositionRecord` (categories 1a and 1b)
- `03b-thesis-records.md` — `ThesisRecord`, `RecentThesisResolution` (category 3)
- `03c-order-and-bracket-records.md` — `OrderRecord`, `BracketRecord` (category 4b and brackets)
- `03d-capital-state-records.md` — `CashLedger`, `DrawdownState`, `RiskBudgetConsumption`,
  `ActiveRiskParameterSet` (categories 2c, 4a, 4c, 4d)
- `03e-activity-log-records.md` — `ActivityLogEntry` (category 5)
- `03f-thesis-quality-aggregate-records.md` — `ThesisQualityAggregate` (category 6)
- `../../../implementation/03-analysis-layer/synthesizer/05b-portfolio-state-read-protocol.md` —
  sibling Protocol-and-stub pattern this story mirrors; the synthesizer's slim `PortfolioStateReader`
  can later converge against the canonical types here

## Depends on

- 02
- 03a, 03b, 03c, 03d, 03e, 03f (imports the record types these stories define)

## Scope

In scope:

### 1. Exception types

- `class RepositoryReadError(RuntimeError)` — base exception for any infrastructure failure (DB
  connection, transaction abort, isolation-level negotiation error). The production reader catches
  and re-wraps low-level storage errors into this type. The stub never raises this in normal use.

- `class RepositoryConsistencyError(RepositoryReadError)` — raised when the production reader
  detects a Phase 1 / Phase 2 isolation violation (e.g., snapshot reads see Phase 2 mutations). The
  stub never raises this; production implementations that detect an isolation violation raise it for
  fail-loud surfacing consistent with AlphaMind's abort-on-failure invocation contract.

Both classes carry the `RuntimeError` message in the ordinary way; no additional fields.

### 2. Helper value objects — all Pydantic v2 `BaseModel`, `model_config = {"frozen": True}`

**`PortfolioPnLInputs`** — carries the raw OMS-aggregate inputs needed for portfolio-pnl rollup
(category 2b). The assembler computes `total_unrealized_pnl_usd` separately from open positions;
these are the persistence-layer fields:

- `daily_realized_pnl_usd: float` — realized P/L from positions closed today
- `cumulative_realized_pnl_usd: float` — lifetime realized track record
- `rolling_realized_pnl: dict[str, float]` — trailing realized P/L keyed by window label (e.g.,
  `"1d"`, `"3d"`, `"5d"`, `"20d"`); keys are opaque strings owned by the persistence layer; values
  are finite floats
- `win_rate_pct: float | None` — trailing win rate as a percentage; `None` when there are no
  resolved positions; finite and in `[0, 100]` when present
- `average_win_size_usd: float | None` — average realized gain per winning position; `None` when
  there are no winning positions; finite and non-negative when present
- `average_loss_size_usd: float | None` — average realized loss per losing position expressed as a
  non-negative dollar amount; `None` when there are no losing positions; finite and non-negative
  when present
- `profit_factor: float | None` — total gains divided by total losses; `None` when total losses are
  zero or there are no resolved positions; finite and non-negative when present

Validation:
- `daily_realized_pnl_usd` and `cumulative_realized_pnl_usd` are finite floats (no NaN, no ±Inf).
- All values in `rolling_realized_pnl` are finite floats.
- `win_rate_pct` is in `[0, 100]` when not `None`.
- `average_win_size_usd`, `average_loss_size_usd`, `profit_factor` are non-negative when not `None`.

**`CurrentInvocationMetadata`** — carries the identity fields the assembler uses to seal the snapshot
with consistent provenance:

- `invocation_id: str` — non-empty; the canonical identifier for the current pipeline invocation
- `phase1_committed_at: datetime` — tz-aware UTC; when Phase 1's fill-integration transaction
  committed; used by the assembler and freshness module
- `pipeline_invocation_started_at: datetime | None` — tz-aware UTC; when the pipeline runner
  started this invocation; `None` when the runtime does not record a separate start timestamp
  (acceptable for early implementations)

Validation:
- `invocation_id` non-empty (`Field(min_length=1)`).
- `phase1_committed_at` is tz-aware UTC; naive datetime raises `ValidationError`.
- `pipeline_invocation_started_at` is tz-aware UTC when not `None`; naive datetime raises
  `ValidationError`.

**`PriorInvocationContext`** — small value object used by the assembler to compute
`parameter_change_flag` on `ActiveRiskParameterSet`. Carries prior-invocation state so the
assembler can detect overnight regime shifts without querying the DB a second time:

- `prior_invocation_id: str | None` — `None` when this is the first invocation in process
  lifetime; the assembler handles `None` gracefully (no prior parameters to compare against)
- `prior_active_risk_parameters: ActiveRiskParameterSet | None` — `None` when
  `prior_invocation_id` is `None`
- `prior_phase1_committed_at: datetime | None` — tz-aware UTC when not `None`; the
  `phase1_committed_at` timestamp from the prior invocation

Validation:
- `prior_active_risk_parameters` must be `None` when `prior_invocation_id` is `None`; and
  non-`None` when `prior_invocation_id` is non-`None`. A `model_validator` enforces this co-null
  invariant.
- `prior_phase1_committed_at` is tz-aware UTC when not `None`; naive datetime raises
  `ValidationError`.

### 3. `PortfolioStateRepository` Protocol

```python
@runtime_checkable
class PortfolioStateRepository(Protocol):
    ...
```

All methods are `async def`. All raise `RepositoryReadError` (or a subclass) on infrastructure
failures. Empty results are in-band — empty tuples, empty dicts, and zero-field aggregates are
normal; they never trigger an exception.

**Category 1 — Position inventory:**

- `async def get_open_positions(self) -> tuple[PositionRecord, ...]`
  Returns all open positions. Consumer-facing computed fields (`current_market_value_usd`,
  `position_weight_pct`, `unrealized_pnl_usd`, etc. per story 03a) are populated by the production
  reader before return; for the stub, these are taken from constructor input. Empty tuple when there
  are no open positions.

- `async def get_pending_positions(self) -> tuple[PositionRecord, ...]`
  Returns all positions with `status == PENDING` (entry order submitted, not yet filled). Empty
  tuple when there are no pending positions.

**Category 2c — Drawdown:**

- `async def get_drawdown_state(self) -> DrawdownState`
  Returns the current drawdown state. Always returns exactly one record; a zero-drawdown portfolio
  returns a `DrawdownState` with all percentage fields at zero.

**Category 2 rollup helper:**

- `async def get_portfolio_pnl_inputs(self) -> PortfolioPnLInputs`
  Returns the OMS-aggregate inputs needed for portfolio-pnl rollup. The assembler computes
  `total_unrealized_pnl_usd` from the open positions; this method supplies the OMS-aggregate fields.

**Category 3 — Thesis registry:**

- `async def get_active_theses(self) -> tuple[ThesisRecord, ...]`
  Returns all theses with lifecycle `status == ACTIVE`. Empty tuple when there are no active theses.

- `async def get_recent_thesis_resolutions(self, *, lookback_trading_days: int) -> tuple[RecentThesisResolution, ...]`
  Returns recently resolved theses within `lookback_trading_days` trading days. `lookback_trading_days`
  must be positive; the production reader maps trading days to calendar dates before querying. Empty
  tuple when no theses were resolved in the window.

**Category 4 — Capital and capacity:**

- `async def get_cash_ledger(self) -> CashLedger`
  Returns the current cash ledger. Always returns exactly one record.

- `async def get_pending_orders(self) -> tuple[OrderRecord, ...]`
  Returns all orders with `status` in `{PENDING, PARTIALLY_FILLED}`. Empty tuple when there are no
  pending orders.

- `async def get_risk_budget_consumption(self) -> RiskBudgetConsumption`
  Returns the current risk budget consumption snapshot across all guardrail rules. Always returns
  exactly one record.

- `async def get_active_risk_parameters(self) -> ActiveRiskParameterSet`
  Returns the current active risk parameter set. Always returns exactly one record.

**Category 5 — Activity log:**

- `async def get_intra_invocation_changelog(self, *, invocation_id: str) -> tuple[ActivityLogEntry, ...]`
  Returns all activity log entries carrying `invocation_id`, ordered chronologically. Empty tuple
  when no entries exist for the given invocation.

- `async def get_recent_pm_decision_log(self, *, sliding_window_invocations: int) -> tuple[ActivityLogEntry, ...]`
  Returns all `pm_decision` entries from the most recent `sliding_window_invocations` invocations,
  ordered chronologically. `sliding_window_invocations` must be positive. Empty tuple when no
  `pm_decision` entries exist in the window.

- `async def get_position_modification_trail(self, *, position_ids: tuple[str, ...]) -> dict[str, tuple[ActivityLogEntry, ...]]`
  Returns a mapping from each `position_id` to the ordered-chronological activity log entries scoped
  to that position. For position IDs that have no matching entries (including unknown IDs), the key
  is omitted from the result dict — consumers receive only entries that exist. An empty dict is
  returned when `position_ids` is empty or all requested IDs have no associated log entries. Never
  raises for unknown IDs.

**Category 6 — Thesis quality:**

- `async def get_thesis_quality_aggregates(self) -> ThesisQualityAggregate`
  Returns the current thesis quality aggregate record. Always returns exactly one record.

**Brackets:**

- `async def get_brackets_for_positions(self, *, position_ids: tuple[str, ...]) -> tuple[BracketRecord, ...]`
  Returns all brackets whose `position_id` is in `position_ids`. Empty tuple when `position_ids` is
  empty or no brackets match. Never raises for unknown position IDs.

**Invocation scaffolding:**

- `async def get_current_invocation_metadata(self) -> CurrentInvocationMetadata`
  Returns the metadata for the currently running invocation. Always returns exactly one record;
  production implementations read this from the invocation record written at pipeline startup.

- `async def get_prior_invocation_context(self) -> PriorInvocationContext`
  Returns context from the prior invocation for parameter-change-flag computation. Returns a
  `PriorInvocationContext` with `prior_invocation_id = None` when this is the first invocation in
  process lifetime.

### 4. `RepositoryFixture` value object

A frozen Pydantic v2 value object carrying every field the Protocol returns. Passed to
`StubPortfolioStateRepository` at construction to keep test setup readable and explicit.

```python
class RepositoryFixture(BaseModel):
    model_config = {"frozen": True}

    open_positions: tuple[PositionRecord, ...]
    pending_positions: tuple[PositionRecord, ...]
    drawdown_state: DrawdownState
    portfolio_pnl_inputs: PortfolioPnLInputs
    active_theses: tuple[ThesisRecord, ...]
    recent_thesis_resolutions: tuple[RecentThesisResolution, ...]
    cash_ledger: CashLedger
    pending_orders: tuple[OrderRecord, ...]
    risk_budget: RiskBudgetConsumption
    active_risk_parameters: ActiveRiskParameterSet
    intra_invocation_changelog: tuple[ActivityLogEntry, ...]
    recent_pm_decision_log: tuple[ActivityLogEntry, ...]
    position_modification_trail: dict[str, tuple[ActivityLogEntry, ...]]
    thesis_quality_aggregates: ThesisQualityAggregate
    brackets: tuple[BracketRecord, ...]
    current_invocation_metadata: CurrentInvocationMetadata
    prior_invocation_context: PriorInvocationContext
```

Pydantic v2 validates types at construction; passing a non-tuple for `open_positions` (for example)
raises `ValidationError` with a path-bearing message.

### 5. `StubPortfolioStateRepository`

Concrete implementation backed by a `RepositoryFixture`. Satisfies `PortfolioStateRepository` at
both type-check and runtime (`isinstance` check).

Constructor: `StubPortfolioStateRepository(fixture: RepositoryFixture)`.

Every protocol method is `async def` returning the constructor-supplied value synchronously from
inside the coroutine. The stub does not raise; it does not read from any external source.

For `get_intra_invocation_changelog`, the stub ignores the `invocation_id` parameter and returns
`fixture.intra_invocation_changelog` unconditionally — tests that need invocation-scoped filtering
construct the fixture accordingly.

For `get_recent_pm_decision_log`, the stub ignores `sliding_window_invocations` and returns
`fixture.recent_pm_decision_log`.

For `get_position_modification_trail`, the stub returns the intersection of the requested
`position_ids` with `fixture.position_modification_trail` — only keys present in both are returned.
Requesting unknown IDs returns only the known subset (or `{}` if none match), consistent with the
protocol's "empty in-band" contract.

For `get_recent_thesis_resolutions`, the stub ignores `lookback_trading_days` and returns
`fixture.recent_thesis_resolutions`.

For `get_brackets_for_positions`, the stub returns all brackets in `fixture.brackets` whose
`position_id` is in `position_ids`.

### 6. Tests at `tests/portfolio_state/test_repository.py`

- `RepositoryReadError` exists and is a subclass of `RuntimeError`.
- `RepositoryConsistencyError` exists and is a subclass of `RepositoryReadError`.
- `PortfolioPnLInputs` constructed with valid fields passes; `win_rate_pct` outside `[0, 100]`
  raises `ValidationError`; any field that must be non-negative raises `ValidationError` when
  negative; non-finite `daily_realized_pnl_usd` raises `ValidationError`.
- `CurrentInvocationMetadata` with a naive `phase1_committed_at` raises `ValidationError`; with a
  naive `pipeline_invocation_started_at` raises `ValidationError`; empty `invocation_id` raises
  `ValidationError`.
- `PriorInvocationContext` co-null invariant: `prior_invocation_id = None` with
  `prior_active_risk_parameters` non-`None` raises `ValidationError`; `prior_invocation_id`
  non-`None` with `prior_active_risk_parameters = None` raises `ValidationError`; both `None`
  passes; both non-`None` passes.
- `PriorInvocationContext` with a naive `prior_phase1_committed_at` raises `ValidationError`.
- `RepositoryFixture` with a non-tuple for `open_positions` raises `ValidationError` (type
  enforcement at construction).
- `StubPortfolioStateRepository` constructed from a happy-path fixture returns the expected records
  via every protocol method.
- `isinstance(stub, PortfolioStateRepository)` returns `True` (runtime_checkable).
- `mypy` confirms `StubPortfolioStateRepository` satisfies `PortfolioStateRepository`.
- `get_position_modification_trail` called with position IDs not present in the fixture returns `{}`
  (no exception, empty in-band).
- `get_position_modification_trail` called with a mix of known and unknown IDs returns only the
  known subset.
- `get_brackets_for_positions` with an empty tuple returns an empty tuple (no exception).
- `get_recent_thesis_resolutions` with any `lookback_trading_days` returns the fixture's resolutions
  (stub ignores the parameter).

Out of scope:

- The production OMS-backed implementation (state-persistence story set + future "Portfolio state
  runtime" story tree).
- Snapshot assembly logic (story 06).
- Per-consumer view projections (story 07).
- Freshness / staleness reporting (story 08; the Protocol returns OMS-side `phase1_committed_at`,
  but freshness comparison and staleness flags are the assembler/freshness module's concern).
- Caching across calls (the protocol is per-call; production implementations may cache
  transparently).
- `RepositoryFactory`, `RepositoryRegistry`, or any DI container — the stub is constructed directly
  in tests, the production reader is constructed by the pipeline runner.
- Failure-injection stubs (tests that need to simulate `RepositoryReadError` construct a tiny
  adapter ad hoc; not this story's concern).

## Notes

The Protocol is `async` even though the stub returns synchronously from inside its coroutines. This
matches the production wiring against `aiosqlite` and lets stubs and production share the same
Protocol without runtime-shape friction. The natural shape for a stub satisfying an `async` Protocol
is `async def get_foo(self): return self._fixture.foo` — no `await` required.

`PortfolioPnLInputs` and `CurrentInvocationMetadata` are introduced here because they are tightly
bound to the Repository's read surface. The assembler (story 06) consumes them alongside the typed
`PortfolioPnL` field in the snapshot. They are simple frozen Pydantic models with the documented
fields; no computation logic.

`RepositoryConsistencyError` is a hook for the production reader's Phase 1 / Phase 2 isolation
violation detection per `state-persistence.md` § Snapshot isolation: "the snapshot must include
every Phase 1 fill-integration mutation and must not include any Phase 2 command-execution
mutation." The stub never raises it; production code that detects the invariant has been violated
raises it for fail-loud surfacing.

Per `feedback_no_inventing_component_names.md`, every method name is derived from the category
names in `portfolio-state.md`. The method-per-category granularity follows the design's structural
hierarchy, enabling the production implementation to issue one DB query per method call.

Per `feedback_simplify_before_building.md`, the stub takes a `RepositoryFixture` rather than a long
constructor argument list. One Protocol, one stub, no factory or registry.

The `StubPortfolioStateRepository` is the happy-path fixture. Tests simulating `RepositoryReadError`
or `RepositoryConsistencyError` construct a minimal adapter in the test file itself (a one-method
class that raises on a given call) — that pattern is the assembler's test concern, not this story's.

The synthesizer's slim `PortfolioStateReader` Protocol at
`src/alphamind/analysis/synthesizer/portfolio_state.py` (story 05b) is a separate, independent
protocol. Its three methods can converge against the canonical types here when its 05b/06b stories
merge to main. Convergence is a future migration, not a blocker.

## Acceptance criteria

- [ ] `PortfolioStateRepository` is declared as `typing.Protocol`, decorated with
      `@runtime_checkable`, with all documented async methods.
- [ ] `RepositoryReadError` is a subclass of `RuntimeError`; `RepositoryConsistencyError` is a
      subclass of `RepositoryReadError`.
- [ ] `PortfolioPnLInputs` is a frozen Pydantic v2 model with the documented fields; `win_rate_pct`
      outside `[0, 100]`, negative `average_win_size_usd`, negative `average_loss_size_usd`,
      negative `profit_factor`, and non-finite `daily_realized_pnl_usd` each raise `ValidationError`.
- [ ] `CurrentInvocationMetadata` is a frozen Pydantic v2 model with the documented fields; empty
      `invocation_id` raises `ValidationError`; naive `phase1_committed_at` or naive
      `pipeline_invocation_started_at` each raise `ValidationError`.
- [ ] `PriorInvocationContext` is a frozen Pydantic v2 model; the co-null invariant between
      `prior_invocation_id` and `prior_active_risk_parameters` is enforced; naive
      `prior_phase1_committed_at` raises `ValidationError`.
- [ ] `RepositoryFixture` is a frozen Pydantic v2 model with the documented fields; a non-tuple for
      `open_positions` raises `ValidationError`.
- [ ] `StubPortfolioStateRepository` implements `PortfolioStateRepository` and returns
      constructor-provided values for every method.
- [ ] `mypy` confirms `StubPortfolioStateRepository` satisfies `PortfolioStateRepository`.
- [ ] `isinstance(stub, PortfolioStateRepository)` returns `True` at runtime.
- [ ] `get_position_modification_trail` with all-unknown position IDs returns `{}` (no exception).
- [ ] `get_position_modification_trail` with a mix of known and unknown IDs returns only the known
      subset.
- [ ] `get_brackets_for_positions` with an empty `position_ids` tuple returns an empty tuple.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
