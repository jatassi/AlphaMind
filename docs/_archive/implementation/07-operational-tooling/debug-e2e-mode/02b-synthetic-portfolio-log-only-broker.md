# 02b — Synthetic portfolio + log-only broker

## Goal

Implement `scheduler/debug_e2e/portfolio.py` (pure data — `Synthetic*` dataclasses + module-level `SYNTHETIC_PORTFOLIO` constant) and `scheduler/debug_e2e/broker.py` (`LogOnlyAccountStateQueries` + `LogOnlyCorporateActionsQueries` satisfying the Protocols from story 01a). Each log-only query method logs the call and returns a deterministic view derived from `SYNTHETIC_PORTFOLIO` — no Alpaca contact.

## Reading

* `docs/design/debug-e2e-mode.md` §§ 4 (Types — [portfolio.py](<http://portfolio.py>) data shape and the canonical `SYNTHETIC_PORTFOLIO` literal), 5 (Testing seam — [broker.py](<http://broker.py>) doubles as the test fake)
* `src/alphamind/_kernel/ids.py` — `Symbol` NewType the synthetic portfolio uses
* `src/alphamind/portfolio_state/records/positions.py` — `Direction`, `OptionContractType` enums
* `src/alphamind/execution/broker_adapter/queries.py` — `PositionSnapshot`, `TradeAccountSnapshot`, `ActivitySnapshot` shapes the log-only broker returns
* `ALP-494` (01a) — provides `AccountStateQueriesP` / `CorporateActionsQueriesP` Protocols this story satisfies

## Depends on

* `ALP-494` (01a — Broker-adapter Protocols + phase1_inputs refactor)

## Scope

In scope: `src/alphamind/scheduler/debug_e2e/portfolio.py` (new) and `src/alphamind/scheduler/debug_e2e/broker.py` (new). Tests at `tests/scheduler/debug_e2e/test_portfolio.py` and `test_broker.py`.

### 1\. `scheduler/debug_e2e/portfolio.py` — pure data

Five frozen-dataclass shapes from design § 4 (`SyntheticEquity`, `SyntheticOption`, `SyntheticStrategyLeg`, `SyntheticStrategy`, `SyntheticThesis`, `SyntheticPortfolio`), the `SyntheticPosition` type alias (the union `SyntheticEquity | SyntheticOption | SyntheticStrategy`), and the module-level `SYNTHETIC_PORTFOLIO` constant matching the literal in the design doc:

* 5 equities: NVDA long 30 @ 620, JPM long 100 @ 190, GOOGL long 70 @ 200, COP long 130 @ 115, TSLA short 30 @ 250 (borrow 1.5%)
* 2 single-leg options: AAPL CALL 230 / +270d / 3 contracts / $14.50, META PUT 480 / +90d / 1 contract / $22.00
* 1 strategy: MSFT bull call spread, +180d, long 2x 440 + short 2x 480 / net debit 12.30 × 2 = 24.60
* 8 theses (one per position, indexed by `position_index`)
* `starting_cash_usd = 24_440.0`

### 2\. `scheduler/debug_e2e/broker.py` — log-only queries

```python
import logging
from datetime import date

from alphamind.execution.broker_adapter.queries import (
    ActivitySnapshot, PositionSnapshot, TradeAccountSnapshot,
)
from alphamind.execution.corporate_actions.types import CorporateActionActivity
from alphamind.scheduler.debug_e2e.portfolio import SyntheticPortfolio

log = logging.getLogger(__name__)


class LogOnlyAccountStateQueries:
    """Log-only AccountStateQueriesP implementation.

    Returns deterministic snapshots derived from SYNTHETIC_PORTFOLIO; logs
    every call at INFO with a [debug_e2e] prefix.
    """

    def __init__(self, portfolio: SyntheticPortfolio) -> None:
        self._portfolio = portfolio

    def get_account(self) -> TradeAccountSnapshot:
        log.info("[debug_e2e] LogOnlyAccountStateQueries.get_account()")
        # Compute cash / portfolio_value / equity from portfolio.
        ...

    def get_positions(self) -> tuple[PositionSnapshot, ...]:
        log.info("[debug_e2e] LogOnlyAccountStateQueries.get_positions()")
        # Translate the 8 synthetic positions into PositionSnapshot tuples.
        ...


class LogOnlyCorporateActionsQueries:
    """Log-only CorporateActionsQueriesP implementation.

    Returns empty tuple — no CAs on the synthetic portfolio. Logs every call.
    """

    async def get_corporate_actions(
        self, *, start_date: date, end_date: date, symbols,
    ) -> tuple[CorporateActionActivity, ...]:
        log.info(
            "[debug_e2e] LogOnlyCorporateActionsQueries.get_corporate_actions("
            "start=%s end=%s symbols=%d)",
            start_date, end_date, len(tuple(symbols)),
        )
        return ()
```

Both classes satisfy the Protocols from story 01a structurally — no inheritance from the concrete production classes. Match the exact method signatures defined on `AccountStateQueriesP` / `CorporateActionsQueriesP` in `execution/broker_adapter/protocols.py`.

### Out of scope

* The seeder that writes the portfolio to the DB (story 01c).
* The `configure_debug_e2e` bundle wiring (story 03).
* Seeding `options_chains` for the option positions' greeks — deferred per parent Issue Pre-resolved decision § (H).

## Acceptance criteria

- [ ] `src/alphamind/scheduler/debug_e2e/portfolio.py` exposes `SyntheticEquity`, `SyntheticOption`, `SyntheticStrategyLeg`, `SyntheticStrategy`, `SyntheticThesis`, `SyntheticPosition` type alias, `SyntheticPortfolio` frozen-dataclasses and the module-level `SYNTHETIC_PORTFOLIO` constant.
- [ ] `SYNTHETIC_PORTFOLIO` has 5 equities (NVDA long, JPM long, GOOGL long, COP long, TSLA short with `borrow_rate_pct=1.5`), 2 single-leg options (AAPL call, META put), 1 strategy (MSFT bull call spread) — matching design § 4.
- [ ] `SYNTHETIC_PORTFOLIO.starting_cash_usd == 24_440.0` and `len(SYNTHETIC_PORTFOLIO.positions) == 8` and `len(SYNTHETIC_PORTFOLIO.theses) == 8`.
- [ ] All `Synthetic*` dataclasses are `@dataclass(frozen=True, slots=True)`.
- [ ] `src/alphamind/scheduler/debug_e2e/broker.py` exposes `LogOnlyAccountStateQueries(portfolio)` and `LogOnlyCorporateActionsQueries()` satisfying `AccountStateQueriesP` / `CorporateActionsQueriesP` structurally (verified by `isinstance` check in tests).
- [ ] `LogOnlyAccountStateQueries(SYNTHETIC_PORTFOLIO).get_positions()` returns a tuple of `PositionSnapshot` records, one per synthetic position.
- [ ] `LogOnlyAccountStateQueries(SYNTHETIC_PORTFOLIO).get_account()` returns a `TradeAccountSnapshot` with cash / portfolio_value / equity derived from the portfolio.
- [ ] `LogOnlyCorporateActionsQueries().get_corporate_actions(start_date=..., end_date=..., symbols=...)` returns `()` (empty tuple).
- [ ] Each log-only method emits one INFO log line prefixed with `[debug_e2e]` capturing the method name and key call args.
- [ ] `tests/scheduler/debug_e2e/test_portfolio.py` covers the dataclass invariants (frozen, slots, position/thesis count, starting cash).
- [ ] `tests/scheduler/debug_e2e/test_broker.py` covers the Protocol-satisfaction check + the return shape from each method.
- [ ] `uv run pytest tests/scheduler/debug_e2e/ -n auto` passes; full linter chain clean.

## Verification

`uv run pytest tests/scheduler/debug_e2e/test_portfolio.py tests/scheduler/debug_e2e/test_broker.py -n auto` passes. Linter chain clean.