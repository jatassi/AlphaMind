"""Analytics-read CLI entrypoint tests (ALP-888).

Invokes ``cli.main`` against an on-disk SQLite (the only DB-touching surface) and a
real ``config/`` dir, asserting valid digest JSON, metric-by-id JSON, and the
metrics-list output. The DB is empty-but-schema-complete: the digest's registry
metrics read zero rows and degrade gracefully, which is exactly the surface the CLI
must emit without erroring.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

import alphamind.state.tables  # noqa: F401 — register all tables on Base.metadata
from alphamind.feedback_loop.digest import cli
from alphamind.feedback_loop.metrics.types import MetricId, MetricResult
from alphamind.persistence.models import Base
from alphamind.persistence.session import make_engine


def _reject_non_finite(token: str) -> float:
    msg = f"non-finite JSON token {token!r} is not valid RFC 8259 JSON"
    raise ValueError(msg)


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    """An on-disk SQLite with the full schema created and no rows."""
    path = tmp_path / "digest_cli.db"
    engine = make_engine(str(path))
    Base.metadata.create_all(engine)
    engine.dispose()
    return str(path)


class TestDigestCommand:
    def test_emits_valid_digest_json(self, db_path: str, capsys) -> None:  # type: ignore[no-untyped-def]
        rc = cli.main(
            [
                "--db-path",
                db_path,
                "digest",
                "--week",
                "2026-05-13",
                "--trajectory-weeks",
                "8",
            ]
        )
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)

        # The six sections are present in the serialised digest.
        assert payload["week"] == "2026-05-11"  # Monday of the 2026-05-13 week
        assert set(payload) >= {
            "week",
            "headline",
            "pulse",
            "trajectory",
            "validation_status",
            "notable_shifts",
            "replay_queue",
        }
        # Trajectory has one point per loaded week (8 requested).
        assert len(payload["trajectory"]["weekly_pl"]["points"]) == 8
        # Empty DB → replay queue zero, validation rows empty, no shifts.
        assert payload["replay_queue"]["total_attempted"] == 0
        assert payload["validation_status"] == []
        assert payload["notable_shifts"] == []

    def test_invalid_week_is_arg_error(self, db_path: str, capsys) -> None:  # type: ignore[no-untyped-def]
        rc = cli.main(["--db-path", db_path, "digest", "--week", "not-a-date"])
        assert rc == 1
        assert "not a valid ISO date" in capsys.readouterr().out

    def test_defaults_to_current_week(self, db_path: str, capsys) -> None:  # type: ignore[no-untyped-def]
        rc = cli.main(["--db-path", db_path, "digest", "--trajectory-weeks", "2"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert len(payload["trajectory"]["weekly_pl"]["points"]) == 2


class TestMetricCommand:
    def test_emits_metric_result_json(self, db_path: str, capsys) -> None:  # type: ignore[no-untyped-def]
        rc = cli.main(
            [
                "--db-path",
                db_path,
                "metric",
                "pm_approval_rate",
                "--window",
                "1",
                "--week",
                "2026-05-13",
            ]
        )
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["metric_id"] == "pm_approval_rate"
        # Empty DB → no PM decisions → empty-sample reading (value None), not an error.
        assert payload["value"] is None
        assert payload["sample_size"] == 0
        assert "insufficient_sample" in payload

    def test_unknown_metric_is_arg_error(self, db_path: str, capsys) -> None:  # type: ignore[no-untyped-def]
        rc = cli.main(["--db-path", db_path, "metric", "no_such_metric"])
        assert rc == 1
        assert "unknown metric id" in capsys.readouterr().out


class TestNonFiniteMetricValue:
    """A non-finite ``MetricResult.value`` (an all-wins ``outcome_profit_factor`` yields
    ``math.inf``) must emit standard JSON from the ``metric`` command, not a bare
    ``Infinity`` token."""

    def test_metric_json_serializes_non_finite_as_standard_json(self) -> None:
        result = MetricResult(
            metric_id=MetricId("outcome_profit_factor"),
            value=math.inf,
            posterior_band=None,
            sample_size=5,
            insufficient_sample=False,
        )
        text = json.dumps(cli._metric_result_json(result), indent=2, allow_nan=False)
        # A strict reader rejecting non-finite constants parses it (no bare Infinity).
        payload = json.loads(text, parse_constant=_reject_non_finite)
        assert payload["value"] == "Infinity"


class TestMetricsListCommand:
    def test_lists_registered_metric_ids(self, capsys) -> None:  # type: ignore[no-untyped-def]
        rc = cli.main(["metrics-list"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        ids = {entry["metric_id"] for entry in payload}
        # A representative registered id from each producing story is present.
        assert "pm_approval_rate" in ids  # 06a
        assert "outcome_win_rate" in ids  # 06c
        # Each entry carries its classification.
        sample = next(e for e in payload if e["metric_id"] == "pm_approval_rate")
        assert sample["po_type"] == "process"
        assert sample["default_window"] == "weekly"
