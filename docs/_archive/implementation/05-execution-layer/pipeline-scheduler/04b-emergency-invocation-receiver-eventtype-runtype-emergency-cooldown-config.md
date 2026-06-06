# 04b — Emergency-invocation receiver + `EventType.EMERGENCY_INVOCATION_REQUESTED` + `RunType.emergency` + `emergency.yaml` + cooldown config

## Goal

Ship the async task that polls `activity_log` for new `EMERGENCY_INVOCATION_REQUESTED` entries (written by [ALP-123](https://linear.app/alphamind-jatassi/issue/ALP-123/continuous-monitor) story 04b / [ALP-439](https://linear.app/alphamind-jatassi/issue/ALP-439/04b-emergency-invocation-trigger)), enforces the 30-minute cooldown (with margin-call override), and dispatches `run_invocation(trigger_type="emergency", firing_run_type=RunType.emergency, ...)` for each accepted request. Lands the shared vocabulary additions this surface needs: a new `EventType` member + typed detail class, a new `RunType` member + `config/run_types/emergency.yaml`, a new `emergency_invocation_cooldown_minutes` field on `BreachBehaviorConfig` + the matching yaml key. Coordinated edits to [ALP-123](https://linear.app/alphamind-jatassi/issue/ALP-123/continuous-monitor) parent decision (E) and [ALP-439](https://linear.app/alphamind-jatassi/issue/ALP-439/04b-emergency-invocation-trigger)'s story body land in the same Linear pass at the time this story is implemented.

## Reading

* Parent issue `ALP-431` § Pre-resolved configuration decisions (B), (C), (F) — HTTP deferral, cooldown canonical home, emergency.yaml shape
* Parent issue `ALP-431` § Notes for the orchestrator — sibling-tree coordinated edits in story 04b, coordinated edits to [ALP-123](https://linear.app/alphamind-jatassi/issue/ALP-123/continuous-monitor) / [ALP-439](https://linear.app/alphamind-jatassi/issue/ALP-439/04b-emergency-invocation-trigger) at landing time
* `docs/design/06-risk-guardrails/breach-behavior.md` § Emergency invocation trigger — trigger conditions, 30-minute cooldown rule, margin-call override, scheduled-cadence reset
* `docs/design/configuration-management.md` § `run_types/<trigger>.yaml` — the bundle shape `emergency.yaml` must match
* `config/breach_behavior.yaml` — where the cooldown key lands (peer of `drawdown_velocity` and `multi_rule_breach`)
* `config/run_types/pre_open.yaml` — the shape `emergency.yaml` mirrors per decision (F)
* `src/alphamind/portfolio_state/events/activity_log.py` — existing `EventType` / `EventGroup` / `EventSource` / `ActivityLogEntry` shape this story extends; the `EVENT_TYPE_TO_GROUP` and `EVENT_TYPE_TO_DETAIL` mappings the new EventType registers into
* `src/alphamind/config/models/run_types.py` — `RunType` StrEnum this story extends with `emergency`
* `src/alphamind/config/models/breach_behavior.py` (or the file currently holding `BreachBehaviorConfig`) — new field landing site
* `src/alphamind/execution/state_persistence/repository/activity_log_queries.py` (or wherever the activity-log read helpers live) — query precedent for polling new entries
* `src/alphamind/scheduler/orchestrator.py` (story 03b) — `run_invocation` is the dispatch target
* `src/alphamind/scheduler/supervisor.py` (story 01) — `PipelineSupervisor.register_task` registration surface
* `ALP-439` (continuous monitor 04b) — the WRITER side of `EMERGENCY_INVOCATION_REQUESTED`; coordinated edit at landing time updates its body to read cooldown from `breach_behavior.yaml`

## Depends on

* `ALP-445` (this work tree, story 03b) — `run_invocation` is the dispatch target.

## Scope

In scope under `src/alphamind/scheduler/emergency.py` (the receiver task) + coordinated edits in `src/alphamind/portfolio_state/events/activity_log.py` (EventType addition) + `src/alphamind/config/models/run_types.py` (RunType.emergency) + `src/alphamind/config/models/breach_behavior.py` (cooldown field) + `config/run_types/emergency.yaml` (new file) + `config/breach_behavior.yaml` (cooldown key). Tests at `tests/scheduler/test_emergency.py` + extensions to existing event / config / run-type tests. Coordinated Linear edits to [ALP-123](https://linear.app/alphamind-jatassi/issue/ALP-123/continuous-monitor) parent + [ALP-439](https://linear.app/alphamind-jatassi/issue/ALP-439/04b-emergency-invocation-trigger) body.

### 1\. `EventType.EMERGENCY_INVOCATION_REQUESTED` + typed detail

In `src/alphamind/portfolio_state/events/activity_log.py`:

* Add `EMERGENCY_INVOCATION_REQUESTED = "EMERGENCY_INVOCATION_REQUESTED"` to the `EventType` StrEnum under the existing "Risk and guardrail events" group.
* Add a typed `EmergencyInvocationRequestedDetail(BaseModel)` Pydantic model with frozen config and fields:
  * `trigger_type: Literal["regime_jump", "multi_rule_breach", "drawdown_velocity", "margin_call"]`
  * `trigger_reason: str` (free-form description: e.g., `"Regime jump: normal → crisis"`)
  * `cooldown_remaining_seconds: int = Field(ge=0)` (0 when cooldown is satisfied or bypassed by margin_call)
* Register the new EventType in `EVENT_TYPE_TO_GROUP` mapping → `EventGroup.RISK_AND_GUARDRAIL`.
* Register the new EventType in `EVENT_TYPE_TO_DETAIL` mapping → `EmergencyInvocationRequestedDetail`.
* Re-export `EmergencyInvocationRequestedDetail` from the module's `__all__` (alongside the other detail classes).

### 2\. `RunType.emergency`

In `src/alphamind/config/models/run_types.py`:

* Add `emergency = "emergency"` member to the `RunType` StrEnum at the end of the existing six members.
* Update the docstring to note the seventh member is non-cron (no `scheduler.yaml` entry) and is dispatched by the emergency-invocation receiver.
* Update the cross-reference validator in `src/alphamind/config/validation/cross_reference.py`: the rule "every trigger key in `scheduler.yaml`'s `triggers:` map has a matching `run_types/{key}.yaml`" is preserved. The new rule: `RunType.emergency` must have a matching `run_types/emergency.yaml` file but does NOT need a `scheduler.yaml` entry. Add a one-line check.

### 3\. `config/run_types/emergency.yaml`

Per parent decision (F), mirror `pre_open.yaml`:

```yaml
# Emergency invocation — dispatched by the pipeline scheduler when the continuous
# monitor writes EMERGENCY_INVOCATION_REQUESTED to activity_log per
# breach-behavior.md § Emergency invocation trigger. Same agent roster + heaviest
# adaptive-researcher budget as pre_open.yaml — emergencies are high-density events.

agents:
  enabled:
    - tech_semis_researcher
    - financials_researcher
    - energy_researcher
    - qualitative_researcher
    - adaptive_researcher
    - synthesizer
    - analyst
    - strategist
    - portfolio_manager
  overrides:
    adaptive_researcher:
      cumulative_tool_call_limit: 25
      cumulative_tool_token_budget: 4000
      latency_budget_seconds: 300

qualitative_researcher:
  news_digest:
    top_n_per_sector: 5
    top_n_high_priority: 3
```

### 4\. Cooldown config addition

Per parent decision (C), the canonical home is `breach_behavior.yaml`. In `src/alphamind/config/models/breach_behavior.py`:

* Add `emergency_invocation_cooldown_minutes: int = Field(ge=1)` to the appropriate Pydantic model (`BreachBehaviorConfig` or its nested emergency-invocation section). If the existing model has nested sections (`drawdown_velocity`, `multi_rule_breach`), add a sibling section or hoist to a top-level field — match the existing structure.
* Add the key to `config/breach_behavior.yaml`:

  ```yaml
  emergency_invocation_cooldown_minutes: 30          # see breach-behavior.md § Cooldown — minimum 30 min between emergency triggers; margin_call bypasses
  ```

### 5\. Activity-log polling loop

`src/alphamind/scheduler/emergency.py`:

```python
async def run_emergency_receiver_task(
    session: PipelineSession,
    *,
    poll_interval_seconds: int,
    cooldown_minutes: int,
    session_factory: async_sessionmaker[AsyncSession],
    archive_root: Path,
    config_dir: Path,
    env_path: Path,
    venue_config: VenueConfig,
    execution_mode: ExecutionMode,
) -> None:
    """Poll activity_log for EMERGENCY_INVOCATION_REQUESTED and dispatch."""
```

Behavior:

1. Maintain in-memory `last_seen_entry_id: int | None`. Initialize from the most recent entry's ID at task start (so a long-running monitor's pre-startup writes are NOT replayed).
2. Loop:
   * `await asyncio.sleep(poll_interval_seconds)`
   * Open a short read-only session.
   * Query `activity_log` for entries with `event_type='EMERGENCY_INVOCATION_REQUESTED'` and `entry_id > last_seen_entry_id`, ordered ASC by `entry_id`.
   * For each new entry:
     * Parse `EmergencyInvocationRequestedDetail` from the JSON detail field.
     * **Cooldown check.** If `detail.trigger_type == "margin_call"`: bypass cooldown. Otherwise: query the most recent `invocations` row with `trigger_type='emergency'` AND `phase2_completed_at IS NOT NULL`. If its `phase2_completed_at >= now - cooldown_minutes`: log `INFO emergency request entry_id=<id> suppressed — cooldown`; advance `last_seen_entry_id`; continue.
     * **Dispatch.** Try:
       * `summary = await run_invocation(trigger_type="emergency", trigger_source="continuous_monitor", trigger_reason=detail.trigger_reason, firing_run_type=RunType.emergency, ...)`
       * Log `INFO emergency invocation completed entry_id=<id> invocation=<inv_id> duration=<sec>s`
         Except Exception:
       * `log.exception("emergency invocation entry_id=%s failed", entry_id)`
       * Do NOT re-raise (the receiver continues polling).
     * Advance `last_seen_entry_id = entry_id`.
3. Loop until cancellation (`asyncio.CancelledError`).

### 6\. Wire into `__main__.py`

Update the daemon-mode branch in `src/alphamind/scheduler/__main__.py` to register the receiver task alongside the APScheduler task from story 04a:

```python
supervisor.register_task(
    name="emergency_receiver",
    coro_fn=partial(
        run_emergency_receiver_task,
        poll_interval_seconds=scheduler_config.emergency_poll_interval_seconds,
        cooldown_minutes=breach_behavior_config.emergency_invocation_cooldown_minutes,
        session_factory=session_factory,
        archive_root=archive_root,
        config_dir=config_dir,
        env_path=env_path,
        venue_config=venue_config,
        execution_mode=execution_mode,
    ),
)
```

### 7\. Coordinated Linear edits at landing time

When this story lands (orchestrator step at PR-merge time, not subagent's job):

* Update `ALP-123` parent decision (E): point cooldown source to `breach_behavior.yaml`, note that monitor's 04b reads from there. Use `save_issue` with the existing description plus the corrected stanza.
* Update `ALP-439` body: in the "## Scope" or wherever the cooldown source is named, replace `continuous_monitor.yaml` references with `breach_behavior.yaml`. Use `save_issue` to update.

This is an *orchestrator-time* coordinated edit, not the subagent's work — the subagent ships the code, the orchestrator updates the sibling Linear stories.

### Out of scope

* The WRITER side (continuous monitor's emergency-trigger detection logic) — owned by `ALP-439`.
* HTTP `/control/trigger_emergency_invocation` surface — deferred per parent decision (B); operators trigger emergencies by hand via SQL insert into `activity_log` if needed, or wait for the monitor to detect.
* NSSM install scripts — story 05.
* End-to-end verify script — story 05.

## Acceptance criteria

- [ ] `EventType.EMERGENCY_INVOCATION_REQUESTED` is a member of the `EventType` StrEnum; `EmergencyInvocationRequestedDetail` is importable from `alphamind.portfolio_state.events.activity_log`.
- [ ] `EmergencyInvocationRequestedDetail` is frozen; constructing with `cooldown_remaining_seconds=-1` raises `ValidationError`.
- [ ] `EVENT_TYPE_TO_GROUP[EventType.EMERGENCY_INVOCATION_REQUESTED] == EventGroup.RISK_AND_GUARDRAIL`.
- [ ] `EVENT_TYPE_TO_DETAIL[EventType.EMERGENCY_INVOCATION_REQUESTED] is EmergencyInvocationRequestedDetail`.
- [ ] `RunType.emergency` is a member of the `RunType` StrEnum; `RunType.emergency.value == "emergency"`.
- [ ] `config/run_types/emergency.yaml` parses cleanly via the resolver and matches the structure of `pre_open.yaml`.
- [ ] `config/breach_behavior.yaml` carries `emergency_invocation_cooldown_minutes: 30` and the matching field validates on the `BreachBehaviorConfig` model (rejects `0` and negatives).
- [ ] Cross-reference validation in `src/alphamind/config/validation/cross_reference.py` confirms `run_types/emergency.yaml` exists; a missing file raises `CrossReferenceError`.
- [ ] `run_emergency_receiver_task` reads no entries on startup (initializes `last_seen_entry_id` from the current high-water mark).
- [ ] When a fresh `EMERGENCY_INVOCATION_REQUESTED` entry is written after task startup, the receiver dispatches `run_invocation(trigger_type="emergency", ..., firing_run_type=RunType.emergency)` and the dispatched invocation row carries `trigger_type='emergency'`.
- [ ] When two `EMERGENCY_INVOCATION_REQUESTED` entries land within the cooldown window (default 30 min), the second is logged as suppressed and NOT dispatched.
- [ ] When a `EMERGENCY_INVOCATION_REQUESTED` entry's `detail.trigger_type == "margin_call"` lands within the cooldown window, the cooldown is bypassed and dispatch occurs.
- [ ] An exception raised by `run_invocation` inside the receiver is logged via `log.exception` and does NOT propagate (the receiver keeps polling).
- [ ] `tests/scheduler/test_emergency.py` covers the criteria above and passes under `uv run pytest tests/scheduler/ -n auto`. Existing `tests/portfolio_state/events/` and `tests/config/test_run_types.py` and `tests/config/test_breach_behavior.py` are extended where touched and pass.
- [ ] `uv run pytest -n auto` (full suite) passes.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` is clean.

## Verification

* `uv run pytest tests/scheduler/test_emergency.py -n auto -v` — every new test passes.
* `uv run pytest -n auto` — full suite green.
* Manual: write an `EMERGENCY_INVOCATION_REQUESTED` entry via direct SQL against the paper DB; `python -m alphamind.scheduler run --mode paper` picks it up within `emergency_poll_interval_seconds` and dispatches an invocation; `sqlite3 alphamind.db "SELECT trigger_type, trigger_source, trigger_reason FROM invocations WHERE trigger_type='emergency' ORDER BY start_at DESC LIMIT 1"` shows the new row.
* Lint clean per CLAUDE.md.