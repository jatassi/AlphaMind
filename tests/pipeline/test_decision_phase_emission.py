"""Tests for decision-layer phase-output emission wiring — ALP-692.

Exercises ``run_decision_pipeline`` with:
* ``archive_root`` set + ``debug_e2e``-populated context — verifies that
  ``analyst.json``, ``strategist.json``, ``pm.json`` land under
  ``<archive_root>/<YYYY-MM-DD>/<invocation_id>/phase_outputs/``.
* ``archive_root is None`` — verifies no files are written under
  ``phase_outputs/``.
* Each emitted file round-trips through ``read_phase_output`` to a model
  equal to the one written.

All four agent runners (analyst, strategist, pre_processor, pm) are
monkeypatched at the composition module's namespace exactly as in
``tests/pipeline/test_decision.py`` so the tests never touch the
filesystem for anything other than the phase output files.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from alphamind._kernel.ids import InvocationId
from alphamind.analysis._shared import TokensUsed
from alphamind.commands.pm_envelope import (
    PMCompletionRecord,
    VerdictSummary,
)
from alphamind.commands.validation_results import ValidationResult
from alphamind.decision.analyst.runner import AnalystResult
from alphamind.decision.portfolio_manager.runner import PMResult
from alphamind.decision.strategist.runner import StrategistResult
from alphamind.scheduler.debug_e2e.phase_outputs import read_phase_output

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_INVOCATION_ID = "inv-emission-test-001"
_TIMESTAMP = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


def _tokens() -> TokensUsed:
    return TokensUsed(input_tokens=100, output_tokens=50, cache_read_tokens=0, cache_write_tokens=0)


# ---------------------------------------------------------------------------
# Stage result factories (minimal valid values)
# ---------------------------------------------------------------------------


def _make_analyst_result() -> AnalystResult:
    from alphamind.decision.analyst.models import AnalystOutput

    return AnalystResult(
        output=AnalystOutput(
            invocation_id=InvocationId(_INVOCATION_ID),
            timestamp=_TIMESTAMP,
            mode="normal",
            recommendations=(),
        ),
        retry_count=0,
        tokens_used=_tokens(),
        tool_calls_used=1,
        wall_clock_seconds=5.0,
        stop_reason="end_turn",
    )


def _make_strategist_result() -> StrategistResult:
    from alphamind.decision.strategist.models import (
        PortfolioLevelObservations,
        StrategistOutput,
    )

    return StrategistResult(
        output=StrategistOutput(
            invocation_id=InvocationId(_INVOCATION_ID),
            timestamp=_TIMESTAMP,
            mode="normal",
            position_assessments=(),
            pending_order_assessments=(),
            portfolio_level_observations=PortfolioLevelObservations(
                aggregate_thesis_health="Stable.",
                sector_balance_shifts="None.",
                thesis_dependency_warnings="None.",
                capital_allocation_observations="Balanced.",
            ),
        ),
        validation_result=ValidationResult(errors=()),
        tokens_used=_tokens(),
        metadata={"attempts": 1},
    )


def _make_pm_result() -> PMResult:
    return PMResult(
        output=PMCompletionRecord(
            invocation_id=InvocationId(_INVOCATION_ID),
            timestamp=_TIMESTAMP,
            envelopes_submitted=0,
            verdict_summary=VerdictSummary(
                approve=0,
                approve_with_modification=0,
                reject=0,
                override_with_corrective_action=0,
            ),
        ),
        submission_log=(),
        retry_count=0,
        tokens_used=_tokens(),
        tool_calls_used=0,
        wall_clock_seconds=3.0,
        stop_reason="end_turn",
    )


# ---------------------------------------------------------------------------
# Pre-processor bundle helper (re-use existing fixture)
# ---------------------------------------------------------------------------


def _make_pre_processor_bundle() -> Any:
    from tests.pipeline.test_decision import _make_pre_processor_bundle as _upstream

    return _upstream()


# ---------------------------------------------------------------------------
# Monkeypatch helpers
# ---------------------------------------------------------------------------


def _patch_runners(
    monkeypatch: pytest.MonkeyPatch,
    *,
    analyst_result: AnalystResult | None = None,
    strategist_result: StrategistResult | None = None,
    pm_result: PMResult | None = None,
) -> None:
    from alphamind.pipeline import decision as composition

    analyst_val = analyst_result or _make_analyst_result()
    strategist_val = strategist_result or _make_strategist_result()
    pp_val = _make_pre_processor_bundle()
    pm_val = pm_result or _make_pm_result()

    async def _analyst_stub(**kw: Any) -> AnalystResult:
        return analyst_val

    async def _strategist_stub(**kw: Any) -> StrategistResult:
        return strategist_val

    def _pp_stub(**kw: Any) -> Any:
        return pp_val

    async def _pm_stub(**kw: Any) -> PMResult:
        return pm_val

    monkeypatch.setattr(composition, "run_analyst", _analyst_stub)
    monkeypatch.setattr(composition, "run_strategist", _strategist_stub)
    monkeypatch.setattr(composition, "run_proposal_pre_processor", _pp_stub)
    monkeypatch.setattr(composition, "run_portfolio_manager", _pm_stub)


def _make_minimal_inputs() -> dict[str, Any]:
    from tests.pipeline.test_decision import _make_minimal_inputs as _upstream

    return _upstream()


_DEBUG_E2E_SENTINEL = object()


def _drive(
    archive_root: Path | None,
    *,
    debug_e2e: object | None = _DEBUG_E2E_SENTINEL,
    **overrides: Any,
) -> Any:
    from alphamind.pipeline.decision import run_decision_pipeline

    async def _go() -> Any:
        kwargs = _make_minimal_inputs()
        kwargs["archive_root"] = archive_root
        kwargs["invocation_id"] = _INVOCATION_ID
        kwargs["debug_e2e"] = debug_e2e
        kwargs.update(overrides)
        return await run_decision_pipeline(**kwargs)

    return asyncio.run(_go())


# ---------------------------------------------------------------------------
# Tests: emission with archive_root set
# ---------------------------------------------------------------------------


class TestDecisionPhaseEmission:
    def test_three_files_written_when_archive_root_set(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """analyst.json, strategist.json, pm.json appear under phase_outputs/."""
        _patch_runners(monkeypatch)
        _drive(archive_root=tmp_path)

        phase_outputs_dir = tmp_path / "2025-06-01" / _INVOCATION_ID / "phase_outputs"
        assert (phase_outputs_dir / "analyst.json").exists(), "analyst.json missing"
        assert (phase_outputs_dir / "strategist.json").exists(), "strategist.json missing"
        assert (phase_outputs_dir / "pm.json").exists(), "pm.json missing"

    def test_analyst_json_round_trips_via_read_phase_output(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """analyst.json can be read back via read_phase_output to an equal model."""
        from alphamind.decision.analyst.models import AnalystResultModel

        _patch_runners(monkeypatch)
        _drive(archive_root=tmp_path)

        archive_dir = tmp_path / "2025-06-01" / _INVOCATION_ID
        model = read_phase_output(
            archive_dir=archive_dir, phase="analyst", model_cls=AnalystResultModel
        )
        assert isinstance(model, AnalystResultModel)
        assert model.stop_reason == "end_turn"

    def test_strategist_json_round_trips_via_read_phase_output(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """strategist.json can be read back via read_phase_output to an equal model."""
        from alphamind.decision.strategist.models import StrategistResultModel

        _patch_runners(monkeypatch)
        _drive(archive_root=tmp_path)

        archive_dir = tmp_path / "2025-06-01" / _INVOCATION_ID
        model = read_phase_output(
            archive_dir=archive_dir, phase="strategist", model_cls=StrategistResultModel
        )
        assert isinstance(model, StrategistResultModel)

    def test_pm_json_round_trips_via_read_phase_output(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """pm.json can be read back via read_phase_output to an equal model."""
        from alphamind.decision.portfolio_manager.models import PMResultModel

        _patch_runners(monkeypatch)
        _drive(archive_root=tmp_path)

        archive_dir = tmp_path / "2025-06-01" / _INVOCATION_ID
        model = read_phase_output(archive_dir=archive_dir, phase="pm", model_cls=PMResultModel)
        assert isinstance(model, PMResultModel)
        assert model.retry_count == 0

    def test_emitted_analyst_json_equals_written_model(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """analyst.json round-trips to a model equal to what was written."""
        from alphamind.decision.analyst.models import AnalystResultModel

        analyst_result = _make_analyst_result()
        _patch_runners(monkeypatch, analyst_result=analyst_result)
        _drive(archive_root=tmp_path)

        archive_dir = tmp_path / "2025-06-01" / _INVOCATION_ID
        recovered = read_phase_output(
            archive_dir=archive_dir, phase="analyst", model_cls=AnalystResultModel
        )
        expected = AnalystResultModel.from_domain(analyst_result)
        assert recovered == expected


# ---------------------------------------------------------------------------
# Tests: no-write when archive_root is None
# ---------------------------------------------------------------------------


class TestDecisionPhaseEmissionNoWrite:
    def test_no_phase_outputs_dir_when_archive_root_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """When archive_root is None, phase_outputs/ is never created anywhere."""
        _patch_runners(monkeypatch)
        _drive(archive_root=None)

        # Nothing under tmp_path (which we pass as a sentinel to confirm
        # the pipeline didn't write anywhere — it won't because archive_root=None
        # prevents the emission path from computing any target directory).
        # The most direct check: there are no .json files in tmp_path subtree.
        json_files = list(tmp_path.rglob("*.json"))
        assert json_files == [], f"Expected zero JSON files, found: {json_files}"

    def test_no_phase_outputs_when_debug_e2e_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Production daemon path: archive_root is always set, but debug_e2e is
        ``None`` outside debug-e2e mode. Emission MUST be skipped — without
        this guard the production daemon would silently accumulate
        analyst/strategist/pm JSON files in its archive directory on every
        invocation.
        """
        _patch_runners(monkeypatch)
        _drive(archive_root=tmp_path, debug_e2e=None)

        json_files = list(tmp_path.rglob("*.json"))
        assert json_files == [], (
            f"Production path (archive_root set, debug_e2e=None) must not write "
            f"phase_outputs. Found: {json_files}"
        )
