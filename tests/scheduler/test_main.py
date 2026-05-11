"""Tests for the ``python -m alphamind.scheduler`` CLI entry point (story 01).

We exercise the CLI shim directly via ``main(argv)`` and assert on
behaviour at the parsing + dispatch layer; the actual daemon loop and
DB-row inserts are exercised end-to-end by the live verify script in
story 05 and the live invocation tests in story 03b.
"""

from __future__ import annotations

import pytest

from alphamind.scheduler.__main__ import main


class TestCliRunOnceNotImplemented:
    def test_run_once_raises_not_implemented_after_argparse(self) -> None:
        with pytest.raises(NotImplementedError, match="run_invocation not yet wired"):
            main(
                argv=[
                    "run",
                    "--once",
                    "market_hours_rolling",
                    "--reason",
                    "test",
                    "--mode",
                    "paper",
                ]
            )

    def test_run_once_rejects_unknown_run_type(self) -> None:
        # Bad <run_type> token surfaces as SystemExit (argparse default for
        # ``choices``) — the CLI surface refuses it before the
        # NotImplementedError branch.
        with pytest.raises(SystemExit):
            main(
                argv=[
                    "run",
                    "--once",
                    "definitely_not_a_run_type",
                    "--reason",
                    "test",
                ]
            )


class TestCliArgparseSurface:
    def test_no_arguments_exits_non_zero(self) -> None:
        with pytest.raises(SystemExit):
            main(argv=[])

    def test_unknown_subcommand_exits_non_zero(self) -> None:
        with pytest.raises(SystemExit):
            main(argv=["nope"])

    def test_run_once_requires_reason(self) -> None:
        with pytest.raises(SystemExit):
            main(argv=["run", "--once", "market_hours_rolling"])
