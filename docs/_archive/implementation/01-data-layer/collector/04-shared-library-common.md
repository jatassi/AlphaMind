---
status: done
completed_date: 2026-04-26
commit_id: 765b050
---

# 04 — Shared library `_common.py`

## Goal

Implement the cross-cutting primitives every vendor adapter depends on: configuration loader, retry-per-tier decorator, per-provider rate limiter, and the `track_run` context manager.

## Reading

- `docs/design/01-data-layer/collector/data-sources.md` — primitive specs
- `docs/design/01-data-layer/api-failure-handling.md` — retry tier definitions
- `docs/design/configuration-management.md § Reload model`, § Validation — config loading semantics

## Depends on

- 03a (Pydantic models for YAML configs)
- 03b (persistence — `track_run` writes `collection_runs`)

## Scope

In scope: `src/alphamind/data_sources/_common.py` containing —
- `load_config()` — reads `.env` via `python-dotenv`, loads `data_sources.yaml`, `collector_schedule.yaml`, `news_outlets.yaml`, validates each via Pydantic models from story 03a, returns a frozen aggregate config object. Missing required env vars raise a clear error naming the variable.
- `RetryShape` — enum with `critical` / `important` / `optional` per `api-failure-handling.md`.
- `with_retries(shape)` — decorator factory implementing tier behavior:
  - `critical`: multiple attempts (default 3), exponential backoff, retries on retryable errors.
  - `important`: limited attempts (default 2), exponential backoff, retries on retryable errors.
  - `optional`: single retry, brief delay, retries on retryable errors.
  - All shapes propagate non-retryable errors immediately.
  - Retryable: timeouts, 5xx, 429 rate-limit responses. Non-retryable: 4xx auth/permission errors.
- `RateLimiter` — per-provider token-bucket driven by `data_sources.yaml.providers.<v>.rate_limit_per_minute`. Thread-safe (the runner uses thread executors). Refills continuously, not in discrete time slices, so a 100/min budget allows 10/sec sustained without bursting. Exposes `acquire(provider)` blocking call.
- `track_run(collector_name)` — context manager that:
  - On enter: inserts a `collection_runs` row with `status='running'`, returns a mutable run object.
  - On normal exit: updates to `status='success'` with `completed_at` and the run object's `rows_written`.
  - On exception: updates to `status='failed'` with `error_summary`, then re-raises.
- Unit tests for each primitive.

Out of scope:
- Vendor-specific code (stories 05*).
- The runner that wires these into APScheduler (story 06b).

## Notes

Vendor SDKs raise their own exception types. Wrap them at the per-vendor `client.py` boundary in stories 05* so `_common.py` stays vendor-agnostic — `with_retries` checks for a small set of generic exception types (e.g., `httpx.HTTPStatusError`, `httpx.TimeoutException`) and the per-vendor wrappers translate.

`load_config()` is one-shot at process start. Don't attempt to reload mid-run; if config files change, the runner restarts. Return an immutable dataclass or Pydantic model instance — no global singleton.

Backoff parameters (initial delay, multiplier, max delay, max attempts) are configurable in `data_sources.yaml.retry_shapes` per `configuration-management.md` rather than hardcoded.

`track_run` should not swallow exceptions. The runner's APScheduler isolates the exception per-job.

## Acceptance criteria

- [ ] `_common.py` exposes `load_config`, `RetryShape`, `with_retries`, `RateLimiter`, `track_run`.
- [ ] `load_config()` reads `.env`, all three YAML files, validates each via Pydantic, returns an immutable config object.
- [ ] `load_config()` raises a clear error naming the missing env var when a required `*_env` reference doesn't resolve.
- [ ] `with_retries(critical)` retries multiple times on retryable failures with exponential backoff.
- [ ] `with_retries(important)` retries with limited attempts.
- [ ] `with_retries(optional)` makes one retry on retryable failure.
- [ ] All shapes propagate non-retryable (4xx auth) errors immediately.
- [ ] `RateLimiter` blocks when the per-provider per-minute budget is exhausted.
- [ ] `RateLimiter.acquire()` is thread-safe under concurrent calls.
- [ ] `track_run` writes a `running` row on enter.
- [ ] `track_run` updates to `success` with `rows_written` and `completed_at` on normal exit.
- [ ] `track_run` updates to `failed` with `error_summary` on exception, then re-raises.
- [ ] Unit tests cover all of the above.
- [ ] `uv run pytest` passes.
