# Design — Scheduler `--debug-e2e` mode

**Date:** 2026-05-16
**Shape:** Long-running daemon — augmentation of the CLI entrypoint
**Status:** draft for review

---

## 1. What we're building

A new `--debug-e2e` flag on `python -m alphamind.scheduler run` that drives one
production-faithful pipeline pass without touching Alpaca and without depending
on whatever happens to be in the paper DB. The flag exists for a single
consumer: an agent operator who needs to verify the pipeline end-to-end while
debugging, and needs intra-phase progress signals so they can distinguish "the
synthesizer is still thinking" from "the synthesizer is hung."

Trust boundaries unchanged: LLM (Claude Agent SDK) is real, the production
analysis + decision pipelines run unaltered, the persistence substrate is the
same SQLAlchemy + SQLite. The two boundaries that change: (a) the broker
adapter is replaced with a log-only stand-in returning a fixed synthetic
portfolio in Alpaca's `PositionSnapshot` shape; (b) per-phase + per-agent-call
events stream to `<archive>/invocations/<id>/progress.jsonl`. Persistence:
debug-mode runs wipe and reseed the entire SQLite DB before each invocation
(per operator decision 2b — full reproducibility) — so the DB target must be a
dedicated debug DB, not the paper or production DB. Concurrency profile
matches production: one `run_invocation` pass, no daemon loop.

---

## 2. Principles guiding this design

- **P4 (hexagonal — explicit Protocol seams):** central. Today
  `scheduler/phase1_inputs.py` constructs `AlpacaClientFactory` inline and
  tests monkey-patch the module-level builder hooks. Swapping in a log-only
  broker forces the seam to be explicit — `AccountStateQueriesP` /
  `CorporateActionsQueriesP` Protocols on the broker adapter, injected via
  `RunInvocationContext`. The monkey-patch goes away as a side effect.
- **P9 (deep modules — small public surface):** central. The debug_e2e
  package exposes one function (`configure_debug_e2e`) and one type
  (`DebugE2ESettings`). Everything else — the synthetic portfolio, the
  log-only broker shims, the seeder, the JSONL emitter — is internal.
  Production code calls one function at the CLI entry; the orchestrator
  reads one field on the context.
- **P3 (illegal states unrepresentable):** central for two micro-decisions.
  (i) `--debug-e2e` is mutually exclusive with `--mode live` at the
  argparse level (not runtime-asserted). (ii) The progress emitter is a
  Protocol with a no-op default — call sites never branch on
  `emitter is None`, they always emit.
- **P1 (functional core, imperative shell):** moderate. The synthetic
  portfolio is pure data (a module-level frozen constant). The seeder and
  log-only broker are imperative shell. The split is enforced by import
  direction — `seed.py` and `broker.py` both import `portfolio.py`, not
  vice versa.

---

## 3. Package layout

```
src/alphamind/
├── execution/broker_adapter/
│   ├── queries.py                       # (existing) AccountStateQueries — Alpaca-backed
│   ├── corporate_actions_queries.py     # (existing) CorporateActionsQueries — Alpaca-backed
│   └── protocols.py                     # NEW — AccountStateQueriesP, CorporateActionsQueriesP
│
└── scheduler/
    ├── __main__.py                      # MODIFIED — +--debug-e2e flag on `run`
    ├── orchestrator.py                  # MODIFIED — read context.debug_e2e; thread emitter; trigger_source
    ├── phase1_inputs.py                 # MODIFIED — depend on Protocols, take factories from context
    ├── run_context.py                   # MODIFIED — +debug_e2e: DebugE2ESettings | None
    ├── progress.py                      # NEW — ProgressEmitter Protocol + NoOpProgressEmitter
    └── debug_e2e/
        ├── __init__.py                  # NEW — re-exports configure_debug_e2e, DebugE2ESettings
        ├── settings.py                  # NEW — DebugE2ESettings frozen dataclass + configure_debug_e2e()
        ├── portfolio.py                 # NEW — SYNTHETIC_PORTFOLIO data + Synthetic* dataclasses
        ├── seed.py                      # NEW — wipe_and_seed(session, now)
        ├── broker.py                    # NEW — LogOnlyAccountStateQueries, LogOnlyCorporateActionsQueries
        └── jsonl_emitter.py             # NEW — JsonlProgressEmitter

src/alphamind/analysis/{distillation,domain_researchers,qualitative_research,adaptive_research,synthesizer}/harness.py
src/alphamind/decision/{analyst,strategist,portfolio_manager}/harness.py
                                        # MODIFIED — accept progress: ProgressEmitter, wrap SDK call
```

