"""Tests for ``scripts/verify_proposal_pre_processor.py`` (ALP-319).

The testable logic — the verdict rubric, the fixture builders, and the four
scenario runners — is exercised here without touching the Claude Agent SDK.
All four scenarios are run against in-memory fixture data.

Mirrors :mod:`tests.scripts.test_verify_strategist`.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema

from alphamind.decision.proposal_pre_processor.models import BUNDLE_OUTPUT_SCHEMA
from alphamind.scripts.verify_proposal_pre_processor import (
    Verdict,
    build_fixture_emergency_strategist_output,
    build_fixture_halt_analyst_output,
    build_fixture_halt_strategist_output,
    build_fixture_library_config,
    build_fixture_market_inputs,
    build_fixture_normal_analyst_output,
    build_fixture_normal_strategist_output,
    build_fixture_portfolio_state_snapshot,
    build_fixture_portfolio_state_snapshot_near_limit,
    main,
    run_all_scenarios,
    run_scenario,
)

_INV_ID = "20260504T235223Z-verify-analyst-normal"
_HALT_INV_ID = "20260504T235223Z-verify-analyst-halt"


# ---------------------------------------------------------------------------
# Tracer bullet: normal scenario runs end-to-end and returns PASS
# ---------------------------------------------------------------------------


def test_normal_scenario_pass(tmp_path: Path) -> None:
    """Normal scenario runs, returns PASS, and writes a fixture JSON."""
    analyst_dir = tmp_path / "analyst"
    analyst_dir.mkdir()
    # Copy the real analyst fixture into tmp_path to isolate the test
    real_analyst = (
        Path(__file__).parent.parent / "fixtures" / "decision" / "analyst" / "normal.json"
    )
    (analyst_dir / "normal.json").write_text(real_analyst.read_text(), encoding="utf-8")

    output_dir = tmp_path / "output"
    verdict, errors = run_scenario(
        "normal",
        analyst_fixtures_dir=analyst_dir,
        output_dir=output_dir,
    )

    assert verdict == Verdict.PASS, f"Expected PASS, got FAIL: {errors}"
    assert not errors
    assert (output_dir / "normal.json").exists()


# ---------------------------------------------------------------------------
# Schema validation: produced bundle validates against BUNDLE_OUTPUT_SCHEMA
# ---------------------------------------------------------------------------


def test_normal_bundle_validates_against_schema(tmp_path: Path) -> None:
    """Produced normal bundle JSON validates against BUNDLE_OUTPUT_SCHEMA."""
    analyst_dir = tmp_path / "analyst"
    analyst_dir.mkdir()
    real_analyst = (
        Path(__file__).parent.parent / "fixtures" / "decision" / "analyst" / "normal.json"
    )
    (analyst_dir / "normal.json").write_text(real_analyst.read_text(), encoding="utf-8")

    output_dir = tmp_path / "output"
    run_scenario("normal", analyst_fixtures_dir=analyst_dir, output_dir=output_dir)

    bundle_json = json.loads((output_dir / "normal.json").read_text())
    jsonschema.validate(bundle_json, BUNDLE_OUTPUT_SCHEMA)  # raises if invalid


# ---------------------------------------------------------------------------
# Normal scenario: no breaches
# ---------------------------------------------------------------------------


def test_normal_scenario_no_breaches(tmp_path: Path) -> None:
    """Normal scenario produces a bundle with no breaches."""
    analyst_dir = tmp_path / "analyst"
    analyst_dir.mkdir()
    real_analyst = (
        Path(__file__).parent.parent / "fixtures" / "decision" / "analyst" / "normal.json"
    )
    (analyst_dir / "normal.json").write_text(real_analyst.read_text(), encoding="utf-8")

    from alphamind.decision.proposal_pre_processor.runner import run_proposal_pre_processor
    from alphamind.scripts.verify_proposal_pre_processor import _AS_OF

    analyst_out = build_fixture_normal_analyst_output(fixtures_dir=analyst_dir)
    strategist_out = build_fixture_normal_strategist_output(invocation_id=analyst_out.invocation_id)
    snapshot = build_fixture_portfolio_state_snapshot()
    library_config = build_fixture_library_config()
    market = build_fixture_market_inputs()

    bundle = run_proposal_pre_processor(
        analyst_output=analyst_out,
        strategist_output=strategist_out,
        snapshot=snapshot,
        library_config=library_config,
        market=market,
        snapshot_timestamp=_AS_OF,
        timestamp=_AS_OF,
    )

    assert bundle.aggregate_observations.combined_set_impact.breaches == ()


# ---------------------------------------------------------------------------
# Halt scenario: mode-conditional shape
# ---------------------------------------------------------------------------


def test_halt_scenario_pass(tmp_path: Path) -> None:
    """Halt scenario returns PASS and writes halt.json."""
    analyst_dir = tmp_path / "analyst"
    analyst_dir.mkdir()
    real_halt = Path(__file__).parent.parent / "fixtures" / "decision" / "analyst" / "halt.json"
    (analyst_dir / "halt.json").write_text(real_halt.read_text(), encoding="utf-8")

    output_dir = tmp_path / "output"
    verdict, errors = run_scenario(
        "halt",
        analyst_fixtures_dir=analyst_dir,
        output_dir=output_dir,
    )

    assert verdict == Verdict.PASS, f"Expected PASS, got FAIL: {errors}"
    assert (output_dir / "halt.json").exists()


def test_halt_scenario_mode_shape(tmp_path: Path) -> None:
    """Halt bundle has analyst_section.mode='watchlist', strategist.mode='defensive_posture'."""
    analyst_dir = tmp_path / "analyst"
    analyst_dir.mkdir()
    real_halt = Path(__file__).parent.parent / "fixtures" / "decision" / "analyst" / "halt.json"
    (analyst_dir / "halt.json").write_text(real_halt.read_text(), encoding="utf-8")

    from alphamind.decision.proposal_pre_processor.runner import run_proposal_pre_processor
    from alphamind.scripts.verify_proposal_pre_processor import _AS_OF

    analyst_out = build_fixture_halt_analyst_output(fixtures_dir=analyst_dir)
    strategist_out = build_fixture_halt_strategist_output(invocation_id=analyst_out.invocation_id)
    snapshot = build_fixture_portfolio_state_snapshot()
    library_config = build_fixture_library_config()
    market = build_fixture_market_inputs()

    bundle = run_proposal_pre_processor(
        analyst_output=analyst_out,
        strategist_output=strategist_out,
        snapshot=snapshot,
        library_config=library_config,
        market=market,
        snapshot_timestamp=_AS_OF,
        timestamp=_AS_OF,
    )

    assert bundle.analyst_section.mode == "watchlist"
    assert bundle.strategist_section.mode == "defensive_posture"
    # Halt mode: no recommendations, so analyst_proposal_ids is empty
    assert bundle.aggregate_observations.combined_set_impact.basis.analyst_proposal_ids == ()


# ---------------------------------------------------------------------------
# Emergency scenario: remedy_flag pass-through
# ---------------------------------------------------------------------------


def test_emergency_scenario_pass(tmp_path: Path) -> None:
    """Emergency scenario returns PASS and writes emergency.json."""
    analyst_dir = tmp_path / "analyst"
    analyst_dir.mkdir()
    real_analyst = (
        Path(__file__).parent.parent / "fixtures" / "decision" / "analyst" / "normal.json"
    )
    (analyst_dir / "normal.json").write_text(real_analyst.read_text(), encoding="utf-8")

    output_dir = tmp_path / "output"
    verdict, errors = run_scenario(
        "emergency",
        analyst_fixtures_dir=analyst_dir,
        output_dir=output_dir,
    )

    assert verdict == Verdict.PASS, f"Expected PASS, got FAIL: {errors}"
    assert (output_dir / "emergency.json").exists()


def test_emergency_remedy_flag_passes_through(tmp_path: Path) -> None:
    """At least one position assessment with remedy_flag appears in the bundle."""
    analyst_dir = tmp_path / "analyst"
    analyst_dir.mkdir()
    real_analyst = (
        Path(__file__).parent.parent / "fixtures" / "decision" / "analyst" / "normal.json"
    )
    (analyst_dir / "normal.json").write_text(real_analyst.read_text(), encoding="utf-8")

    from alphamind.decision.proposal_pre_processor.runner import run_proposal_pre_processor
    from alphamind.scripts.verify_proposal_pre_processor import _AS_OF

    analyst_out = build_fixture_normal_analyst_output(fixtures_dir=analyst_dir)
    strategist_out = build_fixture_emergency_strategist_output(
        invocation_id=analyst_out.invocation_id
    )
    snapshot = build_fixture_portfolio_state_snapshot()
    library_config = build_fixture_library_config()
    market = build_fixture_market_inputs()

    bundle = run_proposal_pre_processor(
        analyst_output=analyst_out,
        strategist_output=strategist_out,
        snapshot=snapshot,
        library_config=library_config,
        market=market,
        snapshot_timestamp=_AS_OF,
        timestamp=_AS_OF,
    )

    remedy_flagged = [
        wpa for wpa in bundle.strategist_section.position_assessments if wpa.assessment.remedy_flag
    ]
    assert remedy_flagged, "Expected at least one remedy_flag populated assessment"
    # Verify the flag value is preserved verbatim
    expected_flag = "regime_transition_breach:position_max_size_pct"
    assert remedy_flagged[0].assessment.remedy_flag == expected_flag


# ---------------------------------------------------------------------------
# normal_with_breach scenario
# ---------------------------------------------------------------------------


def test_normal_with_breach_scenario_pass(tmp_path: Path) -> None:
    """normal_with_breach returns PASS and writes the fixture JSON."""
    output_dir = tmp_path / "output"
    verdict, errors = run_scenario(
        "normal_with_breach",
        output_dir=output_dir,
    )

    assert verdict == Verdict.PASS, f"Expected PASS, got FAIL: {errors}"
    assert (output_dir / "normal_with_breach.json").exists()


def test_normal_with_breach_has_net_long_breach(tmp_path: Path) -> None:
    """normal_with_breach bundle has a non-empty breaches list for net_long_exposure."""
    output_dir = tmp_path / "output"
    run_scenario("normal_with_breach", output_dir=output_dir)

    bundle_json = json.loads((output_dir / "normal_with_breach.json").read_text())
    breaches = bundle_json["aggregate_observations"]["combined_set_impact"]["breaches"]
    assert breaches, "Expected at least one breach"

    rules = [b["rule"] for b in breaches]
    net_long_breach = next((b for b in breaches if "net_long" in b["rule"].lower()), None)
    assert net_long_breach is not None, f"Expected net_long breach, got rules: {rules!r}"

    # Each breach must have non-empty contributors
    assert net_long_breach["contributors"], "Breach has no contributors"

    # At least one contributor must be a REC-N with positive contribution
    positive_recs = [
        c
        for c in net_long_breach["contributors"]
        if c["proposal_id"].startswith("REC-") and c["contribution"] > 0
    ]
    assert positive_recs, (
        "No positive-contribution REC-N contributor found in net_long breach; "
        f"contributors: {net_long_breach['contributors']!r}"
    )


# ---------------------------------------------------------------------------
# run_all_scenarios
# ---------------------------------------------------------------------------


def test_run_all_scenarios(tmp_path: Path) -> None:
    """run_all_scenarios produces all four fixture JSONs, all PASS."""
    analyst_dir = tmp_path / "analyst"
    analyst_dir.mkdir()
    for fname in ("normal.json", "halt.json"):
        real = Path(__file__).parent.parent / "fixtures" / "decision" / "analyst" / fname
        (analyst_dir / fname).write_text(real.read_text(), encoding="utf-8")

    output_dir = tmp_path / "output"
    results = run_all_scenarios(analyst_fixtures_dir=analyst_dir, output_dir=output_dir)

    for scenario, (verdict, errors) in results.items():
        assert verdict == Verdict.PASS, f"Scenario {scenario!r} FAIL: {errors}"

    for scenario in ("normal", "halt", "emergency", "normal_with_breach"):
        assert (output_dir / f"{scenario}.json").exists(), f"Missing fixture: {scenario}.json"


# ---------------------------------------------------------------------------
# Missing analyst fixture → clear error + exit 1
# ---------------------------------------------------------------------------


def test_missing_analyst_fixture_returns_fail(tmp_path: Path) -> None:
    """If analyst fixture is missing, run_scenario returns FAIL with clear message."""
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    verdict, errors = run_scenario(
        "normal",
        analyst_fixtures_dir=empty_dir,
        output_dir=tmp_path / "output",
    )

    assert verdict == Verdict.FAIL
    assert errors
    assert any("normal.json" in err or "verify_analyst" in err for err in errors), (
        f"Expected error about missing fixture, got: {errors}"
    )


# ---------------------------------------------------------------------------
# CLI: main() exit codes
# ---------------------------------------------------------------------------


def test_main_all_pass_exits_0(tmp_path: Path) -> None:
    """main() exits 0 when all scenarios pass."""
    analyst_dir = tmp_path / "analyst"
    analyst_dir.mkdir()
    for fname in ("normal.json", "halt.json"):
        real = Path(__file__).parent.parent / "fixtures" / "decision" / "analyst" / fname
        (analyst_dir / fname).write_text(real.read_text(), encoding="utf-8")

    output_dir = tmp_path / "output"
    exit_code = main(
        [
            "--scenario",
            "all",
            "--analyst-fixtures-dir",
            str(analyst_dir),
            "--fixtures-dir",
            str(output_dir),
        ]
    )

    assert exit_code == 0


def test_main_single_scenario_exits_0(tmp_path: Path) -> None:
    """main() with --scenario normal exits 0."""
    analyst_dir = tmp_path / "analyst"
    analyst_dir.mkdir()
    real = Path(__file__).parent.parent / "fixtures" / "decision" / "analyst" / "normal.json"
    (analyst_dir / "normal.json").write_text(real.read_text(), encoding="utf-8")

    output_dir = tmp_path / "output"
    exit_code = main(
        [
            "--scenario",
            "normal",
            "--analyst-fixtures-dir",
            str(analyst_dir),
            "--fixtures-dir",
            str(output_dir),
        ]
    )

    assert exit_code == 0


def test_main_missing_fixture_exits_1(tmp_path: Path) -> None:
    """main() exits 1 when analyst fixture is missing."""
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    exit_code = main(
        [
            "--scenario",
            "normal",
            "--analyst-fixtures-dir",
            str(empty_dir),
            "--fixtures-dir",
            str(tmp_path / "output"),
        ]
    )

    assert exit_code == 1


# ---------------------------------------------------------------------------
# No SDK import guard
# ---------------------------------------------------------------------------


def test_no_sdk_import() -> None:
    """verify_proposal_pre_processor does not import claude_agent_sdk."""
    import alphamind.scripts.verify_proposal_pre_processor as mod

    source = Path(mod.__file__).read_text(encoding="utf-8")
    assert "claude_agent_sdk" not in source, (
        "verify_proposal_pre_processor.py must not import claude_agent_sdk"
    )


# ---------------------------------------------------------------------------
# Fixture-builder sanity checks
# ---------------------------------------------------------------------------


def test_library_config_has_net_long_limit() -> None:
    """Library config has net_long_pct limit of 60.0."""
    config = build_fixture_library_config()
    assert config.effective_limits["net_long_pct"] == 60.0


def test_near_limit_snapshot_net_long() -> None:
    """Near-limit snapshot has net_long_pct=49.5."""
    snapshot = build_fixture_portfolio_state_snapshot_near_limit()
    assert snapshot.net_long_pct == 49.5


def test_normal_strategist_output_all_hold() -> None:
    """Normal strategist output has all position assessments as 'hold'."""
    out = build_fixture_normal_strategist_output(invocation_id="test-inv")
    for pa in out.position_assessments:
        assert pa.recommended_action == "hold"


def test_emergency_strategist_output_has_remedy_flag() -> None:
    """Emergency strategist output has at least one position with remedy_flag."""
    out = build_fixture_emergency_strategist_output(invocation_id="test-inv")
    remedy_flagged = [pa for pa in out.position_assessments if pa.remedy_flag]
    assert remedy_flagged


def test_halt_strategist_output_defensive_posture() -> None:
    """Halt strategist output has mode=defensive_posture."""
    out = build_fixture_halt_strategist_output(invocation_id="test-inv")
    assert out.mode == "defensive_posture"
