# 03e — Engine-stub coordinated swap

## Goal

Replace the synthetic-acknowledgment behavior in `src/alphamind/execution/oms/submit_envelope_mcp.py` (PM-originated envelopes) and `src/alphamind/execution/oms/submit_engine_envelope.py` (engine-originated envelopes) with real broker-adapter submission via the order-translation modules from stories 02b–e and the websocket fill-stream from 02f. Per <issue id="01ef5ee7-054d-41dc-bfca-be85dfce225c">ALP-120</issue> parent decision (C): broker routing was deferred to <issue id="e12a677b-a183-4f5d-bc48-5669337441c1">ALP-121</issue>, and this story fulfills the deferral. After this swap, the OMS write paths produce real Alpaca paper-mode acknowledgments instead of stub ones; gateway-submission failures map to the existing `command_abandoned` activity-log path; permanent rejections surface to the PM as synchronous OMS rejections.

This is a **coordinated edit** to existing OMS write-path code (already shipped under <issue id="01ef5ee7-054d-41dc-bfca-be85dfce225c">ALP-120</issue>). The orchestrator must run the OMS test suite after integration to catch regressions.

## Reading

* `docs/design/05-execution-layer/oms-commands.md` § Command processing model § Sequencing within an invocation, § Rejection handling — the synchronous-rejection contract this swap preserves.
* `docs/design/05-execution-layer/state-persistence.md` § Phase 2 write path — submission retry window, command_abandoned policy, rolled-back transaction on retry-window exhaustion.
* `src/alphamind/execution/oms/submit_envelope_mcp.py` — the existing engine-stub PM path. Identify the synthetic-ack production sites this story replaces.
* `src/alphamind/execution/oms/submit_engine_envelope.py` — the existing engine-stub engine-originated path. Same sites to replace.
* `src/alphamind/execution/oms/command_models.py` — canonical `OMSCommand` types this swap dispatches on.
* `src/alphamind/execution/state_persistence/write_paths/phase2.py` — the Phase 2 transaction surface; understand how the existing engine-stub plugs into it so the swap preserves the integration shape.
* `src/alphamind/execution/broker_adapter/order_equity.py` (story 02b) — `submit_equity_open / _add / _close`.
* `src/alphamind/execution/broker_adapter/order_options.py` (story 02c) — `submit_options_open / _add / _close`.
* `src/alphamind/execution/broker_adapter/order_mleg.py` (story 02d) — `submit_mleg_open / _add / _close`.
* `src/alphamind/execution/broker_adapter/order_modify.py` (story 02e) — `submit_replace`, `submit_cancel`.
* `src/alphamind/execution/broker_adapter/fill_stream.py` (story 02f) — `FillReport` shape (not used by this story directly, but informs the surface the OMS sees).
* <issue id="e12a677b-a183-4f5d-bc48-5669337441c1">ALP-121</issue> parent body § Pre-resolved configuration decisions (H).

## Depends on

* <issue id="fcaca117-ada1-4d79-b425-5ec2bfe9cebe">ALP-380</issue> (02b — Equity order POST translation).
* <issue id="d594062b-9846-4b02-b795-73d915321492">ALP-381</issue> (02c — Options order POST translation).
* <issue id="39e94c89-6c5c-447c-83ef-de6cd9cac322">ALP-382</issue> (02d — Multi-leg mleg order POST translation).
* <issue id="beb593e1-505a-417a-8456-51c70524e206">ALP-383</issue> (02e — Order PATCH + DELETE translation).
* <issue id="7690cddb-d84d-4a0e-bc52-e7942f0480c4">ALP-384</issue> (02f — trade_updates fill-stream subscriber + fill-report translator).

## Scope

Source under `src/alphamind/execution/oms/submit_envelope_mcp.py` and `src/alphamind/execution/oms/submit_engine_envelope.py` (existing files — modify). Tests at `tests/execution/oms/test_submit_envelope_mcp.py` and `tests/execution/oms/test_submit_engine_envelope.py` (existing — extend with paper-mode-broker-routing scenarios).

### 1. Dispatcher — canonical command → broker translation function

A new helper module: `src/alphamind/execution/oms/broker_dispatch.py`. Maps each canonical `OMSCommand` variant to the right `submit_*_*` function from the broker_adapter package:

