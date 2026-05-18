"""Collection-run tracking helpers writing to the ``collection_runs`` table.

Provides:
- :func:`track_run` — context manager that inserts a ``running`` row and
  updates it to ``success`` / ``failed`` on exit.
- :class:`RunState` — mutable state object yielded by ``track_run`` so the
  collector can set ``rows_written`` before exit.
- :func:`default_session_factory` — bootstrap a session bound to the default
  DB. Co-located here because every consumer that imports ``track_run`` also
  needs a session factory.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

__all__ = ["RunState", "default_session_factory", "track_run"]


@dataclass
class RunState:
    """Mutable state object yielded by :func:`track_run`.

    Collectors set ``error_summary`` to record a degraded-but-successful run
    (e.g. partial per-target failures). When set, ``track_run`` writes the
    summary to the ``collection_runs.error_summary`` column on a
    ``status='success'`` row — callers querying for "clean" runs should
    filter on ``error_summary IS NULL``, not just ``status='success'``.
    """

    rows_written: int = 0
    error_summary: str | None = None


class _DefaultRepo:
    """Production repository — writes to the SQLAlchemy ``collection_runs`` table.

    Imported lazily so that importing this module does not force a DB
    connection at module load time.
    """

    def __init__(self) -> None:
        from alphamind.persistence.models import Base, CollectionRuns
        from alphamind.persistence.session import make_engine, make_session_factory

        engine = make_engine()
        Base.metadata.create_all(engine)
        self._Session = make_session_factory(engine)
        self._model = CollectionRuns

    def insert_running(self, run_id: str, collector: str, started_at: str) -> None:
        with self._Session() as sess:
            sess.add(
                self._model(
                    run_id=run_id,
                    collector=collector,
                    started_at=started_at,
                    status="running",
                )
            )
            sess.commit()

    def update_success(
        self,
        run_id: str,
        completed_at: str,
        rows_written: int,
        error_summary: str | None = None,
    ) -> None:
        with self._Session() as sess:
            row = sess.get(self._model, run_id)
            if row is not None:
                row.status = "success"
                row.completed_at = completed_at
                row.rows_written = rows_written
                row.error_summary = error_summary
                sess.commit()

    def update_failed(self, run_id: str, error_summary: str) -> None:
        with self._Session() as sess:
            row = sess.get(self._model, run_id)
            if row is not None:
                row.status = "failed"
                row.error_summary = error_summary
                row.completed_at = datetime.now(UTC).isoformat()
                sess.commit()


@contextmanager
def track_run(
    collector_name: str,
    *,
    _repo: Any = None,
) -> Generator[RunState]:
    """Context manager that tracks a collection run in ``collection_runs``.

    Parameters
    ----------
    collector_name:
        Human-readable name for the collector (e.g. ``"polygon.equity"``).
    _repo:
        Optional repository override for testing.  When *None*, a
        :class:`_DefaultRepo` connected to the configured SQLite database
        is used.

    Yields
    ------
    RunState
        Mutable object — callers set ``run.rows_written`` before exiting.
    """
    repo = _repo if _repo is not None else _DefaultRepo()
    run_id = str(uuid.uuid4())
    started_at = datetime.now(UTC).isoformat()

    repo.insert_running(run_id, collector_name, started_at)

    run = RunState()
    try:
        yield run
    except BaseException as exc:
        # Failed-run writer per runtime §G1: ``BaseException`` (vs
        # ``Exception``) is intentional — operator-initiated
        # ``KeyboardInterrupt`` or ``CancelledError`` must still write a
        # failed-run row so the ``collection_runs`` audit trail records every
        # invocation. The exception is always re-raised so the supervisor
        # sees the original signal unchanged.
        error_summary = f"{type(exc).__name__}: {exc}"
        repo.update_failed(run_id, error_summary)
        raise
    else:
        completed_at = datetime.now(UTC).isoformat()
        repo.update_success(run_id, completed_at, run.rows_written, error_summary=run.error_summary)


def default_session_factory() -> Any:
    """Build a session factory bound to the default ``make_engine`` DB.

    Equivalent to::

        engine = make_engine()
        Base.metadata.create_all(engine)
        return make_session_factory(engine)
    """
    from alphamind.persistence.models import Base
    from alphamind.persistence.session import make_engine, make_session_factory

    engine = make_engine()
    Base.metadata.create_all(engine)
    return make_session_factory(engine)