**Why nest `debug_e2e/` under `scheduler/`.** The flag only makes sense on
the scheduler entry; the only callers of the debug-only code live in
`scheduler/__main__.py` and `scheduler/orchestrator.py`. Geographic isolation
makes the import-direction rule trivial to enforce (P2 — the import graph
IS the architecture).

**Why `progress.py` lives outside `debug_e2e/`.** Progress emission is a
general capability the production daemon may later want for observability.
Putting the Protocol + no-op default at scheduler-package level lets the
JSONL implementation stay debug-only without forcing the Protocol to follow.

**Import-linter contract** (add to `.importlinter`):

```toml
[[importlinter.contracts]]
name = "Production code must not import debug_e2e"
type = "forbidden"
source_modules = [
  "alphamind.scheduler.__main__",
  "alphamind.scheduler.orchestrator",
  "alphamind.scheduler.phase1_inputs",
  "alphamind.scheduler.run_context",
  "alphamind.scheduler.driver",
  "alphamind.scheduler.emergency",
]
forbidden_modules = ["alphamind.scheduler.debug_e2e"]
```

`__main__.py` and `orchestrator.py` need to *use* debug_e2e when the flag is
set — they do so by depending on `DebugE2ESettings` (Protocol-typed fields)
on `RunInvocationContext`, not by importing from `debug_e2e/` directly. The
construction site (`configure_debug_e2e`) is called from a guarded branch in
`__main__.py` that imports lazily inside the branch so the import-linter
contract holds at module-load time.

---

## 4. Types

```python
# scheduler/progress.py — general facility, no debug coupling

from typing import Any, Protocol


class ProgressEmitter(Protocol):
    """Per-invocation progress event sink.

    Default implementation is :class:`NoOpProgressEmitter`; debug-e2e mode
    swaps in :class:`JsonlProgressEmitter`. Call sites never branch on
    presence — the emitter is always non-None.
    """

    def phase_start(self, phase: str) -> None: ...
    def phase_done(self, phase: str, **fields: Any) -> None: ...
    def agent_request(self, *, phase: str, agent: str, model: str) -> None: ...
    def agent_response(
        self, *, phase: str, agent: str, model: str, duration_s: float,
        input_tokens: int, output_tokens: int,
    ) -> None: ...


class NoOpProgressEmitter:
    """No-op default the production daemon uses."""

    def phase_start(self, phase: str) -> None: ...
    def phase_done(self, phase: str, **fields: Any) -> None: ...
    def agent_request(self, **fields: Any) -> None: ...
    def agent_response(self, **fields: Any) -> None: ...
```

```python
# execution/broker_adapter/protocols.py — explicit Protocols extracted
# from the existing concrete classes

from datetime import datetime
from typing import Protocol

from alphamind.execution.broker_adapter.queries import (
    ActivitySnapshot, PositionSnapshot, TradeAccountSnapshot,
)
from alphamind.execution.corporate_actions.types import CorporateActionActivity


class AccountStateQueriesP(Protocol):
    async def get_account(self) -> TradeAccountSnapshot: ...
    async def get_positions(self) -> tuple[PositionSnapshot, ...]: ...
    async def get_activities_after(
        self, *, after: datetime,
    ) -> tuple[ActivitySnapshot, ...]: ...
    # Mirror the exact method set gather_phase1_inputs and dispatch_phase2
    # call — no broader; small Protocol surface (P9).


class CorporateActionsQueriesP(Protocol):
    async def fetch_unprocessed_ca_activities(
        self, *, position_lookups: dict[str, "PositionLookup"],
    ) -> tuple[CorporateActionActivity, ...]: ...
```