```python
from alpaca.trading.client import TradingClient

from alphamind.config.models.execution import ExecutionConfig
from alphamind.execution.broker_adapter import (
    AccountStateQueries,
    EquitySubmission,
    GatewaySubmissionFailed,
    MLEGSubmission,
    OptionsSubmission,
    Submitted,
    SubmissionOutcome,
    submit_equity_add,
    submit_equity_close,
    submit_equity_open,
    submit_mleg_add,
    submit_mleg_close,
    submit_mleg_open,
    submit_options_add,
    submit_options_close,
    submit_options_open,
)
from alphamind.execution.oms.command_models import (
    AddCommand,
    AdjustCommand,
    CancelCommand,
    CloseCommand,
    EquityInstrument,
    OMSCommand,
    OpenCommand,
    OptionInstrument,
    StrategyInstrument,
)


@dataclass(frozen=True)
class BrokerDispatchResult:
    """Unified result type the OMS persists onto OrderRecord."""

    alpaca_order_id: str
    client_order_id: str
    status: str
    order_class: str
    payload_kind: Literal["equity", "options", "mleg"]
    raw_submission: Any  # the EquitySubmission / OptionsSubmission / MLEGSubmission


async def dispatch_command_to_broker(
    command: OMSCommand,
    *,
    client: TradingClient,
    queries: AccountStateQueries,
    execution: ExecutionConfig,
    client_order_id: str,
    # Threaded from portfolio state at the call site for CLOSE / ADD / mleg-close:
    position_qty: float | None = None,
    position_side: Literal["long", "short"] | None = None,
    occ_symbol: str | None = None,
    open_legs: tuple[MLEGLegAck, ...] | None = None,
) -> SubmissionOutcome[BrokerDispatchResult]:
    """Route the command to the right broker submission function.

    Dispatches on (command_type, instrument.asset_type) for OPEN/ADD/CLOSE.
    AdjustCommand routes to submit_replace; CancelCommand routes to
    submit_cancel — both target an existing alpaca_order_id (which the
    caller threads from the order's record).
    """
    ...
```

### 2. PM-originated swap

In `submit_envelope_mcp.py`:

* Replace each synthetic-ack production site with a call to `dispatch_command_to_broker(...)`.
* The validator runs first (existing behavior); only commands that pass validation reach the dispatcher.
* On `Submitted[BrokerDispatchResult]`: persist the broker-side `alpaca_order_id` onto the `OrderRecord` Phase 2 produces; activity log carries the dispatch result.
* On `GatewaySubmissionFailed`: roll back the Phase 2 transaction (existing behavior); write a `command_abandoned` activity-log entry with the broker's failure reason.
* On `PermanentRejection` raise (re-raised from the broker translator): map to the existing synchronous-rejection payload format the PM tool consumes, with the `PermanentRejection.code` becoming the rejection's `gateway_reason` field.

