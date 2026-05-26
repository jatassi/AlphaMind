"""Resume-from loader + ``ResumeContext`` central data model (story ALP-693).

Provides the substrate ``--resume-from <invocation-id>:<phase>`` rests on:

* :class:`ResumeContext` — frozen + slotted dataclass the orchestrator
  carries on ``RunInvocationContext.debug_e2e.resume_context``.  Stories
  04a / 04b's pipeline-composition runners read it off
  ``context.debug_e2e`` (typed as ``object | None`` at the pipeline
  boundary so production code stays decoupled from this module).
* :class:`ResumeValidationError` — typed exception class the CLI maps to
  exit code 2.  Single ``message: str`` arg keeps the failure-cause
  vocabulary operator-facing only.
* ``_PHASE_DEPENDENCIES`` — the hard-coded SDK-phase DAG (parent
  decision noted in story ALP-696's reading list as a "hardest-to-reverse
  decision").  One entry per SDK phase.  Module-private; consumed via
  :func:`phases_to_replay`.
* :func:`phases_to_replay` — pure-function transitive closure over
  ``_PHASE_DEPENDENCIES`` for a single target phase.  Computed once at
  load time and cached on the :class:`ResumeContext` so the per-replay
  check in stories 04a / 04b does not re-walk the DAG.
* :func:`load_resume_context` — argparse-time validator that builds a
  :class:`ResumeContext` from ``--resume-from`` inputs after probing the
  source archive on disk.  Raises :class:`ResumeValidationError` with a
  named cause for each rejection arm.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from alphamind.scheduler.debug_e2e.phase_outputs import (
    SDK_PHASE_NAMES,
    phase_output_path,
)

__all__ = [
    "ResumeContext",
    "ResumeValidationError",
    "load_resume_context",
    "phases_to_replay",
]


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResumeContext:
    """All the resume-from inputs the orchestrator and pipeline runners read.

    Constructed by :func:`load_resume_context` from validated CLI input;
    threaded onto :class:`alphamind.scheduler.debug_e2e.settings.DebugE2ESettings`
    as the ``resume_context`` field.  Pipeline modules never import this
    module — they read the context off ``context.debug_e2e`` which is
    typed as ``object | None`` at the pipeline boundary (production code
    stays decoupled from the debug-e2e package).

    * ``source_archive_dir`` — the per-invocation directory
      (``<archive-root>/invocations/<source-id>``) the replay short-circuit
      reads ``phase_outputs/<phase>.json`` files from.
    * ``resume_phase`` — the SDK phase to resume execution from.  One of
      :data:`alphamind.scheduler.debug_e2e.phase_outputs.SDK_PHASE_NAMES`.
    * ``phases_to_replay`` — frozen set of SDK phase names strictly upstream
      of ``resume_phase``.  Pre-computed at load time so the per-phase
      replay-gate check is a single ``in`` lookup.
    """

    source_archive_dir: Path
    resume_phase: str
    phases_to_replay: frozenset[str]


class ResumeValidationError(Exception):
    """Raised when ``--resume-from`` inputs cannot produce a valid
    :class:`ResumeContext`.

    The CLI catches this and exits with code 2, surfacing the message on
    stderr.  The message names the rejected input (missing directory,
    unknown phase, missing upstream phase output) so the operator can
    act without re-reading the runbook.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


# ---------------------------------------------------------------------------
# Phase-dependency DAG (hard-coded; parent decision)
# ---------------------------------------------------------------------------

#: Mapping each SDK phase to the set of SDK phases that must complete
#: before it can run.  Hard-coded here rather than reflected out of
#: ``run_analysis_pipeline`` / ``run_decision_pipeline`` — the dependency
#: graph is small (9 nodes, ~12 edges) and changes only when the pipeline
#: composition shape changes.  A unit test pins the shape so refactors
#: of pipeline composition surface here at unit-test time.
#:
#: Direct dependencies only — :func:`phases_to_replay` walks the
#: transitive closure.
_DOMAIN_RESEARCHERS: frozenset[str] = frozenset({"tech_semis", "financials", "energy"})

_PHASE_DEPENDENCIES: Mapping[str, frozenset[str]] = {
    # Three domain researchers run parallel under one TaskGroup; each has
    # no SDK upstream.
    "tech_semis": frozenset(),
    "financials": frozenset(),
    "energy": frozenset(),
    # Qualitative researcher runs parallel with the domain researchers.
    "qualitative": frozenset(),
    # Adaptive runs after the three domain researchers + qualitative.
    "adaptive": _DOMAIN_RESEARCHERS | {"qualitative"},
    # Synthesizer needs the four researchers + adaptive's adjustments.
    "synthesizer": _DOMAIN_RESEARCHERS | {"qualitative", "adaptive"},
    # Analyst + strategist run in parallel off synthesizer's output.
    "analyst": frozenset({"synthesizer"}),
    "strategist": frozenset({"synthesizer"}),
    # PM consumes analyst + strategist (which transitively pull
    # synthesizer + the four researchers).
    "pm": frozenset({"analyst", "strategist"}),
}


# ---------------------------------------------------------------------------
# Closure
# ---------------------------------------------------------------------------


