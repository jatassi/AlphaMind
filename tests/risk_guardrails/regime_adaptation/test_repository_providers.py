"""Tests for ``alphamind.risk_guardrails.regime_adaptation.repository_providers``.

The two closure-providers (active + prior) historically lived in
``scheduler/orchestrator.py`` as ``_make_repository_providers``. ALP-472
lifted them to the regime-adaptation feature package they belong to (both
return an :class:`ActiveRiskParameterSet`, the prior provider uses
:func:`load_prior_active_risk_parameters`).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_active_provider_returns_input_set() -> None:
    """The zero-arg active provider returns its input parameter set."""
    from alphamind.config.models.regimes import Regime
    from alphamind.risk_guardrails.regime_adaptation import (
        build_active_risk_parameters,
    )
    from alphamind.risk_guardrails.regime_adaptation.repository_providers import (
        make_repository_providers,
    )

    current = build_active_risk_parameters(
        rule_values={"daily_drawdown_pct": 0.05},
        regime=Regime.normal,
    )

    active, _prior = make_repository_providers(current)

    assert active() is current


def test_prior_provider_reads_snapshot(tmp_path: Path) -> None:
    """The prior provider rehydrates the snapshot-derived set, not the current one."""
    from alphamind._kernel.regime import RegimeLabel
    from alphamind.config.models.regimes import Regime
    from alphamind.risk_guardrails.regime_adaptation import (
        build_active_risk_parameters,
    )
    from alphamind.risk_guardrails.regime_adaptation.repository_providers import (
        make_repository_providers,
    )

    prior_path = tmp_path / "prior_resolved.json"
    prior_path.write_text(
        json.dumps(
            {
                "regime_label": Regime.crisis.value,
                "rule_values": {"daily_drawdown_pct": 0.005},
            }
        )
    )

    current = build_active_risk_parameters(
        rule_values={"daily_drawdown_pct": 0.05},
        regime=Regime.normal,
    )

    _active, prior = make_repository_providers(current)
    prior_set = prior(str(prior_path))

    assert prior_set.regime_label is RegimeLabel.CRISIS
    rule_map = {entry.rule_id: entry.value for entry in prior_set.entries}
    assert rule_map == {"daily_drawdown_pct": 0.005}


def test_prior_provider_missing_snapshot_falls_back_to_current(tmp_path: Path) -> None:
    """A missing snapshot path falls back to the current parameter set.

    First-ever invocation has no prior; the FK-resolution layer may still
    hand the provider a path that doesn't exist (e.g. archive relocation).
    Falling back to the current set keeps the snapshot assembler operational
    instead of raising mid-invocation.
    """
    from alphamind.config.models.regimes import Regime
    from alphamind.risk_guardrails.regime_adaptation import (
        build_active_risk_parameters,
    )
    from alphamind.risk_guardrails.regime_adaptation.repository_providers import (
        make_repository_providers,
    )

    current = build_active_risk_parameters(
        rule_values={"daily_drawdown_pct": 0.05},
        regime=Regime.normal,
    )

    _active, prior = make_repository_providers(current)
    result = prior(str(tmp_path / "definitely_not_on_disk.json"))

    assert result is current


def test_prior_provider_corrupt_snapshot_propagates(tmp_path: Path) -> None:
    """Corrupt prior snapshot propagates JSON errors.

    Defensive fallback would mask a contract violation by substituting an
    unrelated set; propagation aborts the invocation cleanly so the operator
    sees the cause. The ``FileNotFoundError`` fallback exists only for the
    legitimate "no prior" case.
    """
    from alphamind.config.models.regimes import Regime
    from alphamind.risk_guardrails.regime_adaptation import (
        build_active_risk_parameters,
    )
    from alphamind.risk_guardrails.regime_adaptation.repository_providers import (
        make_repository_providers,
    )

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not valid json")

    current = build_active_risk_parameters(
        rule_values={"daily_drawdown_pct": 0.05},
        regime=Regime.normal,
    )

    _active, prior = make_repository_providers(current)
    with pytest.raises(json.JSONDecodeError):
        prior(str(corrupt))


def test_prior_provider_empty_path_falls_back_to_current(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An empty snapshot path falls back to the current parameter set silently.

    A ``borrow_accrual`` maintenance tick (ALP-754) writes an ``invocations``
    row with an empty ``resolved_config_snapshot_path``. ``Path('').read_text()``
    raises ``FileNotFoundError`` on POSIX but ``PermissionError`` on Windows
    (``Path('')`` resolves to ``'.'``), which previously propagated past the
    ``FileNotFoundError``-only catch and aborted Phase 1. A blank path means
    "no prior snapshot" and must degrade to the current set — and, being the
    routine post-tick case, must NOT log a warning (that would be per-run noise).
    """
    import logging

    from alphamind.config.models.regimes import Regime
    from alphamind.risk_guardrails.regime_adaptation import (
        build_active_risk_parameters,
    )
    from alphamind.risk_guardrails.regime_adaptation.repository_providers import (
        make_repository_providers,
    )

    current = build_active_risk_parameters(
        rule_values={"daily_drawdown_pct": 0.05},
        regime=Regime.normal,
    )

    _active, prior = make_repository_providers(current)

    with caplog.at_level(logging.WARNING):
        assert prior("") is current
        assert prior("   ") is current

    assert caplog.records == []


def test_prior_provider_directory_path_falls_back_and_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A path that resolves to a directory falls back to the current set + warns.

    Reading a directory raises ``IsADirectoryError`` on POSIX and
    ``PermissionError`` on Windows — both ``OSError`` subclasses but neither a
    ``FileNotFoundError``. This is the cross-platform analogue of the empty-path
    crash and exercises the broadened path-resolution fallback (ALP-754). Unlike
    a blank path, an unreadable *present* path is unexpected, so the fallback
    must leave a warning trail rather than substituting the current set silently.
    """
    import logging

    from alphamind.config.models.regimes import Regime
    from alphamind.risk_guardrails.regime_adaptation import (
        build_active_risk_parameters,
    )
    from alphamind.risk_guardrails.regime_adaptation.repository_providers import (
        make_repository_providers,
    )

    current = build_active_risk_parameters(
        rule_values={"daily_drawdown_pct": 0.05},
        regime=Regime.normal,
    )

    _active, prior = make_repository_providers(current)

    with caplog.at_level(logging.WARNING):
        assert prior(str(tmp_path)) is current

    assert any("prior-snapshot rehydrate failed" in record.message for record in caplog.records)


def test_prior_provider_missing_file_falls_back_silently(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A non-blank path to a missing file falls back without warning.

    ``FileNotFoundError`` on a non-blank path is the routine "no prior on disk"
    case (first-ever invocation, archive relocation) and must stay quiet — only
    non-``FileNotFoundError`` ``OSError``s warrant a warning (ALP-754).
    """
    import logging

    from alphamind.config.models.regimes import Regime
    from alphamind.risk_guardrails.regime_adaptation import (
        build_active_risk_parameters,
    )
    from alphamind.risk_guardrails.regime_adaptation.repository_providers import (
        make_repository_providers,
    )

    current = build_active_risk_parameters(
        rule_values={"daily_drawdown_pct": 0.05},
        regime=Regime.normal,
    )

    _active, prior = make_repository_providers(current)

    with caplog.at_level(logging.WARNING):
        assert prior(str(tmp_path / "definitely_not_on_disk.json")) is current

    assert caplog.records == []
