"""Command-line interface for the distillation replay harness.

Story 02 lands the argparse layout and a not-yet-implemented sentinel.
Subsequent stories (03 fixture loader, 04 config loader, 05 engine, 07
report renderer, 08 wiring) replace the sentinel body with the real runner;
the parser surface defined here stays.

See `docs/design/02-distillation-layer/replay-harness.md` S Activation.
"""

from __future__ import annotations

import argparse
import sys

from alphamind.config.models.regimes import Regime

CANONICAL_REGIMES: tuple[str, ...] = tuple(r.value for r in Regime)

NOT_YET_IMPLEMENTED_MESSAGE = "replay harness not yet implemented; story 08 wires the runner"
NOT_YET_IMPLEMENTED_EXIT_CODE = 2


def _split_regimes(value: str) -> list[str]:
    return value.split(",")


def build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser for the replay harness CLI.

    Argument surface mirrors `replay-harness.md` S Activation verbatim:
    `--candidate-config`, `--baseline-config`, `--regimes`.
    """
    parser = argparse.ArgumentParser(
        prog="python -m alphamind.distillation.replay_harness",
        description=(
            "Re-run the deterministic distillation layer against archived "
            "fixtures under a candidate config and emit a per-regime "
            "flag-rate report. See docs/design/02-distillation-layer/"
            "replay-harness.md."
        ),
    )
    parser.add_argument(
        "--candidate-config",
        required=True,
        metavar="PATH",
        help="Path to a config/distillation.yaml snapshot to replay against.",
    )
    parser.add_argument(
        "--baseline-config",
        default=None,
        metavar="PATH",
        help=(
            "Optional path to a second config/distillation.yaml snapshot. "
            "When supplied, the harness runs both configs against the same "
            "fixture and emits a side-by-side diff report."
        ),
    )
    parser.add_argument(
        "--regimes",
        type=_split_regimes,
        default=list(CANONICAL_REGIMES),
        metavar="LABELS",
        help=(
            "Comma-separated subset of canonical regime labels "
            "(low_vol,normal,elevated,crisis). Default: all four."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and emit the not-yet-implemented sentinel.

    Returns exit code 2 — a "not implemented" sentinel deliberately distinct
    from 0 (success after report write) and 1 (real error). Story 08 replaces
    the body with the real runner.
    """
    build_parser().parse_args(argv)
    print(NOT_YET_IMPLEMENTED_MESSAGE, file=sys.stderr)
    return NOT_YET_IMPLEMENTED_EXIT_CODE
