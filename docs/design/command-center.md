# Command center

How the operator monitors active runs, reviews past runs, edits configuration, manages portfolio and theses, and is alerted to conditions requiring attention. Implemented as a web application running on the same machine as the pipeline and continuous monitor, accessible locally and remotely via the operator's `atassi.org` domain with passkey authentication.

This spec covers *features* — what the command center surfaces, what it lets the operator do, how it alerts, how access is controlled. The command center implements the Phase 4 monitoring & alerting design item from [project-tracker.md](../project-tracker.md#phase-4--maturation-before-live-transition); scope includes configuration editing and operator-initiated control actions, which share authentication, audit, and access model with the monitoring surface.

---

## Scope

**In scope.**
- Views surfacing pipeline state, portfolio state, theses, run history, agent outputs, activity log, guardrail headroom, regime and overlay state, continuous-monitor health.
- A graphical configuration editor exposing every knob in `config/` as a structured form, never raw YAML text.
- Operator actions mutating system state (cancel orders, force closes, toggle halt mode, trigger emergency invocations, switch profile, run universe validation).
- Alert rule registry, alert conditions, severity model, notification channel definitions.
- Authentication, session management, local-plus-remote access model.

**Out of scope** (owned by other docs):

| Concern | Authoritative spec |
|---|---|
| Pipeline scheduling, processes, deployment | [architecture/infrastructure.md](../architecture/infrastructure.md) |
| Activity log event taxonomy and entity tables | [05-execution-layer/state-persistence.md](05-execution-layer/state-persistence.md) |
| Configuration file layout, composition resolver, validation layers | [configuration-management.md](configuration-management.md) |
| Per-rule guardrail definitions and breach mechanics | [06-risk-guardrails/rules-and-limits.md](06-risk-guardrails/rules-and-limits.md), [06-risk-guardrails/breach-behavior.md](06-risk-guardrails/breach-behavior.md) |
| Continuous monitor responsibilities | [05-execution-layer/architecture.md § Continuous monitor](05-execution-layer/architecture.md) |
| Feedback-loop analytics (thesis outcomes, calibration, prompt iteration) | Phase 4 [Feedback loop design](../project-tracker.md#phase-4--maturation-before-live-transition) — not yet landed |
| Tech stack, framework selection, interaction protocol with the pipeline/monitor | Follow-on conversation; this spec defers it explicitly |

The command center is an additive read-and-control surface over data that already lives in the system of record. Every datum traces back to an existing table or file.

---

## Architecture context

The command center is a third long-running process on the trading machine, alongside the pipeline and the continuous monitor. It serves a web UI to a single operator. The pipeline and monitor remain authoritative for all state mutation; the command center reads from shared state or invokes a narrow control surface those processes expose.

```
Trading machine (Windows)
├── alphamind-pipeline       (APScheduler-driven, invocation-on-trigger)
├── alphamind-monitor        (always-on, websocket fills, breach detection)
├── command center           (always-on, web UI, this spec)
│
├── %USERPROFILE%\AlphaMind\data\
│   └── alphamind.db         (SQLite, shared)
└── %USERPROFILE%\AlphaMind\archive\     (per-invocation file archive)
```

Three load-bearing properties:

1. **Live updates are push-based.** When a pipeline invocation transitions phases, the live run watcher reflects it within a second. When the monitor logs a fill or breach, the dashboard updates without refresh. Polling is permitted only as connection-recovery fallback.

2. **Read-only by default.** Every view is a read against existing state. State mutation happens only through explicit operator-action endpoints (see [Operator actions](#operator-actions)), each emitting an activity-log entry with `source: operator_console`.

3. **Single source of truth.** The command center never duplicates entity tables, activity log, file archive, or config files. Edits to config files write through to disk and are picked up by the standard invocation-time reload path; operator actions write through to the activity log via the same OMS / monitor write paths an internal command would use.

---

## Tech stack

### Frontend

| Component | Choice |
|---|---|
| Language | TypeScript |
| Framework | React |
| Build | Vite |
| Component primitives | shadcn/ui — BaseUI variants |
| Styling | Tailwind CSS |
| Routing | TanStack Router |
| Data fetching and cache | TanStack Query |
| Forms | React Hook Form + Zod |
| Tables | TanStack Table (headless) |
| Charts | Recharts |
| Type sharing with backend | `openapi-typescript` against the FastAPI-generated OpenAPI schema |

The Pydantic entity, config, and event models defined for the pipeline are reused as FastAPI request/response models; the FastAPI-emitted OpenAPI schema is the single source of truth for frontend types. No hand-maintained TypeScript interfaces for backend-defined entities.

### Backend

| Component | Choice |
|---|---|
| Language | Python 3.13 (matches the pipeline and monitor) |
| Framework | FastAPI (asyncio) |
| Web server | Uvicorn |
| Database access | SQLAlchemy 2.0 + `aiosqlite`, reusing the pipeline's session factory and ORM definitions |
| Authentication | `py_webauthn` for WebAuthn relying-party logic |
| Sessions | Signed cookies (HttpOnly, SameSite=Strict, Secure), CSRF protection on state-mutating endpoints |
| Static asset serving | FastAPI `StaticFiles` mount over the Vite-built frontend |

The command center backend is a third member of the existing Python project (same `pyproject.toml`, same dev tooling, same lint/type configuration). No duplicate ORM layer.

### Build and serving

A single port serves the entire surface in production: FastAPI mounts the Vite-built `dist/` at `/`, exposes the API at `/api/*`, the SSE channel at `/api/events`, and WebAuthn endpoints at `/auth/*`. Development uses the Vite dev server on a separate port with a proxy to the FastAPI backend.

### Process supervision

NSSM wraps each of the three Windows-side processes (pipeline, monitor, command center) as a Windows Service with restart-on-failure and stdout/stderr capture, per [infrastructure.md § Process supervision](../architecture/infrastructure.md#process-supervision-when-running-unattended). Log files and the SQLite database live under `%USERPROFILE%\AlphaMind\`.

### Versioning policy

Manifests carry minimum-version floors (`>=`) only; lockfiles (`uv.lock`, `package-lock.json`) provide build reproducibility. Floors are pinned to current stable releases at the lock date and float upward on subsequent `uv lock` / `npm install`. No upper bounds — upgrade friction outweighs the breakage they prevent. Cross-platform validation (Windows production, macOS development) lives in CI as a Windows + macOS job matrix running lint, type-check, build, and unit tests on each push.

| Toolchain | Floor |
|---|---|
| Python | `requires-python = ">=3.13"` in `pyproject.toml` |
| Node | LTS (current LTS line; the engine field in `package.json` pins the major) |
| Backend packages | Listed in `pyproject.toml` `[project.dependencies]` under the `# Command center backend` block — `fastapi`, `uvicorn[standard]`, `aiosqlite`, `webauthn` |
| Frontend packages | Listed in the command-center `package.json` under `dependencies` / `devDependencies` |

Cross-platform integration is handled by upstream libraries: `aiosqlite` and `webauthn` are pure-Python; `Uvicorn` handles signal differences between Windows and POSIX internally; FastAPI's `StaticFiles` mount handles Windows-vs-Unix path separators when serving the Vite-built `dist/`.

---

## Interaction model

The command center never holds write locks on the SQLite database and never invents authoritative state. State mutation flows through the pipeline's and monitor's write paths via a narrow control surface; live updates are transient screen state pushed over SSE, not persisted history.

### Control surface

The pipeline and the monitor each bind a localhost-only HTTP server (FastAPI + Uvicorn within the same process), loopback-bound and reliant on the OS's loopback isolation — the command center backend is the only client.

**Pipeline endpoints** (operator actions whose effect is scoped to scheduling, configuration, or invocation triggering):

| Method + path | Body | Effect |
|---|---|---|
| `POST /control/pause` | `{reason}` | Sets the scheduler-pause flag; subsequent triggers no-op and log `status: skipped_paused` |
| `POST /control/resume` | — | Clears the scheduler-pause flag |
| `POST /control/trigger_emergency_invocation` | `{reason}` | Fires an out-of-schedule invocation; subject to the existing `max_instances=1` and emergency-invocation cooldown |
| `POST /control/switch_profile` | `{profile_name}` | Writes `main.yaml` `active_profile`; takes effect at next invocation |
| `POST /control/run_universe_validation` | — | Executes `scripts/validate_universe.py` and returns the report inline |
| `GET /events` | — | SSE stream of pipeline state |

**Monitor endpoints** (operator actions whose effect is on positions, orders, or always-on state):

| Method + path | Body | Effect |
|---|---|---|
| `POST /control/cancel_order` | `{order_id}` | Submits CANCEL via engine-originated envelope; `source: operator_console` |
| `POST /control/force_close_position` | `{position_id, rationale}` | Submits CLOSE via engine-originated envelope with `close_rationale_type: risk_management`, `risk_management_subtype: pm_directed`; `source: operator_console` |
| `POST /control/set_halt_mode` | `{enabled, reason}` | Toggles the halt-mode flag |
| `GET /events` | — | SSE stream of monitor state |

Wire format — request bodies, response envelopes, error envelopes, and HTTP status code conventions — is specified in [pipeline-control-and-events-schema.md](pipeline-control-and-events-schema.md) and [monitor-control-and-events-schema.md](monitor-control-and-events-schema.md). Pipeline and monitor are distinct producers; each schema is the authoritative contract for its surface.

The command center backend exposes a public-facing `/api/control/*` surface that the browser talks to. Each browser-facing endpoint is authenticated, audited (writes an `activity_log` entry with `source: operator_console`), and proxies to the appropriate localhost endpoint on the pipeline or monitor. Browsers never reach the pipeline or monitor directly.

### Live event stream

Pipeline and monitor each push a transient event stream over SSE. The command center backend subscribes to both as a long-lived client and re-emits a multiplexed downstream SSE stream to the browser at `/api/events`. The browser maintains a single `EventSource` connection with native reconnect; the backend manages upstream connections and reconnects to pipeline or monitor independently if either drops.

**Pipeline events:**

| Event | Payload |
|---|---|
| `invocation_started` | `invocation_id`, `run_type`, `started_at` |
| `phase_transition` | `invocation_id`, `phase`, `phase_started_at` |
| `agent_started` | `invocation_id`, `agent_name`, `started_at`, `latency_budget_seconds` |
| `agent_succeeded` | `invocation_id`, `agent_name`, `duration_seconds`, `tokens_used` |
| `agent_retrying` | `invocation_id`, `agent_name`, `attempt`, `reason` |
| `agent_failed` | `invocation_id`, `agent_name`, `failure_mode` |
| `invocation_ended` | `invocation_id`, `status`, `commands_issued` |
| `next_trigger_changed` | `next_trigger_at`, `next_trigger_type` |
| `heartbeat` | `timestamp` (sent every 15 s when no other event has been sent) |

**Monitor events:**

| Event | Payload |
|---|---|
| `websocket_connected` | `timestamp` |
| `websocket_disconnected` | `timestamp`, `reason` |
| `fill_received` | `order_id`, `position_id`, `fill_price`, `fill_qty` |
| `breach_detected` | `rule`, `current_value`, `limit`, `response_classification` |
| `emergency_invocation_triggered` | `reason` |
| `greeks_refreshed` | `underlying`, `refreshed_at` |
| `heartbeat` | `timestamp` (sent every 15 s when no other event has been sent) |

SSE framing and per-event payload shapes are specified in [pipeline-control-and-events-schema.md § Event schema](pipeline-control-and-events-schema.md#event-schema) and [monitor-control-and-events-schema.md § Event schema](monitor-control-and-events-schema.md#event-schema).

These events carry no historical guarantee. If the SSE connection drops mid-invocation, the browser reconnects fresh; the live run watcher repopulates static state from the most recent `invocations` row plus current entity reads, then receives live events from the next push. No `Last-Event-ID` resume — live state is screen state, not history.

### Persistence boundary

| Data class | Persistence | Owner |
|---|---|---|
| Trade-relevant state changes (fills, orders, positions, theses, PM decisions, breaches, corporate actions) | `activity_log` and entity tables | Pipeline OMS, monitor — see [state-persistence.md](05-execution-layer/state-persistence.md) |
| Per-invocation summary (phase durations, agent metrics, status) | `invocations` table | Pipeline — see [infrastructure.md § Layer 1](../architecture/infrastructure.md#layer-1-structured-metrics-sqlite) |
| Per-invocation file archive (briefs, decision-layer outputs, commands) | `%USERPROFILE%\AlphaMind\archive\` | Pipeline — see [infrastructure.md § Layer 2](../architecture/infrastructure.md#layer-2-invocation-archive-files) |
| Live phase / agent / monitor state during operation | SSE stream only — not persisted | Pipeline, monitor (transient) |
| Operator actions | `activity_log` entries with `source: operator_console` | Routed through pipeline / monitor write paths |
| Alerts (active, fired history, acknowledgements, snoozes) | New `alerts` table | Command center |
| Operator session and WebAuthn credentials | New small dedicated tables | Command center |
| Review session state (highlights, annotations, pins, current view, operator selections) | In-process memory on the command center backend; transient | Command center — see [Review sessions](#review-sessions) |
| Weekly digest snapshots (serialized digest contents at the weekly snapshot boundary) | New `weekly_digest_snapshots` table | Command center — see [feedback-loop.md § Dashboard and digest curation](feedback-loop.md#dashboard-and-digest-curation) |
| Feedback-loop artifacts (validations, validation outcomes, retrospective reports + saved markdown, retrospective decisions) | New `validations`, `validation_outcomes`, `retrospective_reports`, `retrospective_decisions` tables plus filesystem-stored report markdown | Command center backend, populated via the `/feedback-validate` and `/feedback-retrospective` skills — see [state-persistence.md](05-execution-layer/state-persistence.md) |

The command center owns the alerts table and the credential store; everything else traces to a system of record owned by another component.

---

## Information sources

The command center reads exclusively from sources specified by other docs. Any new view must source from one of these or motivate adding a new source.

| Source | Owner | What the command center reads |
|---|---|---|
| `invocations` table | Pipeline | Per-invocation metadata: id, run_type, started_at, ended_at, status, phase_durations, agent metrics, command count, error summary |
| `activity_log` table | OMS, monitor | Append-only event stream — every position/order/bracket/thesis/cash/guardrail/PM-decision/corporate-action event |
| `positions`, `orders`, `brackets`, `theses`, `cash_ledger`, `fills`, `corporate_action_ledger` tables | OMS | Live core state |
| `portfolio_summary`, `thesis_quality_aggregates` tables | OMS | Derived aggregates refreshed at mutation time |
| `%USERPROFILE%\AlphaMind\archive\<date>\<time>_<run_type>\` | Pipeline | Per-invocation markdown archive: distillation outputs, analysis briefs (sector, qualitative, adaptive, synthesis), decision-layer outputs (analyst, strategist, PM), final OMS commands |
| Source-brief retrieval store | Pipeline (synthesizer) | Sections of upstream briefs indexed by reference ID (`SA-TECH-N`, `QR-N`, `AR-N`, etc.) |
| `%USERPROFILE%\AlphaMind\logs\pipeline.log`, `%USERPROFILE%\AlphaMind\logs\monitor.log` | Both processes | Operational logs for raw error inspection |
| `config/` YAML tree | Operator | Configuration files, presented as structured forms |
| Resolved-config snapshot | Pipeline | The composed profile × regime × mode × overlays bundle for the current invocation, written by the composition resolver at invocation start |
| Live status push from pipeline | Pipeline | Phase transitions, agent state changes, current `invocation_id` |
| Live status push from monitor | Monitor | Websocket connection state, fill events, breach detections, greeks refreshes, emergency invocation triggers |

The push channels in the last two rows are the only new producer obligations this spec adds to the pipeline and monitor.

---

## Views

Organized as five top-level UI sections, each with one or more views.

### A. Live operations

The landing surface. Always reflects current pipeline and monitor state.

#### Live run watcher

Headline view. When a pipeline invocation is running, shows phase-by-phase progress; otherwise shows the most recent completion plus a countdown to the next scheduled trigger.

| Pane | Content | Source |
|---|---|---|
| Pipeline status | `invocation_id`, run type, current phase, per-phase elapsed vs. budget, per-agent status (running / retrying / succeeded / failed-aborted) with retry attempts and reason | `invocations` table; live push from pipeline |
| Continuous monitor | Websocket connection state, time-since-connect, fill buffer depth, per-underlying greeks freshness with last-refresh timestamp, breach-detector state | Live push from monitor; positions table greeks fields |
| Active alerts banner | Unacknowledged alerts grouped by severity, with one-line context and click-through to alert detail | Alerts table (introduced in [Alerting](#alerting)) |

Long-running phases (analysis 1–4 min, decision 1–4 min) show a progress indicator anchored on the agent's latency budget. The watcher does not invent ETA — when the budget is exceeded but the agent has not failed, the indicator pegs and shows "in budget overage" until the agent retries, succeeds, or aborts.

#### Schedule preview

Small view showing the next several scheduled triggers and any pause / overlap-deduplication state. Sourced from APScheduler via the pipeline's status push.

### B. History and diagnostics

The diagnostic substrate — most-used during prompt iteration and incident review.

#### Run history

Paginated, filterable list of past invocations. Filters: date range, run type, status (completed / failed / partial), command-count-greater-than, has-errors. Each row shows `invocation_id`, started-at, duration, run type, status, # commands, # rejections, abort reason if any. Click → per-invocation detail page.

#### Per-invocation detail

Full graph for one `invocation_id`, rendered as a single navigable page:

| Section | Content | Source |
|---|---|---|
| Header | run type, started_at, ended_at, status, phase durations, agent metrics, error summary | `invocations` row |
| Distillation outputs | One tab per produced markdown brief | `archive/.../distillation/*.md` |
| Analysis briefs | Domain researcher briefs, qualitative brief, adaptive research, synthesis — each its own tab, with reference IDs (`SA-TECH-N`, `QR-N`, `AR-N`, `CR-N`) hyperlinked to the source-brief retrieval store viewer | `archive/.../analysis/*.md` |
| Analyst output | Structured rendering of recommendations (`REC-N`) — instrument, conviction, entry/target/invalidation legs, narrative fields, source-reference chips | `archive/.../decision/trader_recommendations.md` plus structured envelope from activity log |
| Strategist output | Per-position assessments (`SA-N`) and pending-order assessments (`SA-ORD-N`) — thesis status with `prior_status` transition arrow, recommended action with parameters, status and action rationales | `archive/.../decision/` plus structured envelope from activity log |
| Pre-processor bundle | §1 aggregate observations (combined-set impact, conviction distribution, book-health summary), §2 strategist section, §3 analyst section, conflict cross-references | bundle file in archive |
| PM envelopes | One per evaluated proposal — verdict, evaluation criteria pass/fail with notes, modifications, concerns, anti-patterns, rationale narrative, resulting commands | `pm_decision` activity log entries plus archived envelope |
| Commands and fills | OMS commands submitted, fills collected (this invocation's Phase 1), guardrail rejections | activity log filtered by `invocation_id` |

Every reference ID, position ID, thesis ID, order ID, command ID is a hyperlink to its detail view.

A rejected proposal renders with the same prominence as an approved one — the verdict pill, failed criterion list, anti-pattern tags, and rationale narrative are all primary content, not collapsed below the fold.

#### Activity log explorer

Filterable view over the full activity log. Filter dimensions: event type (the [taxonomy](05-execution-layer/state-persistence.md) of ~26 types), `invocation_id`, position/thesis/order ID, source subsystem, time range. Result table shows event type, timestamp, primary entity link, one-line summary; row expands to full event detail JSON.

The workhorse view for "what happened and why." Most other views link into it with a pre-applied filter (the position detail view's "history" tab is the activity log filtered by `position_id`).

#### Source-brief retrieval store viewer

Reader for indexed brief content. Pick an `invocation_id` and a reference ID prefix; see the brief sections keyed by that prefix as the decision-layer agents see them via `retrieve_brief`. Used to verify claims in agent narrative against underlying source.

#### Failure and abort log

Filtered view over invocations with `status` ∈ {failed, partial}. Shows failure mode (timeout / malformed / context-overflow / model-API / tool-use / data-layer-abort), retry counts, offending output if applicable. Click-through to invocation detail.

### C. Portfolio and theses

#### Portfolio dashboard

Single page showing current portfolio state in aggregate.

| Pane | Content | Source |
|---|---|---|
| Equity and P/L | Total portfolio value, equity high-water mark, current drawdown, daily realized P/L, cumulative realized P/L, total unrealized P/L | `portfolio_summary` |
| Cash and capital | Current cash balance, settled cash, reserved capital, available buying power, margin held, unsettled proceeds with settlement dates, Reg T excess (trailing 30d / 90d / lifetime — cumulative dollar cost of Reg T vs. portfolio-margin equivalent per [regt-margin-attribution.md](05-execution-layer/regt-margin-attribution.md)) | `cash_ledger`, `fill_records.regt_margin_attribution` |
| Exposure | Gross exposure %, net long/short exposure %, sector exposure breakdown ($ and %, long and short separately), delta-adjusted equivalents where options are present | `portfolio_summary` |
| Positions table | One row per open position: ticker / underlying, instrument type, direction, quantity, market value, unrealized P/L (abs and %), thesis status, position age, distance to target, distance to nearest invalidation | `positions` joined with `theses` |
| Pending orders | One row per pending order: order id, type, instrument, parameters, age, fill probability assessment from most recent strategist review if any | `orders` filtered by status `pending` or `partially-filled` |

#### Position detail

One page per position. Three tabs: state (current fields, bracket legs, fill history); thesis (component-level structure with linked legs, current and prior status, status transitions); history (activity log filtered by `position_id`).

The thesis tab renders components as cards keyed by component type (entry rationale, target rationale, invalidation rationale per leg), each showing narrative, key assumptions, and linked bracket leg or order. Status transitions are a vertical timeline on the right with cited signal per transition.

#### Theses dashboard

Filterable list of theses by status (active, resolved, cancelled), classification (on-track / partially-realized / at-risk / stale / invalidated), sector, age, resolution category. One row per thesis showing summary, current status with `prior_status` if recently transitioned, linked position, age, P/L of the underlying position.

#### Thesis detail

Full thesis: summary, all components with type and narrative, status history with cited signals at each transition, resolution outcome if closed (component-level outcomes plus thesis-level category). Cross-link to the source-brief retrieval store viewer for cited reference IDs.

### D. Configuration

#### Config editor

Graphical editor over the entire `config/` tree. The operator never sees raw YAML.

Each settings page corresponds to one config file or bundle (`profiles/<profile>.yaml`, `regimes/<regime>.yaml`). Each leaf value renders as an input control type-matched to the value:

| Value type | Control |
|---|---|
| Number with unit (%, USD, hours, σ, multipliers) | Number input with unit suffix; min/max enforced if known |
| Boolean | Toggle |
| Enum | Dropdown populated from the schema |
| String | Text input with regex validation if applicable |
| Array of strings (e.g., `active_sectors`, ticker arrays) | Tag-list editor with add and remove |
| Array of objects (e.g., overlay parameters, profile `rule_values`) | Table editor with row add and remove and per-cell type-matched controls |
| Cron expression | Structured "every N hours, anchored at HH:MM" picker with the equivalent cron string shown read-only |
| Path | Text input with existence indicator |

Inline validation surfaces the three layers from [configuration-management.md § Validation](configuration-management.md#validation):

- **Parse-time errors** — red highlight on the offending field with the error message.
- **Cross-reference errors** — page-level banner naming the broken reference (e.g., "active_profile names a profile that doesn't exist").
- **Semantic errors** — page-level banner naming the violated invariant (e.g., "regime multiplier drives `position_max_size_pct` to zero").

Save is gated on all three layers passing. The save action writes the YAML file to disk; the next invocation picks it up via the standard reload path.

Each setting carries a reload-policy badge — invocation-time-reload (most settings) or deploy-time-only (paths in `main.yaml`, SQLite pragmas, package versions, `.env` location). Deploy-time-only changes are saved with a banner explaining what restart is needed.

#### Resolved config viewer

Read-only view of the composed config the most recent invocation consumed (profile base × regime multipliers × active overlays × mode behavioral transform), side-by-side with the profile/regime/overlay sources. Diff between consecutive invocations' resolved configs is one click away.

#### Config history and diff

The `config/` tree is git-tracked; the command center shows commit history per file with diff rendering. Useful for "when did I change the daily drawdown limit and what was the trigger." For files not in git, the view shows last-modified timestamp and surfaces an "uncommitted changes" warning.

### E. Risk and guardrails

#### Guardrail dashboard

Full state of all rules at the current snapshot.

| Pane | Content | Source |
|---|---|---|
| Per-rule status | One row per rule: rule name, current value, limit value, headroom %, zone (normal / warning 70-85% / critical 85-95% / hard-block ≥95%) | Live computation from current state via the guardrail-evaluation library |
| Active multipliers and overlays | Current regime label and multiplier table, list of active overlays with their multipliers, mode flag (normal / halt / defensive_posture) | Resolved config snapshot |
| Drawdown state | Daily drawdown progress, cumulative drawdown progressive tier, halt-mode banner if active | `portfolio_summary`, halt-mode flag |
| Recent breaches | Activity log filter for `guardrail_rejection`, `risk_limit_approached`, `risk_parameter_changed` over the last day | activity log |

Click any rule row → drill-down showing per-position contribution, breach response classification (immediate vs. deferred-to-strategist), and cross-reference to [rules-and-limits.md](06-risk-guardrails/rules-and-limits.md).

#### Regime and overlay timeline

Chronological view of regime classifications, regime transitions (immediate-tightening up, gradual loosening over three invocations down), pre-event overlay activations, stress overlay activations. P/L drawdown and breach event markers overlay on the same timeline. Sourced from `risk_parameter_changed` activity log entries plus per-invocation resolved-config snapshots.

### F. Quality and feedback

The rendering surface for the [feedback loop](feedback-loop.md). Views are deterministic (no LLM tokens) and read from the persistence layer plus [counterfactual replay engine](05-execution-layer/counterfactual-replay-engine.md) output. [Session mode](#review-sessions) overlays on top of these views without changing them.

#### Weekly digest

Single scrollable page, snapshotted weekly. Section content (headline outcomes, process pulse, trajectory sparklines, validation status, notable shifts, open validation queue) and per-section metric selection are in [feedback-loop.md § Dashboard and digest curation](feedback-loop.md#dashboard-and-digest-curation). Default landing view of `/feedback-review`.

#### Validation evaluation view

Dedicated view for evaluating a registered validation (the EVALUATE mode of [`/feedback-validate`](../../.claude/skills/feedback-validate/SKILL.md)). Designed for side-by-side pre/post comparison with the discipline the skill enforces.

Layout, top to bottom:

1. **Registration recap.** Verbatim re-display of the validation record (edited artifact, pre/post git SHAs, watched metric, window length, expected direction and magnitude, success criterion, failure criterion, registered timestamp). The anti-rationalization anchor the skill reads aloud at evaluation start.
2. **Pre-edit window panel.** The watched metric over the registered pre-edit window: line chart with 80% credible band, sample size annotation, regime distribution overlay, active model version overlay, list of other prompt edits that landed in the window.
3. **Post-edit window panel.** Same shape as the pre-edit panel, for the post-edit window.
4. **Comparison summary.** Delta with credible-interval shape (overlapping vs. disjoint), confounder flags surfaced from the pre/post conditioning context (regime distribution mismatch, model version straddle, concurrent edits in window), and a non-binding suggested verdict (`improved` / `degraded` / `no_change` / `inconclusive`).
5. **Verdict capture.** Form for verdict and narrative; submits via `submit_validation_outcome()` and writes the validation outcome record per [state-persistence.md](05-execution-layer/state-persistence.md).

When the validation has been auto-superseded per [feedback-loop.md § Mid-window supersession](feedback-loop.md#mid-window-supersession), the view replaces panels 2–5 with a supersession banner (registration recap remains): the supersession reason, the timestamp it fired, the conditioning shift that triggered it, and a "register a fresh validation" affordance that hands off to `/feedback-validate` REGISTER seeded with the prior registration's fields. The verdict form is suppressed — superseded validations do not produce outcome records.

Session-mode affordances specific to this view: `pin_pre_panel(metric_id)` and `pin_post_panel(metric_id)` keep a specific pre or post panel visible across navigation. Standard `highlight_metric`, `navigate_to_view`, `annotate`, `pin_for_comparison` work as elsewhere.

Reachable from: the validation status row in the weekly digest, the supersession notable-shift row in the weekly digest, `list_pending_validations()` results, and direct deep-link to a specific validation ID.

#### Monthly view

Outcome-tier metrics with conditioning slices (regime, sector, conviction band, prompt version, model version), citation-chain visualizations, anti-pattern accuracy curves. Specific layout drafted once resolved-thesis volume supports meaningful outcome-tier reading (per [feedback-loop.md § Pending](feedback-loop.md#pending)).

#### Retrospective view

Renders a `retrospective_reports` record's saved markdown plus the joining `retrospective_decisions`. Walked through during Phase 4 of [`/feedback-retrospective`](../../.claude/skills/feedback-retrospective/SKILL.md), which is explicitly a top-to-bottom read of the report — the rendering supports that flow rather than imposing an alternative structure.

Layout:

| Pane | Content | Source |
|---|---|---|
| Header strip | Window range, generated timestamp, generated-by session ID, decisions-captured progress (`{decided}/{total}`) | `retrospective_reports` record + count of joined `retrospective_decisions` |
| Report body (left, scrollable) | Rendered markdown of the persisted report. Section headings get stable anchor IDs (slug of the heading text) so the rail can deep-link into them | Filesystem path on `retrospective_reports.report_file` (e.g., `data/retrospective_reports/{report_id}/report.md`) |
| Decision rail (right, sticky) | One row per promotion candidate and per suggested follow-up extracted from the report; each row carries a status pill (`open` / `accepted` / `rejected`), the item title, and either inline capture controls (open rows) or the captured verdict + rationale text (decided rows). Clicking any row scrolls the report body to the row's anchor and highlights it | Item rows parsed from the report's `## Promotion candidates` and `## Suggested follow-ups` sections; existing decisions joined on `(report_id, item_identifier)` from `retrospective_decisions` |

Item identifier and parsing: each promotion-candidate and follow-up bullet in the rendered report carries a stable identifier emitted by the report generator (slug of the bullet's title within its section, prefixed by section — e.g., `promotion_candidate.synthesizer_drop_pattern`, `follow_up.validation_strategist_at_risk_regime`). Identifiers are stable across re-renders so a captured decision survives editing the report's prose.

Decision capture (open rows in the rail):

- Accept / reject toggle, rationale textarea (required), and — for follow-ups whose accept verdict implies a validation registration — an optional "register a paired validation" affordance that hands off to `/feedback-validate` REGISTER seeded with the follow-up's title and rationale.
- Submit posts to `POST /api/retrospective/{report_id}/decisions` with `{item_identifier, decision_type, verdict, rationale, linked_validation_id?}` and writes a `retrospective_decisions` record per [state-persistence.md § Retrospective decisions](05-execution-layer/state-persistence.md). On success, the row flips to its read-only captured state.
- Captured rows are immutable from this view; correcting a decision is a new decision (the latest by `captured_at` wins for status-pill display, and the rail surfaces "{n} prior decisions" when n > 1, expandable to show the audit trail).

Session-mode affordances specific to this view: `scroll_to_section(section_anchor)` brings a markdown section into the operator's viewport during the walkthrough; `highlight_decision_row(item_identifier)` calls attention to a specific rail row when Claude wants to point at a particular promotion candidate or follow-up. Standard `highlight_metric`, `navigate_to_view`, `annotate`, `pin_for_comparison` work as elsewhere; `pin_for_comparison` on a captured decision row keeps it visible across navigation, supporting cross-quarter comparisons later in the session.

Reachable from: a "Recent retrospectives" entry in the weekly digest's notable-shifts section, the `/feedback-retrospective` Phase 4 `create_review_session()` + `navigate_to_view` step, and direct deep-link to `/feedback/retrospective/{report_id}`.

Cross-retrospective decisions ledger (deferred): a separate view filtering `retrospective_decisions` across every `retrospective_reports` record (by decision type, verdict, linked-validation presence, time range) ships once the second retrospective produces enough cross-period decisions to make the ledger meaningful. Per-report rendering does not depend on it.

#### Ad-hoc query surface

For everything not on curated views — direct query against the persistence layer with a SQL-like interface, filterable by date range, agent name, sector, regime, prompt version, and conditioning dimensions defined in [feedback-loop.md](feedback-loop.md).

---

## Review sessions

A capability overlaid on standard views, activated when the operator opens a [feedback-loop](feedback-loop.md) review session via one of the Skills (`/feedback-review`, `/feedback-validate`, `/feedback-retrospective`). In session mode the dashboard becomes a shared canvas: Claude can highlight metrics, navigate to views, annotate chart points, and pin items for comparison; the operator continues using the dashboard normally and Claude reads the resulting state on demand. Mirrors the Claude Code IDE pattern — Claude has *passive awareness* of what the operator is currently viewing and selecting, and can query the dashboard's current state at any prompt turn.

### Asymmetric model

Claude writes via tool calls; Claude reads via tool calls; the operator interacts with the dashboard the same way they do in self-review mode. No reverse channel triggers Claude on operator clicks. The session state on the backend is the single source of truth — both sides read and write it, and the dashboard renders from it via SSE.

### Session lifecycle

| Method + path | Body | Effect |
|---|---|---|
| `POST /review-sessions` | `{skill_name}` | Creates a new session; returns `session_id` and the dashboard URL operator opens in their browser |
| `DELETE /review-sessions/{id}` | — | Ends the session; transient state cleared |
| `GET /review-sessions/{id}/state` | — | Returns the current session state object (see schema below) |
| `POST /review-sessions/{id}/control` | `{action, params}` | Claude-issued action; mutates the session state and pushes an SSE event to the subscribed dashboard |
| `GET /review-sessions/{id}/events` | — | SSE stream the dashboard subscribes to; renders highlights, navigation, annotations, and pins as they arrive |

Session state is **transient**: it lives in process memory on the command center backend and is dropped when the session is explicitly ended or the operator closes the dashboard tab for longer than a 30-second tolerance window (which absorbs page refreshes). Sessions are scoped to the browser tab.

### v1 affordance vocabulary

The `action` field on `POST /control` accepts:

| Action | Params | Effect |
|---|---|---|
| `highlight_metric` | `{metric_id, label?, color?}` | Marks a metric on the current view with a colored badge and optional label |
| `highlight_chart_point` | `{chart_id, point_id, label?}` | Marks a specific data point on a chart with a callout |
| `navigate_to_view` | `{view_path}` | Navigates the dashboard to the named view (e.g., `quality-and-feedback/conviction-calibration`) |
| `annotate` | `{target_id, note_text}` | Attaches a short textual note to a target (metric, chart point, table row) |
| `pin_for_comparison` | `{target_id}` | Adds a target to the comparison-pin tray, where it remains across view changes for side-by-side comparison |
| `clear_highlights` | — | Removes all Claude-set highlights from the current view |
| `clear_annotations` | — | Removes all Claude-set annotations from the current view |
| `clear_pins` | — | Empties the comparison-pin tray |

Multiple actions may be batched in a single `POST /control` call (the body accepts `{actions: [...]}` as the bulk form), reducing tool-call overhead.

### Session state shape

The `GET /review-sessions/{id}/state` response:

```json
{
  "session_id": "...",
  "skill_name": "feedback-review",
  "started_at": "2026-04-26T14:00:00-04:00",
  "current_view": "quality-and-feedback/anti-pattern-frequency",
  "claude_highlights": [
    {"id": "...", "target_type": "metric", "target_id": "...", "label": "...", "color": "...", "set_at": "..."}
  ],
  "claude_annotations": [
    {"id": "...", "target_id": "...", "note": "...", "set_at": "..."}
  ],
  "pinned_items": [
    {"id": "...", "target_type": "...", "target_id": "...", "set_by": "claude" | "operator", "set_at": "..."}
  ],
  "operator_selections": [
    {"id": "...", "target_type": "...", "target_id": "...", "selected_at": "..."}
  ],
  "recent_view_history": [
    {"view_path": "...", "viewed_at": "..."}
  ]
}
```

`operator_selections` captures current and recent selections — table rows clicked, chart points hovered/clicked, metrics expanded — giving Claude the equivalent of the IDE's "currently viewing / currently selected" context. The load-bearing field for IDE-pattern integration.

`recent_view_history` is bounded (last ~20 entries) and rolls; it gives Claude awareness of what the operator has been navigating through during the session.

### Skill workflow shape

Each Skill (`/feedback-review`, `/feedback-validate`, `/feedback-retrospective`) follows the same shape:

1. Skill invocation creates a session via `POST /review-sessions` and returns the dashboard URL to the operator.
2. Operator opens the URL; the dashboard subscribes to the session's SSE stream.
3. On each prompt turn, the skill calls `GET /review-sessions/{id}/state` to inject current dashboard state into Claude's context.
4. Claude reasons about the state plus the operator's message plus the activity log / agent_calls / counterfactual_replays / thesis records data, then issues `POST /review-sessions/{id}/control` calls to highlight or navigate.
5. Operator reads Claude's text and looks at the dashboard; responds verbally in the chat.
6. Repeat until session end (`DELETE /review-sessions/{id}`).

The Skill prompts that orchestrate this are drafted as a follow-up to this surface.

### Session persistence

Review session state lives in process memory on the command center backend and is dropped on session end or browser disconnect (with the 30-second refresh tolerance). The artifacts the session reasons over (activity log, agent calls, counterfactual replays, thesis records) are persisted separately by the OMS and counterfactual replay engine; the session is a viewing surface over them.

---

## Operator actions

State-mutating actions exposed in the UI. Each emits an activity-log entry with `source: operator_console`. Every action is gated on confirmation (typed token for destructive actions, single-click for non-destructive). All actions require the same [authentication](#authentication-and-access) as views.

| Action | What it does | Where it lands in state |
|---|---|---|
| Trigger emergency invocation | Asks the pipeline to fire an out-of-schedule invocation immediately | Same path the monitor uses for breach-driven emergency invocations; respects the existing `max_instances=1` and emergency cooldown |
| Pause / resume scheduler | Sets a flag the scheduler reads at trigger fire time; while paused, fired triggers no-op and log the skip | `invocations` table records skipped triggers with `status: skipped_paused` |
| Cancel an order | Submits a CANCEL command via the engine-originated envelope path | Standard CANCEL command flow; activity log `order_cancelled` with `reason: operator_cancel` |
| Force-close a position | Submits a CLOSE command via the engine-originated envelope path with `close_rationale_type: risk_management`, `risk_management_subtype: pm_directed` | Standard CLOSE flow; activity log `position_closed` plus `pm_decision` envelope tagged `source: operator_console` |
| Toggle halt mode | Flips the halt-mode flag in the resolved config; the next invocation runs in halt / defensive-posture; existing positions retain bracket coverage | Activity log `risk_parameter_changed` with `reason: operator_halt_toggle` |
| Switch active profile | Edits `main.yaml`'s `active_profile`; subject to the [transitioning-between-profiles](06-risk-guardrails/rules-and-limits.md#transitioning-between-profiles) discipline | Standard config write path; takes effect at next invocation |
| Run universe validation | Executes the `scripts/validate_universe.py` validation procedure against `config/universe.yaml` and renders the report inline | No state change unless the operator subsequently edits `universe.yaml` |
| Acknowledge or snooze an alert | Updates the alert state | Alerts table |

No operator action for editing prompts in `prompts/` from the UI — prompt iteration stays in a text-editor-and-git workflow. The command center provides a *viewer* for the active prompt per invocation as a diagnostic aid, sourced from the `agent_calls` table's `system_prompt_path`, `system_prompt_git_sha`, and `system_prompt_snapshot_reference` fields per [state-persistence.md § Agent calls](05-execution-layer/state-persistence.md).

---

## Alerting

### Alert rules

An alert rule has four fields: condition, severity, debounce window, channels. Conditions are predicates over the same state the views read; a rule fires when the predicate transitions from false to true. Severity drives the notification channel and dashboard banner color.

**Severity tiers:**

| Tier | Definition | Default channels |
|---|---|---|
| Critical | Trading is degraded or at risk now | In-app banner (sticky) + Discord webhook |
| Important | Trading remains functional but operator review is warranted within the session | In-app banner + Discord webhook |
| Operational | Information for trend analysis; no immediate action required | In-app banner only |

**Default rule set** (initial population; the operator can add, edit, disable rules via the rule registry editor):

| Rule | Condition | Severity |
|---|---|---|
| Pipeline aborted | Most recent invocation `status = failed` | Critical |
| Critical-tier API failure | Q1, Q6, or Q8 data category failed in the most recent invocation | Critical |
| Schedule miss on critical category | A scheduled trigger for a Critical-tier category (Q1, Q6, Q8) fires but no `collection_runs` row starts within five minutes | Critical |
| Monitor websocket disconnected | Monitor reports websocket disconnected for ≥ 15 min | Critical |
| Margin call detected | Activity log `margin_call` event in the last hour | Critical |
| Drawdown progressive tier crossed | `portfolio_summary` cumulative drawdown crosses a [progressive tier](06-risk-guardrails/breach-behavior.md) threshold | Critical |
| Halt mode entered | Halt-mode flag transitions from off to on for any reason | Critical |
| Important-tier API failure | Q2, Q3, Q5, Q12, Qual 1, Qual 4, Qual 5, or Qual 6 data category failed in the most recent invocation | Important |
| Hard-block guardrail rejection | Activity log `guardrail_rejection` with zone `hard_block` | Important |
| Regime jump | Regime classification skips a level (e.g., normal → crisis without elevated) | Important |
| Agent malformed output | Activity log shows an agent malformed-output retry, before resolution | Important |
| Schedule miss | A scheduled trigger fires but no invocation runs within five minutes | Important |
| Data directory disk pressure | On-disk size of `%USERPROFILE%\AlphaMind\data\` exceeds the configured threshold (operator-pinned at landing — default candidate: 50 GB or 25% of the volume, whichever is smaller) | Important |
| Profile boundary crossed | `portfolio_summary.total_equity` falls outside the active profile's `capital_range_usd` (downward = downgrade candidate; upward = graduation candidate) | Operational |
| Optional data category skipped | Q4, Qual 2, or Qual 3 skipped by failure handler | Operational |
| Command abandoned | Activity log `command_abandoned` event | Operational |
| Thesis resolved | Activity log `thesis_resolved` event (informational, for feedback loop tracking) | Operational |

The rule registry is part of the configuration tree (`config/alerts.yaml`), edited via the same GUI config editor.

### Notification channels

| Channel | Behavior | Format |
|---|---|---|
| In-app banner | Always present on every page; severity-color-coded; persists until acknowledged or snoozed | Title, one-line context, click-through to alert detail |
| Discord webhook | Posts to a configured webhook URL on critical and important alerts | Embed with title, severity, context, link to the command center URL for the alert detail |

The Discord webhook URL is a secret in `.env` referenced from `config/alerts.yaml`. Additional channels (email, SMS, iMessage) are not in initial scope; the channel registry is structured so a future channel adds without altering the alert-rule contract.

### Acknowledge and snooze

Any alert can be acknowledged (clears the banner; remains in history) or snoozed for a specified duration (suppresses re-firing of the same rule for the snooze window). Snoozes are per-rule. An alert that was snoozed but whose underlying condition cleared and re-armed during the snooze re-fires as soon as the window expires. Acknowledges and snoozes are activity-log events with `source: operator_console`.

---

## Authentication and access

### Identity model

**Single user.** The command center serves exactly one operator. No multi-user provisioning, role-based access control, or per-user audit segregation. All state-mutating actions are attributed to the single operator identity in the activity log via `source: operator_console`.

### Authentication

**Passkey-based (WebAuthn).** No passwords. The operator registers one or more passkeys (resident credentials on phone, laptop, or hardware security key) at first launch, gated by a one-time setup token shown on the local console. Subsequent sessions are authenticated by passkey assertion only.

Sessions are time-limited; duration is configured in `config/security.yaml`. No "remember me" beyond the session lifetime.

### Access surfaces

**Local access.** From the trading machine, the command center is reachable on `localhost` at its bound port. Still subject to passkey authentication.

**Remote access.** The trading machine joins the operator's existing VPS WireGuard hub (`wg0`, `10.8.0.0/24`) as a new peer. The command center backend binds to the trading machine's WG IP. The VPS Caddy reverse proxy adds a site block for the chosen subdomain of `atassi.org` that proxies over the WG tunnel:

```caddy
commandcenter.atassi.org {
    import security_headers
    rate_limit /auth* { 10r/m }
    rate_limit /*      { 300r/m }
    reverse_proxy 10.8.0.<peer>:<port>
}
```

The public path: browser → Cloudflare DNS (DNS-only, grey cloud) → VPS:443 → Caddy (TLS termination, security headers, rate limit) → WireGuard tunnel → command center on the trading machine. No WireGuard client required on accessing devices; the VPS is the gateway. TLS terminates at the VPS; traffic between VPS and trading machine traverses the encrypted WG tunnel.

VPS-side artifacts (WG peer entry in `/etc/wireguard/wg0.conf`, Caddyfile block) are operator-maintained alongside existing VPS configuration, outside this repository.

### Audit

Every state-mutating endpoint logs an entry to the activity log with `source: operator_console`, action name, parameters, and session timestamp. The activity log explorer surfaces these alongside system-originated events; a saved filter view named "Operator actions" is available by default.

---

## Implementation status notes

Phase 4 design spec. The command center is unimplemented as of writing; the supporting primitives in [infrastructure.md § Observability](../architecture/infrastructure.md#observability) (the `invocations` table and file archive) are also Phase-4-and-later work.

One related work item tracked in [project-tracker.md](../project-tracker.md):

1. **Feedback-loop design.** The [Quality and feedback](#f-quality-and-feedback-stub) view section is stubbed; concrete metrics, time windows, and visualizations land alongside the feedback-loop spec.

---

## Cross-references

- Pipeline scheduling and process layout: [architecture/infrastructure.md](../architecture/infrastructure.md)
- Activity log event taxonomy and entity tables: [05-execution-layer/state-persistence.md](05-execution-layer/state-persistence.md)
- Configuration file layout and validation: [configuration-management.md](configuration-management.md)
- Continuous monitor responsibilities: [05-execution-layer/architecture.md § Continuous monitor](05-execution-layer/architecture.md)
- Per-rule guardrail definitions: [06-risk-guardrails/rules-and-limits.md](06-risk-guardrails/rules-and-limits.md)
- Breach mechanics and halt mode: [06-risk-guardrails/breach-behavior.md](06-risk-guardrails/breach-behavior.md)
- Per-invocation file archive layout: [architecture/infrastructure.md § Layer 2: Invocation archive](../architecture/infrastructure.md#layer-2-invocation-archive-files)
- Source-brief reference-ID taxonomy: [03-analysis-layer/synthesizer.md](03-analysis-layer/synthesizer.md)
- PM envelope schema and rejection structure: [04-decision-layer/pm-envelope-schema.md](04-decision-layer/pm-envelope-schema.md)
- Phase 4 feedback loop (when shipped): [project-tracker.md § Phase 4](../project-tracker.md#phase-4--maturation-before-live-transition)
