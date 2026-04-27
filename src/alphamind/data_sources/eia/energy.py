"""
EIA energy series collection functions.

Writes ``macro_observations`` rows for the four configured EIA series:
    - eia.crude_inventory_total
    - eia.nat_gas_storage_lower_48
    - eia.wti_spot_price
    - eia.refinery_utilization

Public API
----------
collect_series(series, since, *, _repo, _client, _session_factory)
    Pull data for each series entry and persist to ``macro_observations``.

bootstrap_series(*, _repo, _session_factory)
    One-time historical backfill covering 252 days per lifecycle.md § Bootstrap.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from typing import Any

from alphamind.data_sources._common import track_run
from alphamind.data_sources.eia.series import SERIES
from alphamind.persistence.models import MacroObservations

# ---------------------------------------------------------------------------
# Units normalisation
# ---------------------------------------------------------------------------

_UNITS_MAP: dict[str, str] = {
    "MBBL": "bbl",
    "MBBL/D": "bbl",
    "BCF": "bcf",
    "TCF": "bcf",
    "DOLLARS PER BARREL": "usd",
    "$/BBL": "usd",
    "PCT": "pct",
    "PERCENT": "pct",
}


def _normalise_units(raw: str | None) -> str | None:
    if raw is None:
        return None
    return _UNITS_MAP.get(raw.upper(), raw.lower())


# ---------------------------------------------------------------------------
# Default client factory (production path)
# ---------------------------------------------------------------------------


def _make_default_client() -> Any:
    """Construct an EIAClient from environment / config."""
    from alphamind.data_sources.eia.client import EIAClient

    api_key = os.environ.get("EIA_API_KEY", "")
    return EIAClient(api_key=api_key)


# ---------------------------------------------------------------------------
# collect_series
# ---------------------------------------------------------------------------


def collect_series(
    series: list[dict[str, Any]],
    since: date,
    *,
    _repo: Any = None,
    _client: Any = None,
    _session_factory: Any = None,
) -> None:
    """
    Pull data for each series entry and persist to ``macro_observations``.

    All rows for a given run are written inside a single ``track_run`` context
    so that any fetch failure rolls back the entire run and records ``failed``
    in ``collection_runs`` with no partial data rows.

    Parameters
    ----------
    series:
        List of series dicts from :mod:`~alphamind.data_sources.eia.series`.
    since:
        Earliest observation date to fetch (inclusive).
    _repo:
        Optional collection-runs repository override (for testing).
    _client:
        Optional EIAClient override (for testing).
    _session_factory:
        Optional SQLAlchemy session factory override (for testing).
    """
    client = _client if _client is not None else _make_default_client()

    if _session_factory is None:
        from alphamind.persistence.models import Base
        from alphamind.persistence.session import make_engine, make_session_factory

        engine = make_engine()
        Base.metadata.create_all(engine)
        _session_factory = make_session_factory(engine)

    with track_run("eia.energy", _repo=_repo) as run:
        rows_to_write: list[MacroObservations] = []

        for entry in series:
            data_points = client.fetch_series(
                route=entry["route"],
                facets=entry["facets"],
                frequency=entry["frequency"],
                since=since,
            )
            for point in data_points:
                rows_to_write.append(
                    MacroObservations(
                        source="eia",
                        series_id=entry["series_id"],
                        observation_date=point["period"],
                        revision_number=0,
                        value=point.get("value"),
                        units=_normalise_units(point.get("units")),
                        frequency=entry["frequency"],
                        ingested_at=datetime.now(UTC).isoformat(),
                    )
                )

        inserted = 0
        with _session_factory() as sess:
            for row in rows_to_write:
                pk = (row.source, row.series_id, row.observation_date, row.revision_number)
                if sess.get(MacroObservations, pk) is None:
                    sess.add(row)
                    inserted += 1
            sess.commit()

        run.rows_written = inserted


# ---------------------------------------------------------------------------
# bootstrap_series
# ---------------------------------------------------------------------------


def bootstrap_series(
    *,
    _repo: Any = None,
    _client: Any = None,
    _session_factory: Any = None,
) -> None:
    """
    One-time historical backfill covering 252 trading days per lifecycle.md.

    Calls :func:`collect_series` with ``since = today - 252 days``.
    """
    since = datetime.now(UTC).date() - timedelta(days=252)
    collect_series(
        SERIES,
        since,
        _repo=_repo,
        _client=_client,
        _session_factory=_session_factory,
    )
