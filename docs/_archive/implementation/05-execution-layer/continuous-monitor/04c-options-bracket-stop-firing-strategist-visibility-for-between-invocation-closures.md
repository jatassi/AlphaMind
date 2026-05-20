# 04c — Options bracket-stop firing + strategist visibility

## Goal

Two cohesive deliverables.

**(1) Options bracket-stop firing.** A per-position watcher that, for every open option / strategy position with a price-based or P/L-based bracket leg, evaluates the trigger condition on every cache update (story 02b's quote stream) and on every greeks refresh (story 03a's task). On trigger fire, submit a closing market order via the broker adapter directly — *not* wrapped as an engine envelope, because these are thesis-driven exits, not guardrail-driven CLOSEs per parent decision (I). Persist `POSITION_CLOSED` activity-log entries with `event_source=BRACKET_MANAGER`, `exit_method=STOP_TRIGGERED` (price-based invalidation) or `TARGET_REACHED` (P/L-based target). Strategy positions submit a fresh `mleg` closing order (per-leg market orders if Alpaca rejects the combined close).

**(2) Strategist visibility.** Extend `StrategistView` with a typed `between_invocation_closures` projection so the strategist's next invocation sees monitor-fired closures prominently. The projection filters `intra_invocation_changelog` for events with `event_source ∈ {BRACKET_MANAGER, MARGIN_MONITOR, GUARDRAIL_LAYER}` or `engine_guardrail` provenance — covering both this story's direct-broker-call closures AND story 04a's engine-envelope cascade closures. Per parent decision (I), the operator needs these closures obvious to the strategist at the next invocation.

## Reading

* `docs/design/05-execution-layer/architecture.md` § 4e Options bracket-stop evaluation and P/L-target firing — price-based vs. P/L-based vs. strategy-position semantics
* `docs/design/05-execution-layer/orders-and-brackets.md` § Options price-based stops: trigger on the underlying, § P/L-based bracket legs: anchor to actual fill price
* `docs/design/05-execution-layer/broker-adapter.md` § Known gaps relative to AlphaMind's order vocabulary § Brackets on options — why the monitor owns this
* `src/alphamind/portfolio_state/records/positions.py` — `OptionsPositionDetails`, `StrategyPositionDetails`; check what fields describe the bracket legs (price level, P/L threshold)
* `src/alphamind/portfolio_state/records/orders.py` or equivalent — `BracketRecord` model with bracket leg types (`price_based_invalidation`, `pl_based_target`, `pl_based_stop`)
* `src/alphamind/execution/broker_adapter/` — order-submission entry points (the direct-call path this story uses)
* `src/alphamind/execution/continuous_monitor/underlying_stream/` (story 02b) — `UnderlyingPriceCache` reader
* `src/alphamind/execution/continuous_monitor/greeks_refresh/recompute.py` (story 03a) — `recompute_greeks` helper this story reuses to derive option price from greeks + spot + IV
* `src/alphamind/portfolio_state/events/activity_log.py` — `EventType.POSITION_CLOSED` + `PositionClosedDetail` (with `exit_method: PositionExitMethod`); `PositionExitMethod.STOP_TRIGGERED` and `TARGET_REACHED` already exist
* `src/alphamind/portfolio_state/consumers/strategist.py` — `project_strategist_view` and `StrategistView`; the file extends with a new typed projection
* Parent issue `ALP-123` § Pre-resolved decision (I) — direct-call path; strategist projection covers BOTH 04a and 04c closures

## Depends on

