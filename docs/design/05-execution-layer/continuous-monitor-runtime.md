# Continuous monitor — runtime

The continuous monitor's process shape, paired with [architecture.md § 4 Continuous monitor](architecture.md). This doc covers the supervisor / process / log / config / parallel-service topology; per-responsibility behavior (fill-stream consumption, breach detection, emergency triggering, greeks refresh, options bracket evaluation) lives in `architecture.md § 4`.

## Process model

The monitor is a separate long-running process supervised by NSSM as `alphamind-monitor`, parallel to `alphamind-collector` and the pipeline scheduler. The runtime is asyncio: the hot path combines a websocket subscription (Alpaca `trade_updates` for fills plus `StockDataStream` for underlying prices), periodic timers (breach evaluation, scheduled greeks refresh), and in-memory caches (last refresh price per underlying, halt-mode session bookkeeping). The collector's `BlockingScheduler` + threads model is a poor fit — its job-per-vendor cron cadence and short-lived per-job callable contract doesn't match a single-process event loop with concurrent websocket subscriptions and per-tick reactions. NSSM provides crash-restart; the monitor's task-level exceptions propagate to the supervisor and out of `run()` for NSSM to restart on.

## Shared utilities reused from the collector

The monitor reuses three building blocks from the collector + pipeline scheduler so the operator's mental model stays uniform across services:

- `python-dotenv.load_dotenv()` — the same entry-point preamble the collector and pipeline scheduler use to populate API credentials from the project-root `.env` before any subcommand runs.
- A logging helper matching `collector.scheduler._configure_logging` shape — same `TimedRotatingFileHandler` (30-day retention, daily rotation), same `alphamind` root logger so descendant loggers share a configurable level. The monitor writes to its own file (`monitor.log`) so a collector backlog doesn't entangle the monitor's audit trail.
- `default_session_factory()` from `alphamind.data_sources._common` for SQLite access. The monitor reads from the same database the collector writes to (story 03a reads `options_chains` for the IV used in greeks refresh; later stories will read additional collector tables). WAL mode handles the concurrent reader / writer overlap.

The monitor does not share an executor with the collector. Each NSSM service owns its own process; the asyncio loop lives entirely inside `alphamind-monitor`.

## Logging

`configure_monitor_logging()` installs a `TimedRotatingFileHandler` on the `alphamind` root logger, writing to:

- `%USERPROFILE%\AlphaMind\logs\monitor.log` on Windows (production).
- `~/AlphaMind/logs/monitor.log` on POSIX (development).

Rotation is daily at midnight; retention is 30 days. The handler is idempotent — a repeat call returns without attaching a duplicate handler — so tests that exercise the supervisor in-process don't accumulate handlers across runs.

## Configuration surface

`config/continuous_monitor.yaml` (Class A per the configuration-management taxonomy) is loaded into a frozen Pydantic `ContinuousMonitorConfig` at process start and surfaced on `resolved_config.continuous_monitor`. The six knobs (parent issue ALP-123 § Pre-resolved decision (E)):

- `breach_evaluation_cadence_seconds: 60` — how often the breach loop (story 03b) wakes and walks the active rule set.
- `greeks_refresh_interval_minutes: 15` — scheduled cadence for the greeks-refresh task (story 03a); move-based triggers operate independently.
- `greeks_refresh_underlying_move_threshold_pct: 2.0` — underlying move (percent cumulative from the last refresh) that forces an out-of-cadence greeks recompute. Measured continuously against the live stream the monitor already consumes (decision (C)) — no extra subscription.
- `underlying_stream_provider: alpaca-iex` — `Literal` today; story-02b's vendor surface (Alpaca `StockDataStream` against the IEX feed, per decision (B)).
- `max_reconnect_attempts: 5` — websocket reconnect ceiling per session. Once exhausted, the supervisor's owning task raises and NSSM's restart policy kicks in.
- `supervisor_shutdown_timeout_seconds: 5` — per-task cancellation budget the asyncio supervisor enforces at shutdown so a task that swallows `CancelledError` doesn't stall the process.

The emergency-invocation cooldown lives canonically in `config/breach_behavior.yaml` as `emergency_invocation_cooldown_minutes` — it is a property of the emergency-trigger rule itself, peer of `drawdown_velocity` and `multi_rule_breach`. The monitor's story 04b reads it from `BreachBehaviorConfig`, not from `ContinuousMonitorConfig`.

## Relationship to the collector

Parallel NSSM services with different failure domains. The collector writes market-data tables (`equity_bars`, `options_chains`, `corporate_actions`, etc.); the monitor reads them. SQLite WAL mode handles concurrent reader / writer access without explicit locking. There is no shared executor, no shared process, and no shared supervisor — a collector outage doesn't stop the monitor from consuming fills, and a monitor outage doesn't stop the collector from continuing to poll vendors.

The monitor depends on the collector for one read path today: story 03a reads the latest `options_chains` row per underlying for the IV input to greeks recomputation. The collector's `polygon.options` cron runs every 30 minutes during market hours; the monitor's scheduled greeks-refresh cadence (15 minutes) and move-triggered cadence both consume whatever snapshot the collector has most recently landed. Fresh per-refresh pulls are a future optimization (decision (C)).

## Out of scope for this doc

Per-responsibility runtime detail lives in `architecture.md § 4`. NSSM install scripts ship in story 05 (mirroring `scripts/install_collector_service.ps1` and `scripts/uninstall_collector_service.ps1`). The runbook (operator commands, log inspection, common failure modes) ships in story 05.
