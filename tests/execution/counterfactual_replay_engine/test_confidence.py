"""Behavior tests for the confidence classifier (ALP-560).

Drives :func:`classify_confidence` through each demotion path documented in
``docs/design/05-execution-layer/counterfactual-replay-engine.md`` § Step 5,
encoding parent decision (D): at 15-minute production resolution, same-bar
target-and-stop ambiguity demotes to ``Confidence.MEDIUM`` baseline rather than
the design's minute-bar ``Confidence.LOW``.
"""

from __future__ import annotations

from dataclasses import replace

from alphamind.execution.counterfactual_replay_engine.confidence import (
    ConfidenceSignals,
    classify_confidence,
    replay_engine_version,
)
from alphamind.state.tables.counterfactual_replays import Confidence


def _clean_equity_signals() -> ConfidenceSignals:
    """All-clean equity signals — the HIGH baseline each test perturbs."""
    return ConfidenceSignals(
        instrument_kind="equity",
        same_bar_ambiguity=False,
        bar_coverage_complete=True,
        liquidity_within_typical_envelope=True,
        spread_within_typical_envelope=True,
        iv_lag_minutes_entry=None,
        iv_lag_minutes_exit=None,
        iv_lag_threshold_minutes=None,
    )


def _clean_option_signals() -> ConfidenceSignals:
    """All-clean option signals — fresh IV within the threshold at both ends."""
    return ConfidenceSignals(
        instrument_kind="option",
        same_bar_ambiguity=False,
        bar_coverage_complete=True,
        liquidity_within_typical_envelope=True,
        spread_within_typical_envelope=True,
        iv_lag_minutes_entry=10.0,
        iv_lag_minutes_exit=10.0,
        iv_lag_threshold_minutes=60.0,
    )


def test_equity_happy_path_is_high() -> None:
    assert classify_confidence(_clean_equity_signals()) is Confidence.HIGH


def test_replay_engine_version_is_v2() -> None:
    assert replay_engine_version == "v2"


def test_same_bar_ambiguity_alone_demotes_to_medium_not_low() -> None:
    # Parent decision (D): at 15-min production resolution this is the baseline
    # demotion to MEDIUM, not the design's minute-bar LOW.
    signals = replace(_clean_equity_signals(), same_bar_ambiguity=True)
    assert classify_confidence(signals) is Confidence.MEDIUM


def test_incomplete_coverage_alone_demotes_to_medium() -> None:
    signals = replace(_clean_equity_signals(), bar_coverage_complete=False)
    assert classify_confidence(signals) is Confidence.MEDIUM


def test_out_of_envelope_spread_demotes_to_medium() -> None:
    signals = replace(_clean_equity_signals(), spread_within_typical_envelope=False)
    assert classify_confidence(signals) is Confidence.MEDIUM


def test_thin_liquidity_demotes_to_low() -> None:
    signals = replace(_clean_equity_signals(), liquidity_within_typical_envelope=False)
    assert classify_confidence(signals) is Confidence.LOW


def test_coverage_gap_plus_ambiguity_demotes_to_low() -> None:
    # A gap coinciding with an ambiguous bar makes the trigger genuinely
    # uncertain — LOW wins over the same-bar MEDIUM baseline.
    signals = replace(
        _clean_equity_signals(),
        bar_coverage_complete=False,
        same_bar_ambiguity=True,
    )
    assert classify_confidence(signals) is Confidence.LOW


def test_option_happy_path_is_high() -> None:
    assert classify_confidence(_clean_option_signals()) is Confidence.HIGH


def test_stale_iv_at_entry_demotes_option_to_low() -> None:
    signals = replace(_clean_option_signals(), iv_lag_minutes_entry=90.0)
    assert classify_confidence(signals) is Confidence.LOW


def test_stale_iv_at_exit_demotes_option_to_low() -> None:
    signals = replace(_clean_option_signals(), iv_lag_minutes_exit=90.0)
    assert classify_confidence(signals) is Confidence.LOW


def test_iv_lag_demotion_fires_only_for_options() -> None:
    # An equity proposal whose (nonsensical) IV lag exceeds the threshold must
    # not demote — the IV rules are gated on instrument_kind == "option".
    signals = replace(
        _clean_equity_signals(),
        iv_lag_minutes_entry=90.0,
        iv_lag_minutes_exit=90.0,
        iv_lag_threshold_minutes=60.0,
    )
    assert classify_confidence(signals) is Confidence.HIGH
