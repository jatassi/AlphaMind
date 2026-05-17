# 09c — Split `data_sources/_common.py`; hoist `_atomic_write` to `_kernel`

## Goal

`data_sources/_common.py` is 619 LOC / 8 unrelated concerns / imported by 22 of 47 data_sources modules — the most-imported and most-changed module in the data layer. Split it into focused submodules under `data_sources/_common/`. Separately, hoist the `_atomic_write` helper duplicated across three packages (`config/snapshot.py:129`, `distillation/calibration_snapshot.py:152`, referenced by `risk_guardrails/.../profile_switch.py:123`) to a single canonical implementation in `_kernel/atomic_io.py`.

## Reading

* `audit-alphamind-2026-05-12.html` § Findings — high-yield "Cross-package duplicated infrastructure" + Worth Knowing W3
* `src/alphamind/data_sources/_common.py` — 619 LOC, 8 concerns (config loading, retry policy, headline-tag taxonomy, rate limiter, run-tracking, DB helpers, universe lookup, env-example)
* `src/alphamind/config/snapshot.py:129` — `_atomic_write` source
* `src/alphamind/distillation/calibration_snapshot.py:152` — duplicate implementation
* Story 01b (<issue id="67c0550c-487c-44ba-ad9c-3e9f9f807d79">ALP-456</issue>) — `risk_guardrails/rules_and_limits/profile_switch.py` is moved by 01b; its `_atomic_write` reference will need to retarget if still referenced
* Story 02a (<issue id="81e17dca-4621-40e0-ba40-2a38c590d3a3">ALP-457</issue>) — `_kernel/` exists; `atomic_io.py` joins the existing leaf modules

## Depends on

* 02a (<issue id="81e17dca-4621-40e0-ba40-2a38c590d3a3">ALP-457</issue>) — `_kernel/` exists for `atomic_io.py`'s home

## Scope

In scope: convert `data_sources/_common.py` to `_common/` package with focused submodules; hoist `_atomic_write`. Tests update accordingly.

### 1\. Split `data_sources/_common.py` into `_common/` package

Per the audit's grouping:

* `_common/retry.py` — `RetryShape`, `with_retries`, `_is_retryable`, `_FREDAPI_RETRYABLE_MESSAGES`
* `_common/rate_limit.py` — `RateLimiter`, `_BucketState`
* `_common/run_tracking.py` — `track_run`, `RunState`, `_DefaultRepo`
* `_common/resume.py` — `resume_since` helper
* `_common/universe.py` — `active_universe_tickers`, asset universe lookup helpers
* `_common/config.py` — `load_config`, `AlphaMindConfig` (or hoist this up to `alphamind/config/` if it doesn't belong in data_sources)
* `_common/env_example.py` — `_ENV_EXAMPLE_KEYS`, `_load_env_example_keys` (related to the F4 cycle fix in 01b/02a if not already done)

Move `HeadlineType` + `normalize_vendor_tags` + `decode_topic_tags` to a NEW `data_sources/news/types.py` (the consumers — `finnhub/news.py`, `sec_edgar/rss.py`, `marketaux/news.py`, `news/clustering.py` already moved to analysis per 01b — all live in news domain).

`_common/__init__.py` curated re-exports so the existing `from alphamind.data_sources._common import X` continues to work for backward compat.

### 2\. Hoist `_atomic_write`

Create `src/alphamind/_kernel/atomic_io.py`:

```python
def atomic_write_text(path: Path, contents: str) -> None:
    """Write contents to path atomically: write to tmp, fsync parent, rename."""
    ...

def atomic_write_bytes(path: Path, contents: bytes) -> None:
    ...
```

Delete the duplicate definitions in:

* `src/alphamind/config/snapshot.py:129` — replace with `from alphamind._kernel.atomic_io import atomic_write_text`
* `src/alphamind/distillation/calibration_snapshot.py:152` — same
* `src/alphamind/config/control_handlers/profile_switch.py:123` (post-01b location) — same

### 3\. Update consumers

The 22 importers of `data_sources/_common` continue to work via the curated `_common/__init__.py` re-exports. Optionally retarget high-leverage consumers (those importing 4+ symbols from `_common`) to import directly from the focused submodules.

### Out of scope

The `HeadlineType` move's downstream cascade (analysis/qualitative_input.py etc.) is permitted but optional; the curated re-exports prevent breakage.

## Acceptance criteria

- [ ] `src/alphamind/data_sources/_common/` is a package with `retry.py`, `rate_limit.py`, `run_tracking.py`, `resume.py`, `universe.py`, `config.py`, `env_example.py`, plus curated `__init__.py`.
- [ ] `data_sources/_common.py` (the file) no longer exists; the package replaces it.
- [ ] `HeadlineType` lives in `data_sources/news/types.py`.
- [ ] `src/alphamind/_kernel/atomic_io.py` exists with `atomic_write_text`/`atomic_write_bytes`.
- [ ] Zero duplicate `_atomic_write` definitions across `config/snapshot.py`, `distillation/calibration_snapshot.py`, `config/control_handlers/profile_switch.py`.
- [ ] Backward-compat: existing `from alphamind.data_sources._common import X` patterns continue to work.
- [ ] `uv run ruff check .`, `uv run mypy`, `uv run pytest -n auto`, `uv run lint-imports` all pass.

## Verification

`grep -rn "def _atomic_write\|def atomic_write" src/` shows the single canonical definition in `_kernel/atomic_io.py`. `wc -l src/alphamind/data_sources/_common/*.py` shows each submodule ≤200 LOC.