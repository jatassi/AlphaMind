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
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from alphamind.execution.broker_adapter.protocols import (
    AccountStateQueriesP,
    CorporateActionsQueriesP,
)
from alphamind.scheduler.progress import ProgressEmitter

if TYPE_CHECKING:
    # ``SyntheticPortfolio`` is referenced only by the ``configure_debug_e2e``
    # type annotation; the runtime body never touches it. Hoisting the
    # import into ``TYPE_CHECKING`` keeps the lazy-import seam intact so
    # production callers importing ``configure_debug_e2e`` at module-load
    # time never pull ``portfolio.py`` — story 04's import-linter contract.
    from alphamind.scheduler.debug_e2e.portfolio import SyntheticPortfolio
    from alphamind.scheduler.debug_e2e.resume import ResumeContext

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
    * ``emitter_factory`` is invoked once per invocation with
      ``(invocation_id, as_of)`` so the JSONL emitter (story 02c) can open
      a fresh log file under the date-partitioned invocation archive
      directory (ALP-689 followup — layout migrated from the legacy flat
      ``invocations/<id>/`` to ``<YYYY-MM-DD>/<id>/``).
    * ``resume_context`` (ALP-693) carries the resume-from inputs the
      pipeline-composition runners (stories 04a / 04b) inspect to gate
      the replay short-circuit. ``None`` on a fresh debug-e2e run; a
      populated :class:`ResumeContext` when ``--resume-from`` is set.
      Pipeline modules read this off ``context.debug_e2e`` typed as
      ``object | None`` at the pipeline boundary so production code
      stays decoupled from the debug-e2e package (the import-linter
      contract ``debug-e2e-forbidden-in-production`` enforces this).
    """

    account_queries: AccountStateQueriesP
    ca_queries: CorporateActionsQueriesP
    emitter_factory: Callable[[str, datetime], ProgressEmitter]
    resume_context: ResumeContext | None = None


def configure_debug_e2e(
    *,
    archive_root: Path,
    portfolio: SyntheticPortfolio,
    resume_context: ResumeContext | None = None,
) -> DebugE2ESettings:
    """Construct the debug-e2e injection bundle.

    Called once from ``scheduler/__main__.py`` (story 04) when the
    ``--debug-e2e`` flag is set. The CLI selects ``portfolio`` —
    :data:`SYNTHETIC_PORTFOLIO` for the managed-portfolio fixture or
    :data:`FRESH_START_PORTFOLIO` (ALP-618) for the clean-slate variant —
    so the ``LogOnlyAccountStateQueries`` stand-in projects the same shape
    that the seeder writes into the debug DB.

    ``resume_context`` (ALP-693) is the validated :class:`ResumeContext`
    when ``--resume-from`` is set; ``None`` for fresh debug-e2e runs.
    The CLI calls :func:`alphamind.scheduler.debug_e2e.resume.load_resume_context`
    before this factory and passes the result through unchanged.

    The lazy imports below keep production callers' top-of-module
    ``from alphamind.scheduler.debug_e2e import configure_debug_e2e``
    from triggering module-load of the heavy submodules — story 04's
    import-linter contract forbids production code from reaching
    ``debug_e2e/`` at module-load time, and a lazy seam inside this
    function is what makes the contract holdable while still letting
    the CLI import the factory at top-of-module.
    """
    from alphamind._kernel.archive_layout import invocation_archive_dir
    from alphamind.scheduler.debug_e2e.broker import (
        LogOnlyAccountStateQueries,
        LogOnlyCorporateActionsQueries,
    )
    from alphamind.scheduler.debug_e2e.jsonl_emitter import JsonlProgressEmitter

    def make_emitter(invocation_id: str, as_of: datetime) -> ProgressEmitter:
        """Open the JSONL emitter at the date-partitioned archive location."""
        return JsonlProgressEmitter(
            path=invocation_archive_dir(
                archive_root=archive_root, as_of=as_of, invocation_id=invocation_id
            )
            / "progress.jsonl"
        )

    return DebugE2ESettings(
        account_queries=LogOnlyAccountStateQueries(portfolio),
        ca_queries=LogOnlyCorporateActionsQueries(),
        emitter_factory=make_emitter,
        resume_context=resume_context,
    )
