"""Per-phase output persistence substrate for ``--debug-e2e`` resume.

Story ALP-690. Provides:

* ``SDK_PHASE_NAMES`` — frozenset of the 9 SDK-phase names that produce
  persisted outputs.  Deterministic phases (``snapshot_assembly``,
  ``phase1``, etc.) are excluded per parent decision (H).
* ``phase_output_path`` — compute the canonical on-disk path for a given
  phase's output file.
* ``write_phase_output`` — atomically serialize a Pydantic model to the
  canonical path using ``alphamind._kernel.atomic_io.atomic_write_text``.
* ``read_phase_output`` — deserialize and validate a Pydantic model from
  the canonical path; raises ``FileNotFoundError`` when absent.

These four symbols are the substrate that stories 02a / 02b (emission
hooks) and 04a / 04b (replay short-circuit) build on.
"""

from __future__ import annotations

from pathlib import Path

import pydantic

from alphamind._kernel.atomic_io import atomic_write_text

__all__ = [
    "SDK_PHASE_NAMES",
    "phase_output_path",
    "read_phase_output",
    "write_phase_output",
]

# ---------------------------------------------------------------------------
# Phase-name whitelist (§1)
# ---------------------------------------------------------------------------

#: Exactly the 9 SDK phases that write ``phase_outputs/<phase>.json`` on
#: completion.  Deterministic phases (``phase1``, ``snapshot_assembly``,
#: ``distillation``, ``pre_processor``, ``phase2``) are excluded — they are
#: always re-computed on resume (per design doc § 4).
SDK_PHASE_NAMES: frozenset[str] = frozenset(
    {
        "tech_semis",
        "financials",
        "energy",
        "qualitative",
        "adaptive",
        "synthesizer",
        "analyst",
        "strategist",
        "pm",
    }
)


# ---------------------------------------------------------------------------
# Path computation helper (§2)
# ---------------------------------------------------------------------------


def phase_output_path(archive_dir: Path, phase: str) -> Path:
    """Return ``<archive_dir>/phase_outputs/<phase>.json``.

    Raises ``ValueError`` naming the invalid phase if ``phase`` is not in
    ``SDK_PHASE_NAMES``.  ``archive_dir`` is the per-invocation directory
    (i.e. ``<archive-root>/invocations/<invocation-id>/``), not the archive
    root.
    """
    if phase not in SDK_PHASE_NAMES:
        raise ValueError(
            f"{phase!r} is not a recognised SDK phase name. Valid phases: {sorted(SDK_PHASE_NAMES)}"
        )
    return archive_dir / "phase_outputs" / f"{phase}.json"


# ---------------------------------------------------------------------------
# Atomic write helper (§3)
# ---------------------------------------------------------------------------


def write_phase_output(
    *,
    archive_dir: Path,
    phase: str,
    model: pydantic.BaseModel,
) -> None:
    """Serialize ``model`` to JSON and atomically write to
    ``phase_output_path(archive_dir, phase)``.

    Creates the ``phase_outputs/`` subdirectory if absent.  Uses
    ``alphamind._kernel.atomic_io.atomic_write_text`` for the
    staging-and-rename guarantee — a crash mid-write never produces a
    partial file.
    """
    target = phase_output_path(archive_dir, phase)
    # atomic_write_text already calls target.parent.mkdir(parents=True,
    # exist_ok=True), so explicit mkdir is not needed here.
    atomic_write_text(target, model.model_dump_json())


# ---------------------------------------------------------------------------
# Read helper (§4)
# ---------------------------------------------------------------------------


def read_phase_output[ModelT: pydantic.BaseModel](
    *,
    archive_dir: Path,
    phase: str,
    model_cls: type[ModelT],
) -> ModelT:
    """Read ``phase_output_path(archive_dir, phase)`` and validate via
    ``model_cls.model_validate_json(...)``.

    Raises ``FileNotFoundError`` if the file is absent (no swallow-and-return-
    None; no silent default).  Propagates Pydantic's ``ValidationError`` on
    schema mismatch.
    """
    target = phase_output_path(archive_dir, phase)
    raw = target.read_text(encoding="utf-8")  # raises FileNotFoundError when absent
    return model_cls.model_validate_json(raw)
