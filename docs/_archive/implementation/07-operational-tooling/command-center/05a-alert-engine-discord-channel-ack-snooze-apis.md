# 05a — Alert engine + Discord channel + ack/snooze APIs

## Goal

Ship the alert engine that watches the multiplexed event stream (story 04b) + a 60 s periodic timer, evaluates the 17 default rules from the design doc against state transitions, debounces, routes by severity to the in-app banner + Discord webhook, and exposes acknowledge / snooze APIs. The engine is a deep module per P9 — single `engine.py` hides condition evaluation, debounce, severity routing, channel fanout. Rules whose upstream events don't yet fire in this work tree's scope remain dormant; the engine logs them at startup.

## Reading

* `docs/design/command-center.md` § Alerting — severity tiers, default rule set (17 rules), notification channels, acknowledge / snooze semantics.
* `src/alphamind/command_center/_kernel/ids.py` (from [ALP-666](https://linear.app/alphamind-jatassi/issue/ALP-666/02-command-center-backend-foundation)) — `AlertId`, `AlertRuleName`, `DiscordWebhookUrl` NewTypes.
* `src/alphamind/command_center/persistence/tables.py` (from [ALP-666](https://linear.app/alphamind-jatassi/issue/ALP-666/02-command-center-backend-foundation)) — `alerts` table this story populates.
* `src/alphamind/command_center/config.py` (from [ALP-666](https://linear.app/alphamind-jatassi/issue/ALP-666/02-command-center-backend-foundation)) — `AlertsConfig` model; this story populates the `rules` list.
* `src/alphamind/command_center/events/multiplexer.py` (from [ALP-669](https://linear.app/alphamind-jatassi/issue/ALP-669/04b-apievents-sse-multiplexer)) — `EventMultiplexer` the engine subscribes to.
* `src/alphamind/command_center/auth/dependencies.py` (from [ALP-667](https://linear.app/alphamind-jatassi/issue/ALP-667/03-webauthn-sessions-csrf)) — `current_session`, `csrf_required` for the ack / snooze routes.
* [ALP-128](https://linear.app/alphamind-jatassi/issue/ALP-128/command-center) § Pre-resolved (F) — engine trigger (SSE primary + 60 s fallback) + ship-all-17-rules-even-if-dormant discipline.

## Depends on

* [ALP-666](<https://linear.app/alphamind-jatassi/issue/ALP-666>) (02) — alerts table, AlertsConfig, supervisor.
* [ALP-669](<https://linear.app/alphamind-jatassi/issue/ALP-669>) (04b) — `EventMultiplexer` subscription source.

## Scope

In scope, under `src/alphamind/command_center/alerts/`. Tests at `tests/command_center/alerts/`.

### 1\. New `command_center/alerts/` subpackage

```
src/alphamind/command_center/alerts/
├── __init__.py
├── rules.py                # AlertRule, AlertCondition, AlertSeverity frozen dataclasses + StrEnum
├── conditions.py           # 17 default condition predicates (one per rule from the design doc)
├── engine.py               # AlertEngine: subscribes to mux + 60s timer; evaluates rules; debounces; routes
├── persistence.py          # alerts table CRUD: insert fired, update on ack/snooze, list active
├── channels/
│   ├── __init__.py
│   ├── in_app.py           # InAppChannel: pushes alert events onto the multiplexer for browser delivery
│   └── discord.py          # DiscordChannel Protocol + RealDiscordChannel (httpx POST)
└── routes.py               # GET /api/alerts (list active), POST /api/alerts/{id}/acknowledge, POST /api/alerts/{id}/snooze
```

### 2\. Rule + condition model (`rules.py`, `conditions.py`)

```python
class AlertSeverity(StrEnum):
    CRITICAL = "critical"
    IMPORTANT = "important"
    OPERATIONAL = "operational"

@dataclass(frozen=True, slots=True)
class AlertRule:
    name: AlertRuleName
    severity: AlertSeverity
    debounce_window: timedelta
    channels: tuple[str, ...]   # ("in_app", "discord") or subset
    condition: AlertCondition

class AlertCondition(Protocol):
    def evaluate(self, *, event: CombinedEvent | None, state: AlertEvaluatorState) -> bool: ...
```

`conditions.py` defines 17 predicate classes — one per rule from the design doc's default rule set table. Each predicate either consumes events (`agent_failed` → "Agent malformed output retry"; `breach_detected` → "Hard-block guardrail rejection"; etc.) or polls state via the periodic timer (`portfolio_summary.cumulative_drawdown crosses progressive tier` → DB query against the foreign reader session).

Rules whose triggering events don't yet emit (e.g., `Q1/Q6/Q8 categorical API failure`) are still registered; their predicate logs a one-time WARNING at engine startup that the upstream emit point doesn't exist yet.

### 3\. `AlertEngine` (`engine.py`)

```python
class AlertEngine:
    async def run(self, multiplexer: EventMultiplexer) -> None:
        """Long-running task registered in the supervisor's TaskGroup.
      
        Concurrently:
          - subscribe to the multiplexer; for each event, evaluate matching rules
          - on a 60s periodic timer, evaluate state-polling rules
      
        Each rule's debounce window suppresses re-firing within window.
        On fire: writes alerts row + dispatches to configured channels.
        """
```

Debounce: per `(rule_name, primary_entity)` keying — e.g. `(margin_call, account)` debounces re-fires; `(hard_block_guardrail_rejection, position_X)` debounces by position. Keying lives on `AlertRule.condition`'s output.

### 4\. Channels (`channels/`)

`InAppChannel` — pushes an `AlertFiredEvent` onto the `EventMultiplexer` with `source="cc"`; the frontend's `useEventStream()` hook picks it up; the alert-banner component renders it. The browser fetches active alerts via `GET /api/alerts` on page load + maintains via the SSE channel.

`DiscordChannel` Protocol + `RealDiscordChannel` httpx-backed implementation that posts a webhook embed (title, severity, context, deep link to the alert detail in the command center). Discord webhook URL sourced from `${ALPHAMIND_DISCORD_WEBHOOK}` env var per `config/alerts.yaml`. `FakeDiscordChannel` records calls for tests.

### 5\. Persistence (`persistence.py`)

Three operations on the `alerts` table:

* `insert_fired(rule_name, severity, context_json) -> AlertId`
* `update_acknowledged(alert_id) -> None`
* `update_snoozed(alert_id, snoozed_until) -> None`
* `list_active() -> Sequence[AlertRow]` — alerts with `status in (fired, snoozed)` and (for snoozed) `snoozed_until > now`.

Acknowledged/snoozed events also write activity_log rows with `source: operator_console` per design — wrap each in an `operator_invocation()` block (from story 02).

### 6\. Routes (`routes.py`)

* `GET /api/alerts` — returns active alerts. `Depends(current_session)`.
* `POST /api/alerts/{id}/acknowledge` — marks acknowledged. `Depends(current_session)` + `Depends(csrf_required)`.
* `POST /api/alerts/{id}/snooze` — body `{snoozed_until_iso: str}`. `Depends(current_session)` + `Depends(csrf_required)`.

### 7\. Populate `config/alerts.yaml`

Replace the empty rules list from story 02 with the 17 default rules per the design doc's table. Each entry: name, severity, debounce_minutes, channels.

### Out of scope

* Custom rule registry editor — operator edits `config/alerts.yaml` via the editor in story 06b.
* New channel kinds beyond Discord — channel registry is structured for future expansion.
* Backfill / migration of historical alerts — fresh start.

## Acceptance criteria

- [ ] `command_center/alerts/` subpackage exists with all listed modules.
- [ ] `AlertRule`, `AlertSeverity`, `AlertCondition` types exist per spec.
- [ ] All 17 default rules from the design doc are defined in `conditions.py` and registered in `config/alerts.yaml`.
- [ ] Rules whose upstream emits don't exist yet log a single startup WARNING naming the missing emit point.
- [ ] `AlertEngine.run()` subscribes to the multiplexer + runs a 60 s timer; both fire rule evaluation.
- [ ] Per-rule debounce suppresses re-fires within the configured window; an event firing twice in 1 s with a 60 s debounce produces one alerts row.
- [ ] `InAppChannel` pushes alert-fired events onto the multiplexer (browser will receive via SSE).
- [ ] `DiscordChannel` posts a webhook embed when configured; with `${ALPHAMIND_DISCORD_WEBHOOK}` unset, the channel logs WARNING and skips.
- [ ] `GET /api/alerts` returns active alerts; `POST /api/alerts/{id}/acknowledge` and `/snooze` mutate the row + write an `operator_console` activity_log entry inside an `operator_invocation()` block.
- [ ] Tests cover: each rule firing for its trigger condition, debounce, severity routing, channel fanout (fake Discord verifies post), ack / snooze roundtrip with audit row, session + CSRF gating.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, `uv run lint-imports` all pass.

## Verification

Scoped pytest: `uv run pytest tests/command_center/alerts/ -n auto`. Manual smoke deferred to verify story 07 (observe at least one alert firing end-to-end against running pipeline / monitor).