The factory function `build_submit_envelope_mcp_server` gets two new parameters: `client: TradingClient` and `queries: AccountStateQueries`. The caller (story 05's verify script + the future pipeline composition wiring) supplies them.

### 3. Engine-originated swap

In `submit_engine_envelope.py`: identical pattern. The engine-originated CLOSE always routes through `dispatch_command_to_broker` with `command_type=CLOSE` and `risk_management_subtype=engine_guardrail`. The pre-broker validation (already shipped) runs first; only validated CLOSEs reach the dispatcher.

### 4. Test extensions

Existing tests (`tests/execution/oms/test_submit_envelope_mcp.py`, `tests/execution/oms/test_submit_engine_envelope.py`) need to be extended to cover the broker-routing paths. New scenarios:

* OPEN command for equity reaches `submit_equity_open`; the resulting `BrokerDispatchResult` is persisted onto the `OrderRecord`.
* OPEN command for options reaches `submit_options_open`.
* OPEN command for strategy reaches `submit_mleg_open`.
* CLOSE command (PM-originated) routes to the appropriate close function based on instrument type.
* CLOSE command (engine-originated) routes the same way; `risk_management_subtype` flows through.
* ADD command for each instrument type.
* ADJUST command routes to `submit_replace` against the order's `alpaca_order_id`.
* CANCEL command routes to `submit_cancel`.
* Gateway-submission failure: Phase 2 rolls back; `command_abandoned` activity-log entry written.
* Permanent rejection: synchronous rejection payload returned to the PM tool.

The existing tests that asserted synthetic acks need to update — the new behavior is real broker submission. Use a `responses`-mocked alpaca-py client (matching story 02a's test pattern) for offline test runs.

### 5. RUNBOOK_oms_commands.md update

Add a section noting that as of this work tree, the engine-stub now routes to real broker submission in paper mode. Include operator instructions for running `verify_oms_commands.py` against a fresh DB, expecting real Alpaca paper acknowledgments.

### Out of scope

* Live-mode submission — story 05's e2e verify exercises paper mode only; live mode swap follows the same code path with a different `execution_mode` config.
* Phase 1 fill-integration extensions (covered by stories 03c + 04b).
* Continuous-monitor wiring — <issue id="734df060-3ba0-4514-8e97-bfc009e6b677">ALP-123</issue> work tree consumes this swap when it lands.
* Decision-layer pipeline composition (the runner that threads PM envelope → submit_envelope_mcp) — separate Substantial entry under "Decision-layer pipeline composition wiring" in `docs/project-tracker.md`.

## Acceptance criteria

- [ ] `src/alphamind/execution/oms/broker_dispatch.py` exists with `dispatch_command_to_broker` and `BrokerDispatchResult`.
- [ ] `dispatch_command_to_broker` for an `OpenCommand` carrying an `EquityInstrument` calls `submit_equity_open`.
- [ ] `dispatch_command_to_broker` for an `OpenCommand` carrying an `OptionInstrument` calls `submit_options_open`.
- [ ] `dispatch_command_to_broker` for an `OpenCommand` carrying a `StrategyInstrument` calls `submit_mleg_open`.
- [ ] Same dispatch table for `AddCommand` and `CloseCommand`.
- [ ] `dispatch_command_to_broker` for an `AdjustCommand` routes to `submit_replace` with the threaded `target_alpaca_order_id`.
- [ ] `dispatch_command_to_broker` for a `CancelCommand` routes to `submit_cancel`.
- [ ] `submit_envelope_mcp.py` no longer produces synthetic acknowledgments for accepted commands; it routes through `dispatch_command_to_broker`.
- [ ] `submit_engine_envelope.py` no longer produces synthetic acknowledgments; routes through the dispatcher with `risk_management_subtype=engine_guardrail`.
- [ ] On a successful dispatch, the OMS persists the real Alpaca `alpaca_order_id` onto the `OrderRecord`; `alpaca_order_id_chain` is `(alpaca_order_id,)`.
- [ ] On `GatewaySubmissionFailed`: Phase 2 transaction rolls back; a `command_abandoned` activity-log entry is written with the failure reason and the originating envelope ID.
- [ ] On `PermanentRejection` (synchronous OMS rejection): the PM tool's response includes the `PermanentRejection.code` in `gateway_reason`; `guardrail_rejection`-style entry written.
- [ ] PM envelope → broker submission preserves the existing per-command sequencing within an invocation (cumulative state, conflicting-command detection — existing tests should still pass).
- [ ] All existing OMS tests in `tests/execution/oms/` pass after the swap.
- [ ] New tests cover each instrument type × command type matrix, plus the failure-path scenarios.
- [ ] `RUNBOOK_oms_commands.md` updated with broker-routing notes.
- [ ] `uv run pytest tests/execution/oms/ tests/execution/state_persistence/ -n auto` is green.
- [ ] `uv run pytest -n auto` is green (full suite).
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` is clean.

## Verification

* Run `uv run pytest tests/execution/oms/ tests/execution/state_persistence/ -n auto -v` — green.
* Run `uv run pytest -n auto` — full suite green; no regressions in the existing OMS / state-persistence / decision-layer / portfolio_state / risk_guardrails tests.
* Spot-check by constructing a synthetic PM envelope with an OPEN equity command, calling the MCP tool, asserting the result carries an Alpaca-style order ID (UUID format) rather than a synthetic stub ID.
* Lint clean per CLAUDE.md.
