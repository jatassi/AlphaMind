"""Per-detector boundary tests for the seven notable-shift detectors (ALP-888).

Each detector fires / does-not-fire at its ``config/digest.yaml``
``baseline_window_weeks`` / threshold boundary. The five metric-driven detectors plant
a temporary registered metric (so the gated id resolves); the two validation detectors
read the dataset's validation bundles directly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from alphamind.config.models.digest import (
    AntiPatternSpike,
    CitationChainShift,
    RegimeChange,
    SectorUnderperform,
    SourceSignalSurvivalDrop,
    ValidationSuperseded,
    ValidationWindowEnd,
)
from alphamind.feedback_loop.citation.parser import CitationSource
from alphamind.feedback_loop.digest.shifts import (
    detect_anti_pattern_spike,
    detect_citation_chain_shift,
    detect_regime_change,
    detect_sector_underperform,
    detect_source_signal_survival_drop,
    detect_validation_superseded,
    detect_validation_window_end,
)
from alphamind.feedback_loop.validation.records import SupersededReason
from tests.feedback_loop.digest import _fixtures as fx

_MONDAY = datetime(2026, 3, 30, tzinfo=UTC)  # the base current-week Monday


def _weeks(count: int) -> list[tuple[str, object]]:
    return fx.week_sequence(count, base=_MONDAY)


def _label_at(offset_from_current: int) -> str:
    """The ISO label of the week ``offset_from_current`` weeks before current."""
    return (_MONDAY - timedelta(weeks=offset_from_current)).date().isoformat()


# ---------------------------------------------------------------------------
# Detector 1 — anti-pattern spike
# ---------------------------------------------------------------------------


class TestAntiPatternSpike:
    _CONFIG = AntiPatternSpike(
        baseline_window_weeks=4, multiplier_vs_baseline=2.0, min_occurrences_this_week=5
    )
    _METRIC = "anti_pattern_frequency__sunk_cost_persistence"

    def test_fires_above_multiplier_and_floor(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        weeks = _weeks(5)  # 1 current + 4 baseline
        values = {_label_at(i): 1.0 for i in range(1, 5)}  # baseline mean = 1.0
        values[_label_at(0)] = 6.0  # > 2.0*1.0 and >= floor 5
        fx.install_metrics(monkeypatch, (fx.per_week_metric(self._METRIC, values),))

        findings = detect_anti_pattern_spike(weeks, self._CONFIG)
        assert len(findings) == 1
        assert findings[0].subject == "sunk_cost_persistence"

    def test_does_not_fire_below_floor(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        weeks = _weeks(5)
        values = {_label_at(i): 0.5 for i in range(1, 5)}  # mean 0.5
        values[_label_at(0)] = 4.0  # > 2.0*0.5 but < floor 5
        fx.install_metrics(monkeypatch, (fx.per_week_metric(self._METRIC, values),))

        assert detect_anti_pattern_spike(weeks, self._CONFIG) == ()

    def test_does_not_fire_at_multiplier_boundary(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        weeks = _weeks(5)
        values = {_label_at(i): 3.0 for i in range(1, 5)}  # mean 3.0
        values[_label_at(0)] = 6.0  # == 2.0*3.0 exactly → not strictly greater
        fx.install_metrics(monkeypatch, (fx.per_week_metric(self._METRIC, values),))

        assert detect_anti_pattern_spike(weeks, self._CONFIG) == ()

    def test_unregistered_metric_degrades_to_no_finding(self) -> None:
        # No planted metric → gated id resolves to None → no finding, no error.
        assert detect_anti_pattern_spike(_weeks(5), self._CONFIG) == ()


# ---------------------------------------------------------------------------
# Detector 2 — regime change
# ---------------------------------------------------------------------------


class TestRegimeChange:
    _METRIC = "regime_classification"

    def test_fires_on_label_change(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        weeks = _weeks(2)
        values = {_label_at(1): 1.0, _label_at(0): 2.0}
        fx.install_metrics(monkeypatch, (fx.per_week_metric(self._METRIC, values),))

        findings = detect_regime_change(weeks, enabled=True)
        assert len(findings) == 1
        assert findings[0].kind == "regime_change"

    def test_does_not_fire_on_unchanged_label(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        weeks = _weeks(2)
        values = {_label_at(1): 1.0, _label_at(0): 1.0}
        fx.install_metrics(monkeypatch, (fx.per_week_metric(self._METRIC, values),))

        assert detect_regime_change(weeks, enabled=True) == ()

    def test_disabled_never_fires(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        weeks = _weeks(2)
        values = {_label_at(1): 1.0, _label_at(0): 2.0}
        fx.install_metrics(monkeypatch, (fx.per_week_metric(self._METRIC, values),))

        assert detect_regime_change(weeks, enabled=False) == ()


# ---------------------------------------------------------------------------
# Detector 3 — sector underperforming
# ---------------------------------------------------------------------------


class TestSectorUnderperform:
    _CONFIG = SectorUnderperform(baseline_window_weeks=4, median_offset_sigma=1.5)

    def _plant_sectors(self, monkeypatch, sector_values: dict[str, float]) -> None:  # type: ignore[no-untyped-def]
        metrics = tuple(
            fx.constant_metric(f"sector_rolling_pl__{sector}", value)
            for sector, value in sector_values.items()
        )
        fx.install_metrics(monkeypatch, metrics)

    def test_fires_for_sector_far_below_peers(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        # Peers cluster around 100 with tiny spread; technology is far below.
        self._plant_sectors(
            monkeypatch,
            {
                "technology": -100.0,
                "financials": 100.0,
                "energy": 101.0,
                "healthcare": 99.0,
                "consumer": 100.0,
            },
        )
        findings = detect_sector_underperform(_weeks(5), self._CONFIG)
        subjects = {f.subject for f in findings}
        assert "technology" in subjects

    def test_does_not_fire_when_all_sectors_equal(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        # Equal P/L across sectors → zero peer spread → threshold == peer median for
        # every sector, and no value is strictly below it, so nothing fires.
        self._plant_sectors(
            monkeypatch,
            {
                "technology": 100.0,
                "financials": 100.0,
                "energy": 100.0,
                "healthcare": 100.0,
                "consumer": 100.0,
            },
        )
        assert detect_sector_underperform(_weeks(5), self._CONFIG) == ()

    def test_degrades_when_fewer_than_two_sectors_present(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        self._plant_sectors(monkeypatch, {"technology": -100.0})
        assert detect_sector_underperform(_weeks(5), self._CONFIG) == ()


# ---------------------------------------------------------------------------
# Detector 4 — citation chain shift
# ---------------------------------------------------------------------------


class TestCitationChainShift:
    _CONFIG = CitationChainShift(baseline_window_weeks=4, delta_pp_threshold=20.0)

    def _metric_id(self) -> str:
        from alphamind.feedback_loop.citation.chain import metric_id_for

        return str(metric_id_for("synthesizer_citation_rate", CitationSource.SA_TECH))

    def test_fires_above_pp_threshold(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        weeks = _weeks(5)
        metric_id = self._metric_id()
        values = {_label_at(i): 0.50 for i in range(1, 5)}  # baseline mean 0.50
        values[_label_at(0)] = 0.75  # +25pp > 20pp
        fx.install_metrics(monkeypatch, (fx.per_week_metric(metric_id, values),))

        findings = detect_citation_chain_shift(weeks, self._CONFIG)
        assert any(f.subject == CitationSource.SA_TECH.value for f in findings)

    def test_does_not_fire_at_threshold_boundary(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        weeks = _weeks(5)
        metric_id = self._metric_id()
        values = {_label_at(i): 0.50 for i in range(1, 5)}
        values[_label_at(0)] = 0.70  # exactly +20pp → not strictly greater
        fx.install_metrics(monkeypatch, (fx.per_week_metric(metric_id, values),))

        assert detect_citation_chain_shift(weeks, self._CONFIG) == ()

    def test_fires_on_increase_either_direction(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        weeks = _weeks(5)
        metric_id = self._metric_id()
        values = {_label_at(i): 0.50 for i in range(1, 5)}
        values[_label_at(0)] = 0.25  # -25pp, citation shift fires on any direction
        fx.install_metrics(monkeypatch, (fx.per_week_metric(metric_id, values),))

        assert detect_citation_chain_shift(weeks, self._CONFIG) != ()


# ---------------------------------------------------------------------------
# Detector 5 — source signal survival drop
# ---------------------------------------------------------------------------


class TestSourceSignalSurvivalDrop:
    _CONFIG = SourceSignalSurvivalDrop(baseline_window_weeks=4, delta_pp_threshold=20.0)

    def _metric_id(self) -> str:
        from alphamind.feedback_loop.citation.chain import metric_id_for

        return str(metric_id_for("signal_survival_rate", CitationSource.QR))

    def test_fires_on_drop_above_threshold(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        weeks = _weeks(5)
        metric_id = self._metric_id()
        values = {_label_at(i): 0.60 for i in range(1, 5)}
        values[_label_at(0)] = 0.35  # -25pp drop > 20pp
        fx.install_metrics(monkeypatch, (fx.per_week_metric(metric_id, values),))

        findings = detect_source_signal_survival_drop(weeks, self._CONFIG)
        assert any(f.subject == CitationSource.QR.value for f in findings)

    def test_does_not_fire_on_increase(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        weeks = _weeks(5)
        metric_id = self._metric_id()
        values = {_label_at(i): 0.40 for i in range(1, 5)}
        values[_label_at(0)] = 0.80  # +40pp increase — a drop detector ignores rises
        fx.install_metrics(monkeypatch, (fx.per_week_metric(metric_id, values),))

        assert detect_source_signal_survival_drop(weeks, self._CONFIG) == ()

    def test_does_not_fire_at_drop_boundary(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        weeks = _weeks(5)
        metric_id = self._metric_id()
        values = {_label_at(i): 0.50 for i in range(1, 5)}
        values[_label_at(0)] = 0.30  # exactly -20pp → not strictly greater
        fx.install_metrics(monkeypatch, (fx.per_week_metric(metric_id, values),))

        assert detect_source_signal_survival_drop(weeks, self._CONFIG) == ()


# ---------------------------------------------------------------------------
# Detector 6 — validation reaching window end
# ---------------------------------------------------------------------------


class TestValidationWindowEnd:
    _CONFIG = ValidationWindowEnd(days_before_due=7)

    def _current_with(self, *, due_at: datetime):  # type: ignore[no-untyped-def]
        val = fx.validation(validation_id="val-1", evaluation_due_at=due_at)
        current = fx.dataset(monday=_MONDAY, validations=(val,))
        return [*fx.week_sequence(2, base=_MONDAY), (_MONDAY.date().isoformat(), current)]

    def test_fires_within_days_before_due(self) -> None:
        # Window end is _MONDAY + 7d. Due 3 days after window end → within 7-day band.
        due = (_MONDAY + timedelta(days=7)) + timedelta(days=3)
        findings = detect_validation_window_end(self._current_with(due_at=due), self._CONFIG)
        assert len(findings) == 1
        assert "due soon" in findings[0].detail

    def test_fires_when_overdue(self) -> None:
        due = _MONDAY  # before the window end → overdue
        findings = detect_validation_window_end(self._current_with(due_at=due), self._CONFIG)
        assert len(findings) == 1
        assert "overdue" in findings[0].detail

    def test_does_not_fire_when_far_from_due(self) -> None:
        # Due 30 days past window end → well outside the 7-day band.
        due = (_MONDAY + timedelta(days=7)) + timedelta(days=30)
        assert detect_validation_window_end(self._current_with(due_at=due), self._CONFIG) == ()


# ---------------------------------------------------------------------------
# Detector 7 — validation superseded
# ---------------------------------------------------------------------------


class TestValidationSuperseded:
    def _current_with_superseded(self, supers: tuple):  # type: ignore[no-untyped-def]
        current = fx.dataset(monday=_MONDAY, superseded_validations=supers)
        return [*fx.week_sequence(2, base=_MONDAY), (_MONDAY.date().isoformat(), current)]

    def test_fires_per_superseded_validation(self) -> None:
        val = fx.validation(
            validation_id="val-9",
            evaluation_due_at=_MONDAY + timedelta(days=30),
            superseded_at=_MONDAY + timedelta(days=2),
            superseded_reason=SupersededReason.REGIME_TRANSITION,
        )
        weeks = self._current_with_superseded((val,))
        findings = detect_validation_superseded(weeks, enabled=True)
        assert len(findings) == 1
        assert findings[0].subject == "val-9"
        assert "regime_transition" in findings[0].detail

    def test_no_superseded_no_finding(self) -> None:
        weeks = self._current_with_superseded(())
        assert detect_validation_superseded(weeks, enabled=True) == ()

    def test_disabled_never_fires(self) -> None:
        val = fx.validation(
            validation_id="val-9",
            evaluation_due_at=_MONDAY + timedelta(days=30),
            superseded_at=_MONDAY + timedelta(days=2),
            superseded_reason=SupersededReason.MODEL_VERSION_CHANGE,
        )
        weeks = self._current_with_superseded((val,))
        assert detect_validation_superseded(weeks, enabled=False) == ()


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


class TestDetectShifts:
    def test_dispatcher_runs_all_detectors_without_error(self) -> None:
        from pathlib import Path

        from alphamind.config.loaders import read_yaml_file
        from alphamind.config.models.digest import DigestConfig
        from alphamind.feedback_loop.digest.shifts import detect_shifts

        config = DigestConfig.model_validate(read_yaml_file(Path("config/digest.yaml")))
        # Empty weeks (no planted metrics, no validations) → all detectors degrade.
        result = detect_shifts(fx.week_sequence(5, base=_MONDAY), config)
        assert result == ()

    def test_regime_change_config_uses_enabled_flag(self) -> None:
        # Confirm the config model exposes the flag the dispatcher passes.
        assert RegimeChange(enabled=True).enabled is True
        assert ValidationSuperseded(enabled=False).enabled is False


@pytest.mark.parametrize("count", [1])
def test_single_week_baselines_empty(count: int) -> None:
    # A one-week sequence has no baseline; metric-driven detectors must not error.
    cfg = AntiPatternSpike(
        baseline_window_weeks=4, multiplier_vs_baseline=2.0, min_occurrences_this_week=5
    )
    assert detect_anti_pattern_spike(fx.week_sequence(count, base=_MONDAY), cfg) == ()