```python
# scheduler/debug_e2e/portfolio.py — pure data

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

from alphamind._kernel.ids import Symbol
from alphamind.portfolio_state.records.positions import (
    Direction, OptionContractType,
)


@dataclass(frozen=True, slots=True)
class SyntheticEquity:
    symbol: Symbol
    direction: Direction
    qty: float
    avg_cost: float
    sector: Literal["tech", "semis", "financials", "energy"]
    borrow_rate_pct: float | None = None  # short only


@dataclass(frozen=True, slots=True)
class SyntheticOption:
    underlying: Symbol
    contract_type: OptionContractType
    strike: float
    expiration_offset_days: int  # computed against now() at seed time
    contracts: float
    premium_per_contract: float
    sector: Literal["tech", "semis", "financials", "energy"]


@dataclass(frozen=True, slots=True)
class SyntheticStrategyLeg:
    contract_type: OptionContractType
    strike: float
    direction: Direction
    contracts: float


@dataclass(frozen=True, slots=True)
class SyntheticStrategy:
    underlying: Symbol
    expiration_offset_days: int
    legs: tuple[SyntheticStrategyLeg, ...]
    net_premium: float
    strategy_label: str
    sector: Literal["tech", "semis", "financials", "energy"]


@dataclass(frozen=True, slots=True)
class SyntheticThesis:
    position_index: int             # 0-based index into SYNTHETIC_PORTFOLIO.positions
    headline: str                   # one-sentence thesis
    rationale: str                  # 2-3 sentence elaboration


SyntheticPosition = SyntheticEquity | SyntheticOption | SyntheticStrategy


@dataclass(frozen=True, slots=True)
class SyntheticPortfolio:
    positions: tuple[SyntheticPosition, ...]
    theses: tuple[SyntheticThesis, ...]
    starting_cash_usd: float        # = $100k - sum(long market values) - sum(option premiums)


# Module-level constant — the canonical synthetic portfolio.
SYNTHETIC_PORTFOLIO = SyntheticPortfolio(
    positions=(
        SyntheticEquity(Symbol("NVDA"),  Direction.LONG,  qty=30,  avg_cost=620.0, sector="semis"),
        SyntheticEquity(Symbol("JPM"),   Direction.LONG,  qty=100, avg_cost=190.0, sector="financials"),
        SyntheticEquity(Symbol("GOOGL"), Direction.LONG,  qty=70,  avg_cost=200.0, sector="tech"),
        SyntheticEquity(Symbol("COP"),   Direction.LONG,  qty=130, avg_cost=115.0, sector="energy"),
        SyntheticEquity(Symbol("TSLA"),  Direction.SHORT, qty=30,  avg_cost=250.0, sector="tech",
                        borrow_rate_pct=1.5),
        SyntheticOption(Symbol("AAPL"),  OptionContractType.CALL, strike=230.0,
                        expiration_offset_days=270, contracts=3, premium_per_contract=14.50,
                        sector="tech"),
        SyntheticOption(Symbol("META"),  OptionContractType.PUT,  strike=480.0,
                        expiration_offset_days=90,  contracts=1, premium_per_contract=22.00,
                        sector="tech"),
        SyntheticStrategy(
            underlying=Symbol("MSFT"), expiration_offset_days=180,
            legs=(
                SyntheticStrategyLeg(OptionContractType.CALL, strike=440.0,
                                     direction=Direction.LONG,  contracts=2),
                SyntheticStrategyLeg(OptionContractType.CALL, strike=480.0,
                                     direction=Direction.SHORT, contracts=2),
            ),
            net_premium=12.30 * 2,  # net debit per spread × 2 spreads
            strategy_label="bull_call_spread", sector="tech",
        ),
    ),
    theses=(...),  # 8 SyntheticThesis records, one per position
    starting_cash_usd=24_440.0,  # plus $7,500 short-proceeds reserve, total cash $31,940
)
```

```python
# scheduler/debug_e2e/settings.py

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from alphamind.execution.broker_adapter.protocols import (
    AccountStateQueriesP, CorporateActionsQueriesP,
)
from alphamind.scheduler.progress import ProgressEmitter


@dataclass(frozen=True, slots=True)
class DebugE2ESettings:
    """All debug-e2e injections in one bundle.

    Presence of this object on RunInvocationContext is the single
    indicator that the orchestrator is in debug-e2e mode (P3 — no
    parallel boolean flag).
    """
    account_queries: AccountStateQueriesP
    ca_queries: CorporateActionsQueriesP
    emitter_factory: Callable[[str], ProgressEmitter]  # (invocation_id) -> emitter


def configure_debug_e2e(*, archive_root: Path) -> DebugE2ESettings:
    """Construct the debug-e2e injection bundle. Called once at CLI entry."""
    # Lazy imports so production callers never import debug_e2e at module-load
    from alphamind.scheduler.debug_e2e.broker import (
        LogOnlyAccountStateQueries, LogOnlyCorporateActionsQueries,
    )
    from alphamind.scheduler.debug_e2e.jsonl_emitter import JsonlProgressEmitter
    from alphamind.scheduler.debug_e2e.portfolio import SYNTHETIC_PORTFOLIO

    def make_emitter(invocation_id: str) -> ProgressEmitter:
        return JsonlProgressEmitter(
            path=archive_root / "invocations" / invocation_id / "progress.jsonl"
        )

    return DebugE2ESettings(
        account_queries=LogOnlyAccountStateQueries(SYNTHETIC_PORTFOLIO),
        ca_queries=LogOnlyCorporateActionsQueries(),
        emitter_factory=make_emitter,
    )
```

