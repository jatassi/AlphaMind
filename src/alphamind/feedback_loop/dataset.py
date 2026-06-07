"""WindowDataset + loader shell for the analytics spine (ALP-882 story 05).

The **imperative shell** of the functional-core / imperative-shell split (P1): the one
DB-touching function in the analytics layer is :func:`load_window`. It composes the
existing repository read-helpers into a typed, in-memory, load-once
:class:`WindowDataset` that the metric cores (``feedback_loop.metrics``) compute over
**purely** — no metric ever touches the session.

The dataset is a frozen dataclass composed of per-domain sub-bundles. Two extension
sub-bundles are **pre-declared empty** here so later stories fill distinct, non-colliding
regions of this module:

* ``refs`` — citation reference IDs parsed from agent-call output artifacts. Filled by
  story 06d via the :func:`_load_refs` hook (a stub returning empty here).
* ``replays`` — counterfactual-replay records. Filled by story 06e via the
  :func:`_load_replays` hook (a stub returning empty here, gated on ALP-129).

``feedback_loop`` is read-only over trading state: this module only reads, and never
imports ``execution`` (enforced by ``.importlinter``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from alphamind.state.repository.activity_log_queries import read_recent_pm_decision_log
from alphamind.state.repository.agent_calls_queries import read_agent_calls_in_window
from alphamind.state.repository.validation_queries import read_pending_validations

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from alphamind.feedback_loop.validation.records import ValidationRecord
    from alphamind.portfolio_state.events.activity_log import ActivityLogEntry
    from alphamind.state.tables.agent_calls import AgentCallRecord

# ---------------------------------------------------------------------------
# Loader configuration
# ---------------------------------------------------------------------------

#: Size of the PM-decision sliding window the loader pulls for the dataset
#: (``read_recent_pm_decision_log``'s ``sliding_window_invocations``). The
#: analytics layer reads the most-recent decisions adjacent to the window; this
#: is a definitional read-depth, not an operator knob.
_PM_DECISION_SLIDING_WINDOW = 50


# ---------------------------------------------------------------------------
# Per-domain sub-bundles
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RefsBundle:
    """Citation reference IDs and their chains (story 06d fills this).

    Pre-declared empty so the citation-chain metrics can build against the shape
    without 06d and this story colliding on ``dataset.py``.
    """

    citations: tuple[object, ...] = ()


@dataclass(frozen=True, slots=True)
class ReplaysBundle:
    """Counterfactual-replay records for the window (story 06e fills this).

    Pre-declared empty and gated on ALP-129; the PM-accuracy / modification-
    effectiveness metrics read it. Absent until 06e wires the loader hook.
    """

    replays: tuple[object, ...] = ()


# ---------------------------------------------------------------------------
# WindowDataset
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WindowDataset:
    """Typed, in-memory, load-once view of every record a window's metrics need.

    Built once by :func:`load_window`; metric cores read it without further I/O.

    **Per-bundle window semantics differ — read carefully when computing windowed
    metrics (``start`` inclusive, ``end`` exclusive):**

    * ``agent_calls`` — strictly bounded to ``[start, end)`` (via the invocation join).
    * ``pm_decision_log`` — the most-recent ``_PM_DECISION_SLIDING_WINDOW`` decisions
      (a count-based read-depth), **not** clipped to ``[start, end)``: it may include
      decisions outside the window or omit in-window decisions beyond that depth.
    * ``validations`` — all currently-pending validations (point-in-time), **not**
      window-bounded.

    A metric needing a strict per-window slice of ``pm_decision_log`` / ``validations``
    must filter by timestamp itself.
    """

    start: datetime
    end: datetime
    agent_calls: tuple[AgentCallRecord, ...]
    pm_decision_log: tuple[ActivityLogEntry, ...]
    validations: tuple[ValidationRecord, ...]
    # Pre-declared extension sub-bundles — empty until 06d / 06e fill them.
    refs: RefsBundle = field(default_factory=RefsBundle)
    replays: ReplaysBundle = field(default_factory=ReplaysBundle)


# ---------------------------------------------------------------------------
# Extension loader hooks (pre-declared seams; stubbed empty this story)
# ---------------------------------------------------------------------------


async def _load_refs(
    session: AsyncSession,  # noqa: ARG001 — pre-declared seam; story 06d uses it
    start: datetime,  # noqa: ARG001 — pre-declared seam; story 06d uses it
    end: datetime,  # noqa: ARG001 — pre-declared seam; story 06d uses it
) -> RefsBundle:
    """Citation-reference sub-bundle loader hook (story 06d fills this).

    Stubbed to return an empty :class:`RefsBundle`. Story 06d parses agent-call
    output artifacts here; the seam is pre-declared so that work edits only this
    function and :class:`RefsBundle`, not the rest of the loader.
    """
    return RefsBundle()


async def _load_replays(
    session: AsyncSession,  # noqa: ARG001 — pre-declared seam; story 06e uses it
    start: datetime,  # noqa: ARG001 — pre-declared seam; story 06e uses it
    end: datetime,  # noqa: ARG001 — pre-declared seam; story 06e uses it
) -> ReplaysBundle:
    """Counterfactual-replay sub-bundle loader hook (story 06e fills this).

    Stubbed to return an empty :class:`ReplaysBundle`. Story 06e reads
    ``counterfactual_replays`` here (gated on ALP-129); the seam is pre-declared
    so that work edits only this function and :class:`ReplaysBundle`.
    """
    return ReplaysBundle()


# ---------------------------------------------------------------------------
# The loader — the only DB-touching function in the analytics layer
# ---------------------------------------------------------------------------


async def load_window(session: AsyncSession, start: datetime, end: datetime) -> WindowDataset:
    """Compose the repository read-helpers into a :class:`WindowDataset`.

    Reads every record the window's metrics need in one pass:

    * ``agent_calls`` whose owning invocation started in ``[start, end)``.
    * the recent PM-decision activity-log sliding window.
    * pending validation contracts (the validation-discipline read surface).
    * the ``refs`` / ``replays`` extension sub-bundles via their hooks (empty
      this story).

    ``validation_queries`` exposes synchronous ``Session``-based helpers; they are
    bridged onto this async session via :meth:`AsyncSession.run_sync` — SQLAlchemy
    2.x's canonical sync bridge over the same connection.
    """
    agent_calls = await read_agent_calls_in_window(session, start, end)
    pm_decision_log = await read_recent_pm_decision_log(session, _PM_DECISION_SLIDING_WINDOW)
    validations = await session.run_sync(
        lambda sync_session: read_pending_validations(sync_session)
    )
    refs = await _load_refs(session, start, end)
    replays = await _load_replays(session, start, end)
    return WindowDataset(
        start=start,
        end=end,
        agent_calls=agent_calls,
        pm_decision_log=pm_decision_log,
        validations=validations,
        refs=refs,
        replays=replays,
    )


__all__ = [
    "RefsBundle",
    "ReplaysBundle",
    "WindowDataset",
    "load_window",
]
