# 03 — DebugE2ESettings + configure_debug_e2e bundle

## Goal

Implement `scheduler/debug_e2e/settings.py` with the `DebugE2ESettings` frozen dataclass and `configure_debug_e2e(*, archive_root)` factory; update `scheduler/debug_e2e/__init__.py` to re-export the two-symbol public surface (`configure_debug_e2e`, `DebugE2ESettings`). The factory uses lazy imports inside the function body so the production-side `__main__.py` import of `configure_debug_e2e` only triggers the heavy module loads inside the function — not at module-load time, where the story-04 import-linter contract would forbid it.

## Reading

* `docs/design/debug-e2e-mode.md` §§ 3 (Package layout — `settings.py` + `__init__.py`), 4 (Types — `DebugE2ESettings` shape), 6 (Hardest-to-reverse decisions — why P9 / small public surface)
* `ALP-498` (02b) — provides `LogOnlyAccountStateQueries`, `LogOnlyCorporateActionsQueries`, `SYNTHETIC_PORTFOLIO`
* `ALP-499` (02c) — provides `JsonlProgressEmitter`

## Depends on

* `ALP-498` (02b — Synthetic portfolio + log-only broker)
* `ALP-499` (02c — JSONL progress emitter)

## Scope

In scope: `src/alphamind/scheduler/debug_e2e/settings.py` (new) and `src/alphamind/scheduler/debug_e2e/__init__.py` (re-exports). Tests at `tests/scheduler/debug_e2e/test_settings.py`.

### 1\. `scheduler/debug_e2e/settings.py`

```python
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from alphamind.execution.broker_adapter.protocols import (
    AccountStateQueriesP, CorporateActionsQueriesP,
)
from alphamind.scheduler.progress import ProgressEmitter


@dataclass(frozen=True, slots=True)
class DebugE2ESettings:
    """All debug-e2e injections in one bundle.

    Presence on RunInvocationContext signals debug-e2e mode (P3 — no
    parallel boolean flag).
    """
    account_queries: AccountStateQueriesP
    ca_queries: CorporateActionsQueriesP
    emitter_factory: Callable[[str], ProgressEmitter]


def configure_debug_e2e(*, archive_root: Path) -> DebugE2ESettings:
    """Construct the debug-e2e injection bundle. Called once at CLI entry."""
    # Lazy imports keep production callers from import-loading debug_e2e
    # at module-load time (story 04's import-linter contract enforces this).
    from alphamind.scheduler.debug_e2e.broker import (
        LogOnlyAccountStateQueries, LogOnlyCorporateActionsQueries,
    )
    from alphamind.scheduler.debug_e2e.jsonl_emitter import JsonlProgressEmitter
    from alphamind.scheduler.debug_e2e.portfolio import SYNTHETIC_PORTFOLIO

    def make_emitter(invocation_id: str) -> ProgressEmitter:
        return JsonlProgressEmitter(
            path=archive_root / "invocations" / invocation_id / "progress.jsonl"
        )

    return DebugE2ESettings(
        account_queries=LogOnlyAccountStateQueries(SYNTHETIC_PORTFOLIO),
        ca_queries=LogOnlyCorporateActionsQueries(),
        emitter_factory=make_emitter,
    )
```

### 2\. `scheduler/debug_e2e/__init__.py`

Re-export only the two-symbol public surface (P9 — small public surface):

```python
from alphamind.scheduler.debug_e2e.settings import (
    DebugE2ESettings, configure_debug_e2e,
)

__all__ = ["DebugE2ESettings", "configure_debug_e2e"]
```

The seeder (`wipe_and_seed`) and the underlying broker / emitter classes are NOT re-exported here — story 04's CLI wiring imports them directly from their submodules where needed.

### Out of scope

* CLI integration / orchestrator wiring (story 04).
* The import-linter contract forbidding production imports of `debug_e2e/` (story 04).

## Acceptance criteria

- [ ] `src/alphamind/scheduler/debug_e2e/settings.py` exposes `DebugE2ESettings` frozen-dataclass and `configure_debug_e2e(*, archive_root)` factory.
- [ ] `DebugE2ESettings` has exactly three fields: `account_queries: AccountStateQueriesP`, `ca_queries: CorporateActionsQueriesP`, `emitter_factory: Callable[[str], ProgressEmitter]`.
- [ ] `DebugE2ESettings` is `@dataclass(frozen=True, slots=True)`.
- [ ] `configure_debug_e2e(archive_root=tmp_path)` returns a `DebugE2ESettings` whose `account_queries` is a `LogOnlyAccountStateQueries` instance, `ca_queries` is a `LogOnlyCorporateActionsQueries` instance, and `emitter_factory("inv-test")` returns a `JsonlProgressEmitter` writing to `<archive_root>/invocations/inv-test/progress.jsonl`.
- [ ] `src/alphamind/scheduler/debug_e2e/__init__.py` re-exports only `DebugE2ESettings` and `configure_debug_e2e` (verified by inspecting `__all__`).
- [ ] `tests/scheduler/debug_e2e/test_settings.py` covers the factory construction, field shape, and emitter-factory output path.
- [ ] `uv run pytest tests/scheduler/debug_e2e/ -n auto` passes; full linter chain clean.

## Verification

`uv run pytest tests/scheduler/debug_e2e/test_settings.py -n auto` passes. Linter chain clean.