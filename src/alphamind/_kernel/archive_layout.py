"""Canonical path helpers for the per-invocation archive layout.

Pins the date-partitioned directory convention documented in
``docs/architecture/infrastructure.md`` § Layer 2 invocation archive:

    <archive_root>/<YYYY-MM-DD>/<invocation_id>/<artifact-type-subdir>/

Replaces the legacy flat layout (``<archive_root>/invocations/<id>/``) that
coexisted under the same ``archive_root``.  All production callers that write
per-invocation artifacts should import :func:`invocation_archive_dir` and
callers that need to locate a prior invocation without knowing its ``as_of``
timestamp should use :func:`find_invocation_archive_dir`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

__all__ = ["find_invocation_archive_dir", "invocation_archive_dir"]


def invocation_archive_dir(
    *, archive_root: Path, as_of: datetime, invocation_id: str
) -> Path:
    """Return ``<archive_root>/<YYYY-MM-DD>/<invocation_id>/``.

    The date partition is ``as_of.astimezone(UTC).strftime('%Y-%m-%d')`` —
    matching the convention pinned by ``docs/architecture/infrastructure.md``
    § Layer 2 invocation archive and the existing
    ``distillation.orchestrator._invocation_archive_dir`` helper this
    function generalises.

    Callers append the artifact-type subdir (``distillation/``,
    ``analysis/``, ``decision/``, ``phase_outputs/``) themselves;
    per-invocation files (``progress.jsonl``, ``resolved_config.json``,
    ``data_calibration_state.json``) live at the per-invocation root.
    """
    date_part = as_of.astimezone(UTC).strftime("%Y-%m-%d")
    return archive_root / date_part / invocation_id


def find_invocation_archive_dir(
    *, archive_root: Path, invocation_id: str
) -> Path | None:
    """Return the per-invocation archive dir without knowing its ``as_of``.

    Globs ``<archive_root>/*/<invocation_id>`` so any date partition
    that carries this invocation matches.  Returns the resolved path on
    exactly one match; ``None`` on zero matches OR multiple matches
    (ambiguous — caller decides whether to fail or fall back).

    Use this from callers that don't have ``as_of`` available (e.g.
    the resume CLI loader, which references a PRIOR invocation it has
    no timestamp for).  Callers that own ``as_of`` should call
    :func:`invocation_archive_dir` directly to avoid the glob cost.
    """
    matches = [
        p for p in sorted(archive_root.glob(f"*/{invocation_id}")) if p.is_dir()
    ]
    if len(matches) != 1:
        return None
    return matches[0]
