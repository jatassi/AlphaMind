"""Tests for ``alphamind.risk_guardrails.regime_adaptation.active_parameters``.

These three helpers historically lived inline in ``scheduler/orchestrator.py``
as the pre-review-triage shims that wrapped the resolver-folded
``rule_values`` map in an :class:`ActiveRiskParameterSet` and an
:class:`RegimeAdaptationOutput`. ALP-472 lifted them to the
regime-adaptation feature package they belong to.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_build_active_risk_parameters_returns_set_with_entries() -> None:
    """``build_active_risk_parameters`` wraps a flat rule_values map."""
    from alphamind._kernel.regime import RegimeLabel, RegimeTransitionState
    from alphamind.config.models.regimes import Regime
    from alphamind.risk_guardrails.regime_adaptation.active_parameters import (
        build_active_risk_parameters,
    )

    params = build_active_risk_parameters(
        rule_values={"daily_drawdown_pct": 0.025, "position_max_size_pct": 0.05},
        regime=Regime.normal,
    )

    assert params.regime_label is RegimeLabel.NORMAL
    assert params.transition_state is RegimeTransitionState.STABLE
    assert params.transition_invocations_remaining == 0
    assert params.parameter_change_flag is False
    rule_map = {entry.rule_id: entry.value for entry in params.entries}
    assert rule_map == {"daily_drawdown_pct": 0.025, "position_max_size_pct": 0.05}
    assert params.active_overlays == ()


def test_build_active_risk_parameters_sorts_entries_by_rule_id() -> None:
    """Entries are sorted by ``rule_id`` for deterministic order."""
    from alphamind.config.models.regimes import Regime
    from alphamind.risk_guardrails.regime_adaptation.active_parameters import (
        build_active_risk_parameters,
    )

    params = build_active_risk_parameters(
        rule_values={"zzz": 1.0, "aaa": 2.0, "mmm": 3.0},
        regime=Regime.normal,
    )

    assert [e.rule_id for e in params.entries] == ["aaa", "mmm", "zzz"]


def test_build_active_risk_parameters_regime_mapping() -> None:
    """Each ``Regime`` member maps to its corresponding ``RegimeLabel``."""
    from alphamind._kernel.regime import RegimeLabel
    from alphamind.config.models.regimes import Regime
    from alphamind.risk_guardrails.regime_adaptation.active_parameters import (
        build_active_risk_parameters,
    )

    mapping = {
        Regime.low_vol: RegimeLabel.LOW_VOL,
        Regime.normal: RegimeLabel.NORMAL,
        Regime.elevated: RegimeLabel.ELEVATED,
        Regime.crisis: RegimeLabel.CRISIS,
    }
    for regime, expected_label in mapping.items():
        params = build_active_risk_parameters(rule_values={}, regime=regime)
        assert params.regime_label is expected_label


def test_build_synthetic_regime_output_wraps_parameters() -> None:
    """``build_synthetic_regime_output`` wraps a parameter set in a RegimeAdaptationOutput."""
    from datetime import UTC, datetime

    from alphamind._kernel.regime import RegimeTransitionState
    from alphamind.config.models.regimes import Regime
    from alphamind.risk_guardrails.regime_adaptation.active_parameters import (
        build_active_risk_parameters,
        build_synthetic_regime_output,
    )

    now = datetime(2026, 5, 12, 14, 30, tzinfo=UTC)
    params = build_active_risk_parameters(
        rule_values={"daily_drawdown_pct": 0.025},
        regime=Regime.normal,
    )

    output = build_synthetic_regime_output(
        active_risk_parameters=params,
        runtime_active_regime=Regime.normal,
        invocation_id="inv-test-1",
        now=now,
    )

    assert output.runtime_dimensions_active_regime is Regime.normal
    assert output.runtime_dimensions_active_overlays == ()
    assert output.overlay_activation_decisions == ()
    assert output.effective_limits == {}
    assert output.active_risk_parameter_set is params
    assert output.regime_transition_breaches == ()
    assert output.regime_skip_emergency is False
    assert output.new_persisted_state.invocation_id == "inv-test-1"
    assert output.new_persisted_state.active_regime is Regime.normal
    assert output.new_persisted_state.transition_state is RegimeTransitionState.STABLE
    assert output.audit_log_entries == ()


def test_load_prior_active_risk_parameters_reads_snapshot(tmp_path: Path) -> None:
    """``load_prior_active_risk_parameters`` rehydrates from a resolved-config snapshot."""
    from alphamind._kernel.regime import RegimeLabel
    from alphamind.config.models.regimes import Regime
    from alphamind.risk_guardrails.regime_adaptation.active_parameters import (
        load_prior_active_risk_parameters,
    )

    snapshot_path = tmp_path / "prior_resolved.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "regime_label": Regime.elevated.value,
                "rule_values": {
                    "daily_drawdown_pct": 0.025,
                    "position_max_loss_pct": 0.01,
                },
            }
        )
    )

    result = load_prior_active_risk_parameters(str(snapshot_path))

    assert result.regime_label is RegimeLabel.ELEVATED
    rule_map = {entry.rule_id: entry.value for entry in result.entries}
    assert rule_map == {
        "daily_drawdown_pct": 0.025,
        "position_max_loss_pct": 0.01,
    }


def test_load_prior_active_risk_parameters_corrupt_json_raises(tmp_path: Path) -> None:
    """Corrupt JSON propagates a :class:`json.JSONDecodeError`."""
    from alphamind.risk_guardrails.regime_adaptation.active_parameters import (
        load_prior_active_risk_parameters,
    )

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not valid json")
    with pytest.raises(json.JSONDecodeError):
        load_prior_active_risk_parameters(str(corrupt))


def test_load_prior_active_risk_parameters_missing_file_raises(tmp_path: Path) -> None:
    """A missing snapshot file propagates :class:`FileNotFoundError`.

    The fallback to the current parameter set is the repository-provider's
    responsibility, not the loader's.
    """
    from alphamind.risk_guardrails.regime_adaptation.active_parameters import (
        load_prior_active_risk_parameters,
    )

    missing = tmp_path / "definitely_not_on_disk.json"
    with pytest.raises(FileNotFoundError):
        load_prior_active_risk_parameters(str(missing))
