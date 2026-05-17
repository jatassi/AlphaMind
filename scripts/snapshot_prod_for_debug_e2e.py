"""Snapshot the production DB into a debug-e2e DB for verification runs.

The debug-e2e pipeline (parent ALP-493) wipes-and-seeds the state-persistence
subset on every invocation but assumes the *data layer* — ``asset_universe``,
``sector_classification``, distillation calibration baselines, news clusters,
the event calendar, regime-adaptation state, and the rest of the
collector-populated tables — is already present. Production-faithful means
that data layer is the live production state, not a synthetic fixture.

This script materializes that assumption by copying the production SQLite
file (``data/alphamind.db`` or whatever ``DATABASE_PATH`` resolves to) onto
a ``-debug-e2e.db`` target. ``wipe_and_seed`` then runs over the snapshot
on each invocation, clearing the state-persistence rows and overlaying the
synthetic portfolio while every data-layer FK target stays intact.

Safety guards:

* The target must end with ``-debug-e2e.db`` (the same suffix
  :func:`alphamind.scheduler.debug_e2e.seed.wipe_and_seed` enforces). Any
  other path is refused.
* An existing target is preserved unless ``--force`` is passed. Snapshot
  refresh is intentional, not implicit.
* Production and target are normalized to absolute paths before the
  compare so symlink / relative-path footguns can't collapse the two.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

_log = logging.getLogger(__name__)

_DEBUG_SUFFIX = "-debug-e2e.db"
_DEFAULT_SOURCE = Path("data/alphamind.db")
_DEFAULT_TARGET = Path("data/alphamind-debug-e2e.db")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Snapshot the production DB to a debug-e2e DB. The target file is "
            "the bootstrap DB the debug-e2e mode wipes-and-seeds on every "
            "invocation."
        )
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=_DEFAULT_SOURCE,
        help=f"Source DB path. Defaults to {_DEFAULT_SOURCE}.",
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=_DEFAULT_TARGET,
        help=(f"Target DB path. Must end with '{_DEBUG_SUFFIX}'. Defaults to {_DEFAULT_TARGET}."),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing target without prompting.",
    )
    return parser


def snapshot(source: Path, target: Path, *, force: bool) -> None:
    """Copy ``source`` to ``target`` after safety checks.

    Raises :class:`RuntimeError` with an actionable message on guard
    violation; raises :class:`FileNotFoundError` if the source is absent.
    """
    if target.name != target.name.rstrip() or not target.name.endswith(_DEBUG_SUFFIX):
        msg = (
            f"refusing to snapshot to {target!s}: target basename must end "
            f"with {_DEBUG_SUFFIX!r} so wipe_and_seed's safety guard can "
            "later wipe it without risk to production data."
        )
        raise RuntimeError(msg)

    source_abs = source.resolve()
    target_abs = target.resolve()
    if source_abs == target_abs:
        msg = f"refusing to snapshot {source!s} onto itself"
        raise RuntimeError(msg)

    if not source.exists():
        msg = f"source DB does not exist: {source!s}"
        raise FileNotFoundError(msg)

    if target.exists() and not force:
        msg = (
            f"target {target!s} already exists; pass --force to overwrite. "
            "The existing target may carry a previously-seeded debug-e2e "
            "state — confirm you want to replace it before re-running."
        )
        raise RuntimeError(msg)

    target.parent.mkdir(parents=True, exist_ok=True)
    _log.info("copying %s -> %s", source_abs, target_abs)
    shutil.copy2(source_abs, target_abs)
    size_mb = target_abs.stat().st_size / (1024 * 1024)
    _log.info("snapshot complete (%.1f MB)", size_mb)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _build_parser().parse_args(argv)
    try:
        snapshot(args.source, args.target, force=args.force)
    except (RuntimeError, FileNotFoundError) as exc:
        _log.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
