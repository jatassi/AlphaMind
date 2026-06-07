"""Weekly-digest section assembly + determinism + graceful degradation (ALP-888).

The generator is pure over a ``Sequence[(week_label, WindowDataset)]``. These tests
build that sequence by hand (no DB) and assert the six sections' contents, the
multi-week trajectory, week-over-week deltas, graceful degradation of unregistered
metrics, and byte-for-byte determinism.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from alphamind.feedback_loop.digest import generator as gen
from alphamind.feedback_loop.digest.generator import WeeklyDigest, generate_digest
from tests.feedback_loop.digest import _fixtures as fx


@pytest.fixture
def digest_config():  # type: ignore[no-untyped-def]
    from alphamind.config.loaders import read_yaml_file
    from alphamind.config.models.digest import DigestConfig

    return DigestConfig.model_validate(read_yaml_file(Path("config/digest.yaml")))


class TestSectionAssembly:
    def test_six_sections_present_over_multi_week_sequence(self, digest_config) -> None:  # type: ignore[no-untyped-def]
        weeks = fx.week_sequence(10)
        digest = generate_digest(weeks, digest_config)

        assert isinstance(digest, WeeklyDigest)
        # The current week is the last input.
        assert digest.week == weeks[-1][0]
        # All six sections exist as their typed shapes.
        assert digest.headline is not None
        assert digest.pulse is not None
        assert digest.trajectory is not None
        assert isinstance(digest.validation_status, tuple)
        assert isinstance(digest.notable_shifts, tuple)
        assert digest.replay_queue is not None

    def test_trajectory_spans_every_input_week(self, digest_config) -> None:  # type: ignore[no-untyped-def]
        weeks = fx.week_sequence(11)
        digest = generate_digest(weeks, digest_config)

        # Each sparkline carries one point per input week, oldest-to-newest.
        sparkline = digest.trajectory.weekly_pl
        assert len(sparkline.points) == 11
        assert [p.week for p in sparkline.points] == [label for label, _ in weeks]

    def test_trajectory_reflects_per_week_metric_value(self, monkeypatch, digest_config) -> None:  # type: ignore[no-untyped-def]
        weeks = fx.week_sequence(8)
        labels = [label for label, _ in weeks]
        # A registered win-rate metric that returns a distinct value per week.
        values = {label: 0.5 + 0.01 * i for i, label in enumerate(labels)}
        fx.install_metrics(monkeypatch, (fx.per_week_metric("outcome_win_rate", values),))

        digest = generate_digest(weeks, digest_config)
        win_rate = digest.trajectory.win_rate
        assert [p.cell.value for p in win_rate.points] == [values[label] for label in labels]

    def test_pulse_week_over_week_delta(self, monkeypatch, digest_config) -> None:  # type: ignore[no-untyped-def]
        weeks = fx.week_sequence(3)
        labels = [label for label, _ in weeks]
        # PM rejection-rate registered id: prior week 0.2, current 0.5.
        values = {labels[-2]: 0.2, labels[-1]: 0.5}
        fx.install_metrics(monkeypatch, (fx.per_week_metric("pm_verdict_rate__reject", values),))

        digest = generate_digest(weeks, digest_config)
        pulse = digest.pulse.pm_rejection_rate
        assert pulse.current.value == 0.5
        assert pulse.prior.value == 0.2
        assert pulse.delta == pytest.approx(0.3)

    def test_anti_pattern_bar_group_has_one_cell_per_canonical_pattern(
        self, digest_config
    ) -> None:  # type: ignore[no-untyped-def]
        digest = generate_digest(fx.week_sequence(4), digest_config)
        assert len(digest.pulse.anti_pattern_frequencies) == 5

    def test_validation_status_row_per_pending_validation(self, digest_config) -> None:  # type: ignore[no-untyped-def]
        monday = datetime(2026, 4, 6, tzinfo=UTC)  # a Monday
        val = fx.validation(
            validation_id="val-1",
            evaluation_due_at=datetime(2026, 5, 1, tzinfo=UTC),
            edited_artifact="prompts/decision/pm.md",
        )
        current = fx.dataset(monday=monday, validations=(val,))
        weeks = [*fx.week_sequence(2), (monday.date().isoformat(), current)]

        digest = generate_digest(weeks, digest_config)
        assert len(digest.validation_status) == 1
        row = digest.validation_status[0]
        assert row.validation_id == "val-1"
        assert row.edited_artifact == "prompts/decision/pm.md"
        assert row.expected_direction == "improved"


class TestGracefulDegradation:
    def test_unregistered_metric_renders_empty_not_error(self, digest_config) -> None:  # type: ignore[no-untyped-def]
        # PM-accuracy (06e) and the 7d-headline metrics (06f) are unregistered today.
        digest = generate_digest(fx.week_sequence(9), digest_config)

        # PM rejection-accuracy sparkline: every point's cell is absent (None result).
        for point in digest.trajectory.pm_rejection_accuracy.points:
            assert point.cell.result is None
            assert point.cell.present is False
            assert point.cell.value is None

        # Headline 7d P/L is unregistered → absent cell, not a raised error.
        assert digest.headline.pl_last_7d.result is None

    def test_replay_queue_empty_renders_zero(self, digest_config) -> None:  # type: ignore[no-untyped-def]
        digest = generate_digest(fx.week_sequence(4), digest_config)
        assert digest.replay_queue.total_attempted == 0
        assert digest.replay_queue.by_status == {}


class TestDeterminism:
    def test_same_sequence_yields_identical_digest(self, monkeypatch, digest_config) -> None:  # type: ignore[no-untyped-def]
        weeks = fx.week_sequence(10)
        # Plant a couple of live metrics so the digest is non-trivially populated.
        labels = [label for label, _ in weeks]
        fx.install_metrics(
            monkeypatch,
            (
                fx.per_week_metric(
                    "outcome_win_rate", {label: 0.6 for label in labels}
                ),
                fx.constant_metric("pm_modification_rate", 0.1),
            ),
        )

        first = generate_digest(weeks, digest_config)
        second = generate_digest(weeks, digest_config)
        assert first == second

    def test_empty_sequence_raises(self, digest_config) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(ValueError, match="at least one week"):
            generate_digest([], digest_config)


class TestPurity:
    def test_generator_module_imports_no_sqlalchemy(self) -> None:
        # The pure core must not pull the session layer (enforced by import-linter;
        # this is a fast local guard that the module's transitive imports stay clean).
        import sys

        # Importing the generator should not have imported sqlalchemy transitively
        # via a session/repository path. (sqlalchemy may already be loaded by other
        # test modules, so we assert the generator does not *reference* it.)
        assert "sqlalchemy" not in dir(gen)
        assert sys.modules.get("alphamind.feedback_loop.digest.generator") is not None
