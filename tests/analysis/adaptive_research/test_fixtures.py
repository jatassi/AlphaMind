"""Drift canaries for the adaptive-researcher test fixtures.

The shipped JSON fixtures under ``tests/analysis/adaptive_research/fixtures/``
encode the typed five-tuple the runner consumes:
``(tuple[SectorBrief, ...], QualitativeBrief, CorrelationRegimeBrief,
DistillationOutputs, universal_regime_label)``. These tests assert the
fixtures still load, still cover every Layer-3 prefix family the validator
resolves, still drive the renderer out of its empty-state branches, and
still mirror the canonical regime-payload keys
:func:`alphamind.distillation.regime.assemble_regime_block` emits in
production.

Originally extracted from a live-SDK e2e test (ALP-265); the live path is
covered by ``scripts/verify_debug_e2e.py`` end-to-end, but the fixture
invariants are worth keeping as a fast, no-SDK canary.
"""

from __future__ import annotations

from alphamind.analysis.adaptive_research.validation import _build_reference_universe
from alphamind.analysis.domain_researchers.models import SectorBrief
from alphamind.analysis.qualitative_research.models import QualitativeBrief
from alphamind.distillation.correlation_brief import CorrelationRegimeBrief
from alphamind.distillation.orchestrator import DistillationOutputs
from tests.analysis.adaptive_research.fixtures import load_e2e_fixtures


def test_load_e2e_fixtures_returns_typed_tuple() -> None:
    """``load_e2e_fixtures()`` returns the typed five-tuple the runner consumes."""
    fixtures = load_e2e_fixtures()
    sector_briefs, qualitative_brief, correlation_regime_brief, distillation_outputs, regime = (
        fixtures
    )

    assert isinstance(sector_briefs, tuple)
    assert len(sector_briefs) == 3
    for sb in sector_briefs:
        assert isinstance(sb, SectorBrief)
    assert isinstance(qualitative_brief, QualitativeBrief)
    assert isinstance(correlation_regime_brief, CorrelationRegimeBrief)
    assert isinstance(distillation_outputs, DistillationOutputs)
    assert isinstance(regime, dict)


def test_fixtures_exercise_validator_layer3_universe() -> None:
    """Fixtures populate every Layer-3 prefix family the validator resolves.

    The reference universe must include at least one ID for each prefix
    family:

    * ``SA-{SECTOR}-N`` from sector findings
    * ``SA-{SECTOR}-ANOM-N`` from sector anomalies
    * ``SA-{SECTOR}-TC-N`` from sector thesis candidates
    * ``QR-N`` from qualitative threads
    * ``QR-CW-N`` from qualitative catalyst watches
    * ``CR-N`` from correlation/regime reference index
    """
    sector_briefs, qb, crb, _do, _regime = load_e2e_fixtures()
    valid_ids = _build_reference_universe(sector_briefs, qb, crb)
    assert any(
        rid.startswith("SA-") and "-ANOM-" not in rid and "-TC-" not in rid for rid in valid_ids
    )
    assert any("-ANOM-" in rid for rid in valid_ids)
    assert any("-TC-" in rid for rid in valid_ids)
    assert any(rid.startswith("QR-") and not rid.startswith("QR-CW-") for rid in valid_ids)
    assert any(rid.startswith("QR-CW-") for rid in valid_ids)
    assert any(rid.startswith("CR-") for rid in valid_ids)


def test_distillation_outputs_carries_mixed_anomaly_blocks() -> None:
    """Distillation fixture is multi-block with at least one anomaly flag.

    Drives the renderer's ``DISTILLATION ANOMALY FLAGS`` section out of
    the ``(none)`` branch so a downstream e2e bundle would exercise real
    flag rendering.
    """
    _sb, _qb, _crb, distillation_outputs, _regime = load_e2e_fixtures()
    assert len(distillation_outputs.all_blocks) >= 2
    flag_total = sum(len(block.anomaly_flags) for block in distillation_outputs.all_blocks)
    assert flag_total >= 1


def test_universal_regime_label_carries_canonical_keys() -> None:
    """Regime fixture mirrors the production payload from ``assemble_regime_block``.

    The adaptive bundle renders every key in the regime payload (matching
    the qualitative bundle); the fixture must therefore carry the same
    keys :func:`alphamind.distillation.regime.assemble_regime_block`
    emits in production.
    """
    _sb, _qb, _crb, _do, regime = load_e2e_fixtures()
    expected = {
        "regime_label",
        "transition_state",
        "prior_label",
        "invocations_held",
        "indicator_agreement_count",
        "regime_skip_emergency",
        "vix_level",
        "term_structure_basis",
        "vvix_percentile",
        "realized_vol_5d",
        "realized_vol_20d",
    }
    assert expected.issubset(regime.keys())
