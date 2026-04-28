"""
Tests for alphamind.collector.__main__ dispatcher.

Verifies that each subcommand routes to the correct module-level function.
"""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Slice 7 — `bootstrap` subcommand routes to bootstrap.run_all
# ---------------------------------------------------------------------------


def test_bootstrap_subcommand_routes_to_run_all() -> None:
    with (
        patch("alphamind.collector.__main__.bootstrap_run_all") as mock_bootstrap,
        patch("alphamind.collector.__main__._parse_args") as mock_parse,
    ):
        mock_parse.return_value.subcommand = "bootstrap"
        mock_parse.return_value.only = None

        from alphamind.collector.__main__ import main

        main()

    mock_bootstrap.assert_called_once_with(only_vendor=None)


def test_bootstrap_subcommand_passes_only_vendor() -> None:
    with (
        patch("alphamind.collector.__main__.bootstrap_run_all") as mock_bootstrap,
        patch("alphamind.collector.__main__._parse_args") as mock_parse,
    ):
        mock_parse.return_value.subcommand = "bootstrap"
        mock_parse.return_value.only = "polygon"

        from alphamind.collector.__main__ import main

        main()

    mock_bootstrap.assert_called_once_with(only_vendor="polygon")


# ---------------------------------------------------------------------------
# Slice 8 — `catch-up` subcommand routes to catchup.run_all
# ---------------------------------------------------------------------------


def test_catchup_subcommand_routes_to_catchup_run_all() -> None:
    with (
        patch("alphamind.collector.__main__.catchup_run_all") as mock_catchup,
        patch("alphamind.collector.__main__._parse_args") as mock_parse,
    ):
        mock_parse.return_value.subcommand = "catch-up"
        mock_parse.return_value.only = None
        mock_parse.return_value.skip = None

        from alphamind.collector.__main__ import main

        main()

    mock_catchup.assert_called_once_with(only=None, skip=None)


# ---------------------------------------------------------------------------
# Slice 9 — `run` subcommand routes to scheduler.start_blocking
# ---------------------------------------------------------------------------


def test_run_subcommand_routes_to_start_blocking() -> None:
    with (
        patch("alphamind.collector.__main__.start_blocking") as mock_start,
        patch("alphamind.collector.__main__._parse_args") as mock_parse,
    ):
        mock_parse.return_value.subcommand = "run"

        from alphamind.collector.__main__ import main

        main()

    mock_start.assert_called_once_with()


# ---------------------------------------------------------------------------
# Slice 10 — `--help` lists all three subcommands
# ---------------------------------------------------------------------------


def test_help_lists_all_three_subcommands() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alphamind.collector", "--help"],
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr
    assert "run" in output
    assert "bootstrap" in output
    assert "catch-up" in output
