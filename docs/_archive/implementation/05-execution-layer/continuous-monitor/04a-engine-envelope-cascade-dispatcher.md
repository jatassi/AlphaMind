# 04a — Engine-envelope cascade dispatcher

## Goal

The `on_immediate_breach` callback the breach loop (story 03b) awaits. For each immediate-action `HARD_BLOCK` breach: select the position to close per the breach's deterministic selection rule, run the secondary-breach check, compose the engine envelope via `compose_engine_envelope`, and call `submit_engine_envelope` to persist the protective CLOSE. For cascades (margin call, primary + secondary breach), use the existing `orchestrate_breach_cascade` / `orchestrate_margin_call_cascade` primitives to chain envelopes under a shared `cascade_id`. Generates per-session monotonic trigger IDs so envelope IDs follow `MON.{session}.{trigger}` format.

## Reading

* `docs/design/06-risk-guardrails/breach-behavior.md` § Forced reduction policy, § Position selection logic, § Secondary breach checking, § Margin call cascade handling, § Traceability for engine-originated actions
* `docs/design/05-execution-layer/engine-envelope-schema.md` — full schema the produced envelopes satisfy
* `docs/design/05-execution-layer/oms-commands.md` § Command origins — engine-originated CLOSE constraints (`close_rationale_type`, `risk_management_subtype`)
* `docs/design/oms-command-ids.md` § Engine-originated command IDs — `MON.{session}.{trigger}.{ordinal}` format
* `src/alphamind/risk_guardrails/breach_behavior/__init__.py` — every primitive this story composes: `select_for_*`, `check_secondary_breach`, `orchestrate_breach_cascade`, `orchestrate_margin_call_cascade`, `search_for_alternate_position`, `generate_cascade_id`, `compose_engine_envelope`, `compose_guardrail_trigger_record`, `envelope_id_for`, `command_id_for`
* `src/alphamind/execution/oms/submit_engine_envelope.py` ([ALP-375](https://linear.app/alphamind-jatassi/issue/ALP-375/04-engine-envelope-submission-path-monitor-facing-write-function)) — `submit_engine_envelope(envelope, *, handle, retrieval_store=None, library_config, library_market, ...)`; happy path persists the CLOSE + activity-log entry; secondary `deferred_to_pm` paths reject
* `src/alphamind/execution/oms/engine_envelope.py` ([ALP-372](https://linear.app/alphamind-jatassi/issue/ALP-372/02a-engine-envelope-pydantic-models)) — `EngineEnvelope`, `GuardrailTriggerRecord`, `BreachDetails`, `SecondaryBreachCheckResult`
* `src/alphamind/execution/continuous_monitor/breach_loop/result.py` (story 03b) — `BreachLoopResult` + `RuleEvaluation` the callback receives
* Parent issue `ALP-123` § Pre-resolved decisions; § Notes for the orchestrator — invariants this story must respect (CLOSE-only, deterministic selection)

## Depends on

* [ALP-432](https://linear.app/alphamind-jatassi/issue/ALP-432/01-package-skeleton-monitor-entry-point-config-runtime-design-doc) (01) — supervisor + config
* [ALP-437](https://linear.app/alphamind-jatassi/issue/ALP-437/03b-breach-evaluation-loop) (03b) — `on_immediate_breach` callback the loop awaits

## Scope

Source under `src/alphamind/execution/continuous_monitor/cascade_dispatch/` (new sub-package). Tests at `tests/execution/continuous_monitor/cascade_dispatch/`.

### 1\. `TriggerIdGenerator` per-session counter

`src/alphamind/execution/continuous_monitor/cascade_dispatch/trigger_ids.py`:

```python
class TriggerIdGenerator:
    """Monotonically-increasing trigger ID generator scoped to a MonitorSession.

    The cascade dispatcher requests a fresh trigger ID per breach event; the
    same trigger ID is reused across the envelopes that share a cascade.
    """
    def __init__(self, *, session_id: str, start: int = 1) -> None: ...
    def next(self) -> int: ...
```

In-memory only; gapless within a session. A monitor restart starts at `1` (new session id makes the envelope IDs distinct).

### 2\. Per-rule selector dispatch table

`src/alphamind/execution/continuous_monitor/cascade_dispatch/selectors.py`:

```python
RULE_SELECTOR_DISPATCH: Mapping[str, PositionSelector] = {
    "daily_drawdown": select_for_drawdown_breach,
    "cumulative_drawdown": select_for_drawdown_breach,
    "per_position_max_loss": select_for_position_max_loss,
    "total_short_exposure": select_for_total_short_exposure_breach,
    "single_short_max_size": select_for_single_short_max_size_breach,
    "margin_call": select_for_margin_call,
}
```

Lookup function `def selector_for(rule_id: str) -> PositionSelector | None`. Returning `None` indicates a deferred rule that should not have arrived at this callback — the dispatcher raises a structural error.

### 3\. Dispatcher

`src/alphamind/execution/continuous_monitor/cascade_dispatch/dispatcher.py`:

```python
class CascadeDispatcher:
    def __init__(
        self,
        *,
        session: MonitorSession,
        config: ContinuousMonitorConfig,
        repository: PortfolioStateRepository,
        trigger_ids: TriggerIdGenerator,
        session_factory: Callable[[], AsyncSession],
    ) -> None: ...

    async def handle_immediate_breach(
        self, result: BreachLoopResult, rule: RuleEvaluation,
    ) -> None:
        """The on_immediate_breach callback signature.

        1. Resolve the selector for rule.rule_id.
        2. Run selector against the snapshot to pick the candidate position.
        3. Call check_secondary_breach(candidate_close, …) against the breach
           loop's LibraryConfig + MarketInputs. The check returns
           SecondaryBreachCheckResult with one of:
             - no_secondary_breach    → proceed with the candidate.
             - secondary_breach_avoided → use search_for_alternate_position
               to pick a different position; recurse the check.
             - deferred_to_pm         → log the deferral; do not submit.
        4. Compose the GuardrailTriggerRecord via
           compose_guardrail_trigger_record(rule_breached=rule.rule_id, …).
        5. Compose the EngineEnvelope via compose_engine_envelope(…).
        6. Open an AsyncSession + InvocationHandle stub appropriate for the
           monitor context; call submit_engine_envelope(envelope, handle=…).
        7. For margin-call breaches, orchestrate_margin_call_cascade(…); for
           breaches that trigger a cascade chain, orchestrate_breach_cascade(…).
           Both helpers return tuples of cascade-stage envelopes sharing
           generate_cascade_id(); the dispatcher submits each in order.
        """
```

The dispatcher does NOT call the LLM; it does NOT modify position state directly; it only constructs envelopes and hands them to `submit_engine_envelope`, which owns persistence.

### 4\. Supervisor wiring

Extend `__main__.py` so the breach loop's `on_immediate_breach` callback is now wired to `CascadeDispatcher.handle_immediate_breach` (replacing the no-op stub from story 03b). The same dispatcher instance is reused across cycles.

### 5\. Tests at `tests/execution/continuous_monitor/cascade_dispatch/`

* `test_trigger_ids.py` — monotonic + gapless within a session; resets at start of a fresh session.
* `test_selectors.py` — dispatch table covers every rule classified as IMMEDIATE in breach-behavior; lookup for a deferred rule returns `None`.
* `test_dispatcher.py` — using fakes for repository + `submit_engine_envelope`:
  * Happy path: `per_position_max_loss` breach selects the breaching position; secondary check returns `no_secondary_breach`; engine envelope is composed; `submit_engine_envelope` is called once with the envelope.
  * `secondary_breach_avoided`: dispatcher calls `search_for_alternate_position` and re-runs the check; eventually submits with the alternative.
  * `deferred_to_pm`: dispatcher logs and does NOT submit; activity-log entry records the deferral.
  * Margin-call cascade: `orchestrate_margin_call_cascade` returns N envelopes sharing a `cascade_id`; the dispatcher submits each in order.
  * Breach cascade: `orchestrate_breach_cascade` returns chained envelopes; same submission order discipline.
  * Structural error: handing the dispatcher a deferred-classification rule raises `ValueError`.
  * Trigger-ID monotonicity: 50 sequential breaches produce envelopes with strictly increasing `trigger_id` values.

### Out of scope

* Re-implementing any `select_for_*` selector — they live in `breach_behavior/position_selection.py`.
* Re-implementing `check_secondary_breach` or `orchestrate_*_cascade` — they live in `breach_behavior/`.
* Strategist visibility for the resulting closures — story 04c centralizes the projection.
* Real broker routing — `submit_engine_envelope` handles persistence; broker routing came online in [ALP-121](https://linear.app/alphamind-jatassi/issue/ALP-121/broker-adapter).

## Acceptance criteria

- [ ] `TriggerIdGenerator`, `CascadeDispatcher`, and `selector_for` are importable from `alphamind.execution.continuous_monitor.cascade_dispatch`.
- [ ] `CascadeDispatcher.handle_immediate_breach` satisfies the `on_immediate_breach: Callable[[BreachLoopResult, RuleEvaluation], Awaitable[None]]` signature consumed by story 03b's breach loop.
- [ ] For a `per_position_max_loss` immediate breach, the dispatcher selects the breaching position via `select_for_position_max_loss`, runs the secondary-breach check (returns `no_secondary_breach` in test fixture), composes a valid `EngineEnvelope`, and calls `submit_engine_envelope` exactly once.
- [ ] The composed envelope's `envelope_id` matches `^MON\.<session_id>\.<trigger_id>$`; the embedded CLOSE's `command_id` matches `^MON\.<session_id>\.<trigger_id>\.0$`.
- [ ] When `check_secondary_breach` returns `secondary_breach_avoided`, the dispatcher invokes `search_for_alternate_position` and recurses until either a clean candidate is found or the search exhausts.
- [ ] When `check_secondary_breach` returns `deferred_to_pm`, no envelope is submitted; an activity-log entry records the deferral with the rule + the candidate that was rejected.
- [ ] For a margin-call breach, `orchestrate_margin_call_cascade` is invoked; each returned envelope is submitted in order with the shared `cascade_id` preserved.
- [ ] For cascade-eligible breaches that produce multiple envelopes via `orchestrate_breach_cascade`, submission order matches the helper's returned order; each envelope's `cascade_id` matches the first envelope's.
- [ ] Handing the dispatcher a deferred-classification rule (e.g., `sector_concentration`) raises a structural error.
- [ ] `TriggerIdGenerator` yields strictly increasing trigger IDs starting at 1 within a session.
- [ ] Tests at `tests/execution/continuous_monitor/cascade_dispatch/` cover the criteria above and pass under `uv run pytest tests/execution/continuous_monitor/cascade_dispatch/ -n auto`.
- [ ] `uv run pytest -n auto` (full suite) passes.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` is clean.

## Verification

* `uv run pytest tests/execution/continuous_monitor/cascade_dispatch/ -n auto -v`.
* `uv run pytest -n auto` — full suite green.
* Manual smoke during story 05's e2e verification: drive a fixture portfolio into a `per_position_max_loss` `HARD_BLOCK`, observe the engine envelope persisting via `submit_engine_envelope`'s usual code path; query `activity_log` for the `POSITION_CLOSED` entry with `engine_guardrail` provenance.
* Lint clean per CLAUDE.md.