"""
BLS macro collection functions.

Writes ``macro_observations`` rows for employment and inflation series.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from alphamind.data_sources._common import track_run
from alphamind.data_sources.bls.client import BLSClient
from alphamind.data_sources.bls.series import SERIES
from alphamind.persistence.models import MacroObservations
from alphamind.persistence.session import make_engine, make_session_factory


def _resolve_api_key(api_key: str | None) -> str:
    if api_key is not None:
        return api_key
    env_value = os.environ.get("BLS_API_KEY")
    if not env_value:
        raise RuntimeError("BLS_API_KEY is not set in the environment")
    return env_value


def _period_to_date(year: str, period: str) -> str:
    """Convert BLS year + period to an ISO date string (first of month).

    BLS monthly period format: ``M01`` (January) through ``M13`` (annual).
    Only ``M01``-``M12`` are produced for monthly series.
    """
    month = int(period[1:])
    return date(int(year), month, 1).isoformat()


def _load_latest_revisions(
    sess: Session,
    series_ids: list[str],
) -> dict[tuple[str, str], MacroObservations]:
    """Return a mapping of (series_id, observation_date) → highest-revision row.

    Fetches all existing BLS rows for the given series IDs in one query,
    avoiding per-observation round trips.
    """
    subq = (
        select(
            MacroObservations.series_id,
            MacroObservations.observation_date,
            func.max(MacroObservations.revision_number).label("max_rev"),
        )
        .where(
            and_(
                MacroObservations.source == "bls",
                MacroObservations.series_id.in_(series_ids),
            )
        )
        .group_by(MacroObservations.series_id, MacroObservations.observation_date)
        .subquery()
    )
    rows = sess.execute(
        select(MacroObservations).join(
            subq,
            and_(
                MacroObservations.source == "bls",
                MacroObservations.series_id == subq.c.series_id,
                MacroObservations.observation_date == subq.c.observation_date,
                MacroObservations.revision_number == subq.c.max_rev,
            ),
        )
    ).scalars()
    return {(r.series_id, r.observation_date): r for r in rows}


def collect_series(
    series_ids: list[str] | None = None,
    *,
    since: date | None = None,
    api_key: str | None = None,
    session_factory: Any = None,
    client: Any = None,
) -> int:
    """
    Fetch BLS series since ``since`` and persist to ``macro_observations``.

    All parameters are optional so the runner can call ``collect_series()`` /
    ``collect_series(since=None)`` without context. Defaults:

    - ``series_ids`` → all configured series in ``SERIES``.
    - ``since`` → first day of last calendar month (BLS series are monthly).
    - ``api_key`` → ``BLS_API_KEY`` env var.
    - ``session_factory`` → default engine targeting the configured DB path.
    - ``client`` → :class:`BLSClient` constructed from ``api_key``; tests
      inject :class:`FakeBLSAPI` here.

    Returns the number of new rows written.
    """
    if series_ids is None:
        series_ids = list(SERIES.keys())
    if since is None:
        today = datetime.now(UTC).date()
        since = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    if client is None:
        api_key = _resolve_api_key(api_key)
        client = BLSClient(api_key=api_key)
    if session_factory is None:
        from alphamind.persistence.models import Base

        engine = make_engine()
        Base.metadata.create_all(engine)
        session_factory = make_session_factory(engine)

    now = datetime.now(UTC)
    start_year = str(since.year)
    end_year = str(now.year)
    ingested_at = now.isoformat()
    engine = session_factory.kw["bind"]

    with track_run("bls.macro", _repo=_make_repo(engine)) as run:
        series_results = client.post_timeseries(
            series_ids, start_year=start_year, end_year=end_year
        )
        with Session(engine) as sess:
            # Load all existing latest revisions in one query to avoid N+1.
            existing = _load_latest_revisions(sess, series_ids)

            for series in series_results:
                sid = series["seriesID"]
                meta = SERIES.get(sid, {})
                frequency = meta.get("frequency")
                units = meta.get("units")
                for point in series["data"]:
                    period = point["period"]
                    # Skip annual summaries (M13) and non-monthly periods.
                    if not period.startswith("M") or int(period[1:]) > 12:
                        continue
                    obs_date = _period_to_date(point["year"], period)
                    raw_value = point.get("value")
                    value = float(raw_value) if raw_value not in (None, "-") else None

                    prior = existing.get((sid, obs_date))
                    if prior is not None:
                        if prior.value == value:
                            continue
                        next_revision = prior.revision_number + 1
                    else:
                        next_revision = 0

                    new_row = MacroObservations(
                        source="bls",
                        series_id=sid,
                        observation_date=obs_date,
                        revision_number=next_revision,
                        value=value,
                        units=units,
                        frequency=frequency,
                        ingested_at=ingested_at,
                    )
                    sess.add(new_row)
                    # Update the in-memory index so a second revision within
                    # the same batch is detected correctly.
                    existing[(sid, obs_date)] = new_row
                    run.rows_written += 1
            sess.commit()

    return run.rows_written


def bootstrap_series(
    *,
    api_key: str | None = None,
    session_factory: Any = None,
    client: Any = None,
    _now: datetime | None = None,
) -> int:
    """Pull 24 months of history for all configured BLS series."""
    now = _now if _now is not None else datetime.now(UTC)
    return collect_series(
        list(SERIES.keys()),
        since=date(now.year - 2, 1, 1),
        api_key=api_key,
        session_factory=session_factory,
        client=client,
    )


# ---------------------------------------------------------------------------
# Internal: thin in-process repo adapter so track_run uses the injected engine
# rather than the production default.
# ---------------------------------------------------------------------------


def _make_repo(engine: Any) -> Any:
    """Return a track_run-compatible repo bound to *engine*."""
    from alphamind.persistence.models import CollectionRuns

    class _Repo:
        _model = CollectionRuns

        def insert_running(self, run_id: str, collector: str, started_at: str) -> None:
            with Session(engine) as sess:
                sess.add(
                    self._model(
                        run_id=run_id,
                        collector=collector,
                        started_at=started_at,
                        status="running",
                    )
                )
                sess.commit()

        def update_success(self, run_id: str, completed_at: str, rows_written: int) -> None:
            with Session(engine) as sess:
                row = sess.get(self._model, run_id)
                if row is not None:
                    row.status = "success"
                    row.completed_at = completed_at
                    row.rows_written = rows_written
                    sess.commit()

        def update_failed(self, run_id: str, error_summary: str) -> None:
            with Session(engine) as sess:
                row = sess.get(self._model, run_id)
                if row is not None:
                    row.status = "failed"
                    row.error_summary = error_summary
                    sess.commit()

    return _Repo()
