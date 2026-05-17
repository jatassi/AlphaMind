"""Debug-e2e runtime settings — broker overrides + progress-emitter factory.

Exposes the two-symbol public surface the design doc (§ 3 — package
layout; § 6 P9 — small public surface) prescribes:

* :class:`DebugE2ESettings` — frozen bundle the orchestrator inspects to
  detect debug-e2e mode (P3 — presence on
  :class:`alphamind.scheduler.run_context.RunInvocationContext` is the
  sole indicator; no parallel boolean flag).
* :func:`configure_debug_e2e` — factory the CLI entrypoint
  (``scheduler/__main__.py``, story 04) calls once to construct the
  bundle. Heavy submodule imports are deferred to inside the function
  body so production callers' module-load-time import graph never
  reaches ``debug_e2e/`` — story 04's import-linter contract enforces
  this.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from alphamind.execution.broker_adapter.protocols import (
    AccountStateQueriesP,
    CorporateActionsQueriesP,
)
from alphamind.scheduler.progress import ProgressEmitter

__all__ = ["DebugE2ESettings", "configure_debug_e2e"]


@dataclass(frozen=True, slots=True)
class DebugE2ESettings:
    """All debug-e2e injections in one bundle.

    Presence on :class:`alphamind.scheduler.run_context.RunInvocationContext`
    is the single indicator that the orchestrator is in debug-e2e mode
    (P3 — no parallel boolean flag). The orchestrator inspects each
    field in turn:

    * ``account_queries`` / ``ca_queries`` substitute the Alpaca-backed
      broker-adapter classes ``gather_phase1_inputs`` would otherwise
      construct.
    * ``emitter_factory`` is invoked once per invocation with the
      ``invocation_id`` so the JSONL emitter (story 02c) can open a
      fresh log file under the invocation's archive directory.
    """

    account_queries: AccountStateQueriesP
    ca_queries: CorporateActionsQueriesP
    emitter_factory: Callable[[str], ProgressEmitter]


def configure_debug_e2e(*, archive_root: Path) -> DebugE2ESettings:
    """Construct the debug-e2e injection bundle.

    Called once from ``scheduler/__main__.py`` (story 04) when the
    ``--debug-e2e`` flag is set. The lazy imports below keep production
    callers' top-of-module ``from alphamind.scheduler.debug_e2e import
    configure_debug_e2e`` from triggering module-load of the heavy
    submodules — story 04's import-linter contract forbids production
    code from reaching ``debug_e2e/`` at module-load time, and a lazy
    seam inside this function is what makes the contract holdable while
    still letting the CLI import the factory at top-of-module.
    """
    from alphamind.scheduler.debug_e2e.broker import (
        LogOnlyAccountStateQueries,
        LogOnlyCorporateActionsQueries,
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