```python
# scheduler/run_context.py — single new field

@dataclass(frozen=True, slots=True)
class RunInvocationContext:
    ...  # existing fields
    debug_e2e: DebugE2ESettings | None = None
```

---

## 5. Testing seam

Four owned Protocols, four owned fakes / substitutes. The Protocol seam IS
the testing seam — what production uses for swapability, tests use for
substitution (P8 — fakes over mocks; don't mock what you don't own).

| Protocol | Production impl | Test substitute |
|---|---|---|
| `AccountStateQueriesP` | `AccountStateQueries` (existing, Alpaca-backed) | `LogOnlyAccountStateQueries` (debug_e2e; doubles as the test fake) |
| `CorporateActionsQueriesP` | `CorporateActionsQueries` (existing, Alpaca-backed) | `LogOnlyCorporateActionsQueries` (debug_e2e; doubles as the test fake) |
| `ProgressEmitter` | `NoOpProgressEmitter` | `RecordingProgressEmitter` — appends events to a list for assertion |
| seeder + DB | `wipe_and_seed(session, now)` | in-memory SQLite (existing pattern); assert on `positions` table contents post-seed |

```python
# tests/scheduler/test_progress.py

class RecordingProgressEmitter:
    """Test substitute — captures every event for assertion."""
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def phase_start(self, phase: str) -> None:
        self.events.append(("phase_start", {"phase": phase}))

    def phase_done(self, phase: str, **fields: Any) -> None:
        self.events.append(("phase_done", {"phase": phase, **fields}))

    def agent_request(self, **fields: Any) -> None:
        self.events.append(("agent_request", fields))

    def agent_response(self, **fields: Any) -> None:
        self.events.append(("agent_response", fields))
```

Integration test: drive `_run_once` with `--debug-e2e` against an in-memory
SQLite, assert that `progress.jsonl` contains all 13 phase events + 8 agent
request/response pairs in dependency order.

The monkey-patches on `phase1_inputs._build_account_state_queries` /
`_build_corporate_actions_queries` retire — tests that needed them now
inject queries via `RunInvocationContext.debug_e2e` or via a new
`account_queries_override` / `ca_queries_override` parameter on
`gather_phase1_inputs` (the orchestrator passes either the
`debug_e2e`-provided queries or the Alpaca-backed defaults).

---

## 6. Hardest-to-reverse decisions

### 6.1 Formalize broker adapter Protocols

**Picked.** Add `AccountStateQueriesP` and `CorporateActionsQueriesP`
Protocols to `execution/broker_adapter/protocols.py`. Change
`gather_phase1_inputs` to depend on the Protocols, not the concrete classes.

**Alternative.** Structural typing — let `LogOnlyAccountStateQueries`
satisfy the concrete `AccountStateQueries` interface via duck typing and
use `cast()` at the seam.

**Why.** P4 — the broker is exactly the kind of hexagonal port that wants
an explicit seam. The monkey-patch seams the tests use today
(`_build_account_state_queries`) are a smell that fall away naturally
once the Protocol exists. The Protocol surface is small (~3 methods on
each) so the extraction is cheap.

**Reconsider if.** The broker adapter is being rewritten — at which point
redo the Protocol against the new shape rather than retrofit.

### 6.2 Nest `debug_e2e/` under `scheduler/` (vs. top-level)

**Picked.** `src/alphamind/scheduler/debug_e2e/`.

**Alternative.** `src/alphamind/debug_e2e/` (top-level package).

**Why.** The flag's only consumer is the scheduler entry — nesting keeps
the geography honest. The import-linter contract that forbids production
code from importing `debug_e2e` is trivial to express when the package is
nested. Top-level placement would imply generality this feature doesn't
have.

**Reconsider if.** A second entry point (e.g., a `verify_*` CLI debug
command) wants the same machinery. At that point promote to top-level
`alphamind/debug_e2e/` and update the import-linter contract.

### 6.3 Wipe-everything seed vs. tagged-rows-only seed