def phases_to_replay(phase: str) -> frozenset[str]:
    """Return the frozen-set transitive closure of ``_PHASE_DEPENDENCIES[phase]``.

    The result is the set of SDK phases strictly upstream of ``phase``
    in the composition shape — every phase the replay short-circuit in
    stories 04a / 04b must hydrate from the source archive's
    ``phase_outputs/<upstream>.json`` files before running ``phase``.

    Raises ``ValueError`` naming the rejected phase if ``phase`` is not
    in :data:`_PHASE_DEPENDENCIES`.
    """
    if phase not in _PHASE_DEPENDENCIES:
        raise ValueError(
            f"{phase!r} is not a recognised SDK phase name. "
            f"Valid phases: {sorted(_PHASE_DEPENDENCIES)}"
        )

    closure: set[str] = set()
    frontier: list[str] = list(_PHASE_DEPENDENCIES[phase])
    while frontier:
        upstream = frontier.pop()
        if upstream in closure:
            continue
        closure.add(upstream)
        frontier.extend(_PHASE_DEPENDENCIES[upstream])
    return frozenset(closure)


def _earliest_valid_resume_target(*, present_phases: frozenset[str]) -> str | None:
    """Return the earliest SDK phase whose ``phases_to_replay`` closure is
    a subset of ``present_phases``.

    "Earliest" means most-upstream: the phase the operator could
    re-invoke ``--resume-from`` against given only the phase outputs the
    source archive actually covers.  Returns ``None`` if no SDK phase
    has all its upstreams present (e.g. an empty archive — the operator
    must restart from scratch).

    The "earliest" ordering follows the DAG: a phase ``p`` is earlier
    than ``q`` iff ``p`` is in ``phases_to_replay(q)``.  Phases at the
    same DAG depth are tie-broken by name to keep the message
    deterministic.
    """
    candidates: list[str] = []
    for candidate in sorted(SDK_PHASE_NAMES):
        if phases_to_replay(candidate) <= present_phases:
            candidates.append(candidate)
    if not candidates:
        return None
    # Pick the candidate with the smallest closure (most-upstream); break
    # ties on name for determinism.  ``analyst``/``strategist`` share a
    # closure and would tie at the synthesizer-or-deeper level; the
    # alphabetical fallback picks ``analyst`` consistently.
    candidates.sort(key=lambda p: (len(phases_to_replay(p)), p))
    return candidates[0]


def _phase_output_present(source_archive_dir: Path, phase: str) -> bool:
    """Return True iff ``phase_outputs/<phase>.json`` exists in the archive.

    Defensive against TOCTOU: a concurrent archive prune that removes the
    directory between the loader's :func:`Path.is_dir` check and this
    probe can cause :func:`Path.is_file` to raise ``OSError`` /
    ``PermissionError`` on some platforms (notably Windows when stat'ing
    a path under a deleted parent). Treat any such failure as "not
    present" so the loader returns a typed
    :class:`ResumeValidationError` rather than a raw traceback.
    """
    try:
        return phase_output_path(source_archive_dir, phase).is_file()
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_resume_context(
    *,
    archive_root: Path,
    invocation_id: str,
    phase: str,
) -> ResumeContext:
    """Validate the source archive and build the :class:`ResumeContext`.

    Argparse-time validator: every rejection surfaces as
    :class:`ResumeValidationError` so the CLI can ``parser.error()`` /
    exit 2 before any DB write.

    Raises :class:`ResumeValidationError` when:

    * ``<archive_root>/invocations/<invocation_id>/`` does not exist;
    * ``phase`` is not in :data:`SDK_PHASE_NAMES`;
    * any phase in the computed ``phases_to_replay(phase)`` lacks its
      ``phase_outputs/<phase>.json`` file in the source archive.  The
      message names the missing phase and the earliest valid resume
      target the source archive does cover.
    """
    source_archive_dir = archive_root / "invocations" / invocation_id
    try:
        archive_is_dir = source_archive_dir.is_dir()
    except OSError as e:
        # Defensive: a concurrently-removed parent directory can raise
        # PermissionError on Windows rather than returning False. Surface
        # as the typed validation error so the CLI exits 2 cleanly.
        msg = f"--resume-from: cannot stat source invocation directory {source_archive_dir}: {e}"
        raise ResumeValidationError(msg) from e
    if not archive_is_dir:
        msg = (
            f"--resume-from: source invocation directory not found: "
            f"{source_archive_dir} (invocation_id={invocation_id!r})"
        )
        raise ResumeValidationError(msg)

    if phase not in SDK_PHASE_NAMES:
        msg = f"--resume-from: unknown SDK phase {phase!r}. Valid phases: {sorted(SDK_PHASE_NAMES)}"
        raise ResumeValidationError(msg)

    required_upstreams = phases_to_replay(phase)
    missing = [
        p for p in sorted(required_upstreams) if not _phase_output_present(source_archive_dir, p)
    ]

    if missing:
        # Compute the earliest valid resume target the source archive
        # covers — operators iterate on this between failed attempts. The
        # hint must reflect what is ACTUALLY on disk across the whole
        # archive, not just the subset of phases we scanned as upstreams
        # of the failed target. Scan every SDK phase's output file here
        # so the hint never recommends a phase whose file is also absent.
        all_present = frozenset(
            p for p in SDK_PHASE_NAMES if _phase_output_present(source_archive_dir, p)
        )
        earliest = _earliest_valid_resume_target(present_phases=all_present)
        hint = (
            f"earliest valid resume target the source archive covers: "
            f"--resume-from {invocation_id}:{earliest}"
            if earliest is not None
            else "the source archive has no SDK phase outputs; restart from scratch"
        )
        msg = (
            f"--resume-from: source archive {source_archive_dir} is "
            f"missing upstream phase output(s) {missing} required to "
            f"resume from {phase!r}. {hint}"
        )
        raise ResumeValidationError(msg)

    return ResumeContext(
        source_archive_dir=source_archive_dir,
        resume_phase=phase,
        phases_to_replay=required_upstreams,
    )
