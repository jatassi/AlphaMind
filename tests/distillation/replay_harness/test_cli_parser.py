"""Verify the argparse parser for the replay harness CLI.

The parser was introduced in story 02 with the three core arguments
(`--candidate-config`, `--baseline-config`, `--regimes`); story 08 extended it
with `--fixture-store-root` and `--report-root` once the runner needed them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alphamind.distillation.replay_harness.cli import (
    DEFAULT_FIXTURE_STORE_ROOT,
    DEFAULT_REPORT_ROOT,
    build_parser,
)


def test_required_candidate_config_parses() -> None:
    args = build_parser().parse_args(["--candidate-config", "snapshot.yaml"])
    assert args.candidate_config == "snapshot.yaml"
    assert args.baseline_config is None


def test_missing_candidate_config_exits() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_baseline_config_parses_when_supplied() -> None:
    args = build_parser().parse_args(
        ["--candidate-config", "cand.yaml", "--baseline-config", "base.yaml"]
    )
    assert args.candidate_config == "cand.yaml"
    assert args.baseline_config == "base.yaml"


def test_regimes_split_into_list() -> None:
    args = build_parser().parse_args(
        ["--candidate-config", "cand.yaml", "--regimes", "low_vol,normal"]
    )
    assert args.regimes == ["low_vol", "normal"]


def test_regimes_default_to_all_four_canonical_labels() -> None:
    args = build_parser().parse_args(["--candidate-config", "cand.yaml"])
    assert args.regimes == ["low_vol", "normal", "elevated", "crisis"]


def test_fixture_store_root_defaults_to_data_replay_fixtures() -> None:
    args = build_parser().parse_args(["--candidate-config", "cand.yaml"])
    assert args.fixture_store_root == DEFAULT_FIXTURE_STORE_ROOT
    assert args.fixture_store_root == Path("data/replay_fixtures")


def test_report_root_defaults_to_data_replay_reports() -> None:
    args = build_parser().parse_args(["--candidate-config", "cand.yaml"])
    assert args.report_root == DEFAULT_REPORT_ROOT
    assert args.report_root == Path("data/replay_reports")


def test_fixture_store_root_overridable() -> None:
    args = build_parser().parse_args(
        ["--candidate-config", "cand.yaml", "--fixture-store-root", "/tmp/fix"]
    )
    assert args.fixture_store_root == Path("/tmp/fix")


def test_report_root_overridable() -> None:
    args = build_parser().parse_args(
        ["--candidate-config", "cand.yaml", "--report-root", "/tmp/reports"]
    )
    assert args.report_root == Path("/tmp/reports")