**Picked.** Wipe-everything (per operator decision 2b). Every
`--debug-e2e` invocation deletes all rows from `positions`,
`position_theses`, `position_fills`, `brackets`, `bracket_legs`,
`cash_ledger`, `activity_log`, `invocations`, `process_lifetimes` —
then seeds fresh. **Guarded by a DB-path safety check**: the seeder
refuses to wipe unless the resolved DB path contains the substring
`debug` (recommended path: `data/alphamind-debug-e2e.db`).

**Alternative (a).** Seed-tagged-rows-only: insert with a sentinel
`origin="debug_e2e_seed"` field; wipe deletes only rows where
`origin LIKE 'debug_e2e_%'`. Pro: shares a DB safely with other data.
Con: many tables don't have an `origin` column; would require schema
changes.

**Alternative (b).** No safety check — trust the operator and the flag
name. Con: one mis-set `DATABASE_PATH` env var and we've wiped prod.

**Why.** Wipe-everything is the simplest path to full reproducibility
(decision 2b). The DB-path guard is the smallest backstop that makes the
operation safe — one substring check at seed entry, refuses with a clear
error message otherwise. The operator picks the dedicated DB path once;
the flag enforces it.

**Reconsider if.** Operators want to preserve some state across debug
invocations (e.g., to test multi-invocation behavior). At that point a
`--reseed-debug-portfolio` sub-flag splits "always wipe" into "wipe on
explicit demand only."

### 6.4 ProgressEmitter as Protocol + NoOp default (vs. optional emitter)

**Picked.** Protocol with NoOp default; call sites always emit.

**Alternative.** `emitter: ProgressEmitter | None = None`; call sites
guard with `if emitter is not None: emitter.event(...)`.

**Why.** P3 — `None` makes the type system carry a state ("emitter
absent") that propagates as guards through every call site. NoOp default
encodes the same intent in the type system once and the guards go away.
P9 — the Protocol is a tiny, deep interface; the depth lives in the two
implementations.

**Reconsider if.** Progress emission grows to need multiple sinks
(stdout + file + Datadog) — at which point a `CompositeProgressEmitter`
that fans out is a natural extension, still satisfying the Protocol.

### 6.5 Harness instrumentation: explicit parameter (vs. contextvar)

**Picked.** Each harness function takes `progress: ProgressEmitter` as
an explicit parameter, defaulting to `NoOpProgressEmitter()`. Orchestrator
threads the real emitter through the analysis + decision composition
runners.

**Alternative.** `progress.current` via `contextvars.ContextVar` —
entry point sets it; harnesses read it from context.

**Why.** P3 — explicit parameters keep the dependency visible in the
signature. ContextVar would hide a load-bearing input. The thread-through
cost is one extra kwarg in 8 harness functions + 2 composition runners.

**Reconsider if.** Harness count grows past ~20 and the thread-through
becomes a Liskov-style annoyance. Async contextvars are the natural
escape hatch.

---

## 7. Open decisions

- **Whether `--debug-e2e` implies `--once` or requires it.** Leaning
  implies (operator can't accidentally start a debug daemon). Decide at
  implementation time; trivial to swap.
- **Whether to add `--reseed-debug-portfolio` as a separate flag** to
  override "always wipe" with "skip wipe if seed marker present." Defer
  per operator decision 2b — add only when an actual workflow needs it.
- **Whether to seed the `options_chains` table with synthetic options
  data** matching the option positions' strikes. Today the system handles
  missing chain data by falling back to fixture greeks attached to the
  `PositionRecord` directly. Defer; revisit if the agent prompts surface
  "chain data missing" warnings noisily.
- **What gets included in `agent_response` event fields.** Minimum:
  `duration_s`, `input_tokens`, `output_tokens`. Possibly: `tool_calls`,
  `retries`, `stop_reason`. Defer to implementation — pick the smallest
  set that answers "is it stuck or working" + "is cost in budget."

---

## 8. Out of scope (explicit)

- **Real-broker dry-run mode.** This is "log-only broker AT THE SDK
  BOUNDARY" — no Alpaca contact whatsoever. A separate "submit but
  rollback" mode against real Alpaca paper is a different feature.
- **Production observability.** The `ProgressEmitter` Protocol is general
  enough to be reused later, but wiring `JsonlProgressEmitter` (or
  equivalents) into the production daemon is a separate decision.
- **Refactoring per-layer verify scripts.** The runbook reframing
  (already landed) recommends per-layer verifies retire as fallback-only
  diagnostics; `--debug-e2e` is orthogonal to that work. If the post-hoc
  invariant-check split lands later, the debug-e2e archive becomes its
  natural input.