* [ALP-432](https://linear.app/alphamind-jatassi/issue/ALP-432/01-package-skeleton-monitor-entry-point-config-runtime-design-doc) (01) — supervisor + config
* [ALP-434](https://linear.app/alphamind-jatassi/issue/ALP-434/02b-live-underlying-price-stream-task-alpaca-stockdatastream-iex) (02b) — `UnderlyingPriceCache` for the trigger evaluation
* [ALP-436](https://linear.app/alphamind-jatassi/issue/ALP-436/03a-greeks-refresh-task-scheduled-move-triggered) (03a) — fresh greeks + the recompute helper for P/L-target derived pricing

## Scope

Source under `src/alphamind/execution/continuous_monitor/bracket_stops/` (new sub-package) for the firing logic, and `src/alphamind/portfolio_state/consumers/strategist.py` (extend) for the visibility projection. Tests at `tests/execution/continuous_monitor/bracket_stops/` and `tests/portfolio_state/consumers/test_strategist.py` (extend).

### Part A — Options bracket-stop firing

#### A.1 Per-bracket trigger evaluators

`src/alphamind/execution/continuous_monitor/bracket_stops/triggers.py`:

```python
def evaluate_price_based_trigger(
    *, position: PositionRecord, bracket: BracketRecord, spot: float,
) -> bool:
    """For options/strategy positions with a price-based invalidation leg,
    return True when the underlying spot has crossed the stop level."""

def evaluate_pl_target_trigger(
    *, position: PositionRecord, bracket: BracketRecord, spot: float,
    iv_provider: IvProvider, risk_free_rate: float, as_of: datetime,
) -> bool:
    """For P/L-based take-profit or take-loss legs on options/strategy
    positions, derive the option's current price via the guardrail-evaluation
    Black-Scholes core, compare against the P/L threshold (with the
    configurable derivation-uncertainty buffer). Return True when fired."""
```

Pure synchronous; no I/O.

#### A.2 Closing-order submission

`src/alphamind/execution/continuous_monitor/bracket_stops/closer.py`:

```python
async def submit_options_bracket_close(
    *,
    position: PositionRecord,
    bracket: BracketRecord,
    trigger_reason: PositionExitMethod,  # STOP_TRIGGERED or TARGET_REACHED
    broker_adapter: BrokerAdapter,
    session_factory: Callable[[], AsyncSession],
) -> None:
    """Submit a closing market order via the broker adapter:
    * Single-leg options: market order on the option's OCC symbol.
    * Strategy: fresh mleg closing order; on Alpaca rejection of the combined
      close, fall back to per-leg market orders (broker_adapter.options_close).
    Write a POSITION_CLOSED activity-log entry with event_source=BRACKET_MANAGER,
    exit_method=trigger_reason, and the closing order ID(s).
    """
```

#### A.3 Watcher task

`src/alphamind/execution/continuous_monitor/bracket_stops/task.py`:

```python
async def run_options_bracket_watcher(
    session: MonitorSession,
    config: ContinuousMonitorConfig,
    *,
    repository: PortfolioStateRepository,
    cache: UnderlyingPriceCache,
    broker_adapter: BrokerAdapter,
    session_factory: Callable[[], AsyncSession],
    risk_free_rate_provider: Callable[[], float],
) -> None:
    """Run-forever task. Every loop iteration (configurable; default 1 s):

    1. Snapshot open option + strategy positions with their bracket legs.
    2. For each price-based invalidation leg: read spot from cache; run
       evaluate_price_based_trigger; on True, submit close.
    3. For each P/L-based leg: read spot from cache + iv_provider from the
       position's greeks (refreshed by story 03a); run evaluate_pl_target_trigger;
       on True, submit close.
    4. Track per-bracket "already-fired" state so each leg fires once.
    """
```

Add `bracket_stop_evaluation_cadence_seconds: float = 1.0` to `ContinuousMonitorConfig`.

#### A.4 Supervisor registration

Register the watcher task as `"bracket_stops"`.

### Part B — Strategist visibility projection

#### B.1 Typed projection record

`src/alphamind/portfolio_state/consumers/strategist.py` — append:

```python
@dataclass(frozen=True, slots=True)
class BetweenInvocationClosure:
    position_id: str
    ticker: str
    instrument_type: InstrumentType  # equity / options / strategy
    closed_at: datetime
    closing_order_id: str | None  # None for engine-envelope cascade closures (they don't carry an order_id at the activity-log level)
    exit_method: PositionExitMethod  # STOP_TRIGGERED / TARGET_REACHED / MARGIN_LIQUIDATION / engine_guardrail-derived
    origin: Literal["bracket_manager", "margin_monitor", "guardrail_layer", "engine_guardrail"]
    rationale: str  # short; e.g., "price-based stop fired: NVDA underlying $848 < $865 trigger"
```

#### B.2 Project function

```python
def _project_between_invocation_closures(
    snapshot: PortfolioStateSnapshot,
) -> tuple[BetweenInvocationClosure, ...]:
    """Filter intra_invocation_changelog for POSITION_CLOSED events with
    event_source ∈ {BRACKET_MANAGER, MARGIN_MONITOR, GUARDRAIL_LAYER} or
    where the linked envelope has source_provenance='engine_guardrail'.
    """
```

#### B.3 Extend `StrategistView`

```python
@dataclass(frozen=True, slots=True)
class StrategistView:
    # existing fields ...
    between_invocation_closures: tuple[BetweenInvocationClosure, ...]
```

Append to the existing field list. Update `project_strategist_view` to populate the new field via `_project_between_invocation_closures(snapshot)`.

#### B.4 Strategist render template

If the strategist's input-bundle render template (likely `src/alphamind/decision/strategist/input_bundle.py` or similar) consumes `StrategistView`, add a section "Recent between-invocation closures" listing the new field. Style: short header + per-closure line with `ticker / exit_method / rationale / closed_at`. If no template change is needed (e.g., the rendering iterates all view fields automatically), document why in the test.

#### B.5 Tests

* `tests/portfolio_state/consumers/test_strategist.py` (extend):
  * BRACKET_MANAGER-sourced POSITION_CLOSED in the changelog → projected as `BetweenInvocationClosure(origin="bracket_manager", exit_method=STOP_TRIGGERED, ...)`.
  * Engine-envelope cascade closure (provenance `engine_guardrail`) → projected as `BetweenInvocationClosure(origin="engine_guardrail", exit_method=PM_DECISION-derived, ...)`.
  * Mix of both → projection contains both in chronological order.
  * Empty changelog → empty tuple.
* `tests/execution/continuous_monitor/bracket_stops/test_triggers.py` — price-based + P/L-based evaluators with fixture brackets.
* `tests/execution/continuous_monitor/bracket_stops/test_closer.py` — single-leg options close, strategy mleg close, per-leg fallback on synthetic Alpaca rejection.
* `tests/execution/continuous_monitor/bracket_stops/test_task.py` — watcher loop; bracket fires once per leg; activity-log entries persist.

### Out of scope

* Time-based invalidation (covered by the existing time-bracket primitive elsewhere; not in this story's scope).
* Event-based invalidation (soft, PM-evaluated; not enforced by the monitor).
* Greek-driven max-loss guardrails (handled by story 03b's breach loop via `per_position_max_loss`).
* PM-side rendering — `_render_recent_engine_actions_block` already exists; no changes there.
* Modifying the bracket model itself — the leg-shape comes from existing records.

## Acceptance criteria

### Part A — bracket-stop firing

- [ ] `evaluate_price_based_trigger` returns True when `spot ≤ stop_price` (long position) or `spot ≥ stop_price` (short position) — matching the design's directional semantics.
- [ ] `evaluate_pl_target_trigger` computes derived option price via the guardrail-evaluation Black-Scholes core, compares against the P/L threshold (in absolute price terms, anchored to actual fill price per `orders-and-brackets.md`), and applies the configurable derivation-uncertainty buffer.
- [ ] `submit_options_bracket_close` submits a market close on the OCC symbol for single-leg options; submits a fresh `mleg` close for strategy positions; falls back to per-leg market orders on Alpaca rejection of the combined close.
- [ ] Each fired close persists a `POSITION_CLOSED` activity-log entry with `event_source=BRACKET_MANAGER`, `exit_method ∈ {STOP_TRIGGERED, TARGET_REACHED}`, and the closing order ID(s).
- [ ] `run_options_bracket_watcher` tracks per-bracket-leg already-fired state so each leg fires at most once per session.
- [ ] `bracket_stop_evaluation_cadence_seconds` is added to `ContinuousMonitorConfig` with default 1.0 and a positive-float validator.

### Part B — strategist visibility

- [ ] `BetweenInvocationClosure` is importable from `alphamind.portfolio_state.consumers.strategist`.
- [ ] `StrategistView.between_invocation_closures` is populated by `project_strategist_view`; the field is a `tuple[BetweenInvocationClosure, ...]` ordered chronologically.
- [ ] BRACKET_MANAGER-sourced `POSITION_CLOSED` entries appear in the projection with `origin="bracket_manager"` and the matching `exit_method`.
- [ ] Engine-envelope cascade closures (provenance `engine_guardrail`) appear in the projection with `origin="engine_guardrail"`.
- [ ] If a strategist input-bundle render template renders `StrategistView`, the new field is included in the rendered output as a "Recent between-invocation closures" section. If no template change is needed, documented in the test.
- [ ] Existing strategist tests continue to pass — the additive field does not break fixture builders.

### Cross-cutting

- [ ] Tests at `tests/execution/continuous_monitor/bracket_stops/` AND the extended `tests/portfolio_state/consumers/test_strategist.py` pass under `uv run pytest -n auto`.
- [ ] `uv run pytest -n auto` (full suite) passes.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` is clean.

## Verification

* `uv run pytest tests/execution/continuous_monitor/bracket_stops/ tests/portfolio_state/consumers/ -n auto -v`.
* `uv run pytest -n auto` — full suite green.
* Manual smoke during story 05's e2e verification: open a fixture options position with a price-based stop above current spot; drive the underlying-price cache to trip the stop; observe the closing order submitted and the `POSITION_CLOSED` activity-log entry; then run a synthetic next-invocation render of the strategist view and confirm the closure appears in the `between_invocation_closures` section.
* Lint clean per CLAUDE.md.