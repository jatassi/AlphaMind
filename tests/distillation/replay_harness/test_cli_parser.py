"""Verify the argparse parser for the replay harness CLI."""

from __future__ import annotations

import pytest

from alphamind.distillation.replay_harness.cli import build_parser


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
