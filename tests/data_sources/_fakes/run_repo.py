"""Shared fake for the track_run repository (``collection_runs`` writer).

Most data_sources tests want a lightweight in-memory repo that captures
which runs were marked success vs failed so the test can assert on it.
This fake replaces the prior mock-library repo doubles.

Several test files defined a near-identical ``_FakeRunRepo`` inline; this
module consolidates them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeRunRepo:
    """In-memory ``track_run`` repo implementation."""

    rows: dict[str, dict[str, Any]] = field(default_factory=dict)

    def insert_running(self, run_id: str, collector: str, started_at: str) -> None:
        self.rows[run_id] = {
            "run_id": run_id,
            "collector": collector,
            "started_at": started_at,
            "status": "running",
            "completed_at": None,
            "rows_written": None,
            "error_summary": None,
        }

    def update_success(self, run_id: str, completed_at: str, rows_written: int) -> None:
        self.rows[run_id].update(
            status="success",
            completed_at=completed_at,
            rows_written=rows_written,
        )

    def update_failed(self, run_id: str, error_summary: str) -> None:
        self.rows[run_id].update(status="failed", error_summary=error_summary)

    # Convenience predicates for test assertions
    def succeeded(self) -> bool:
        """Return True when at least one tracked run ended with status='success'."""
        return any(row["status"] == "success" for row in self.rows.values())

    def failed(self) -> bool:
        """Return True when at least one tracked run ended with status='failed'."""
        return any(row["status"] == "failed" for row in self.rows.values())

    def latest(self) -> dict[str, Any]:
        """Return the most-recently inserted row.  Raises ``StopIteration`` when empty."""
        return next(iter(self.rows.values()))
