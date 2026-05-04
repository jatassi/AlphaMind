"""
Polygon options chain collection.

Exports
-------
collect_options_chains(ticker_scope, since, ...)
bootstrap_options_chains(...)  — no-op (start fresh per lifecycle.md)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from alphamind.data_sources._common import (
    RetryShape,
    active_universe_tickers,
    default_session_factory,
    track_run,
    with_retries,
)
from alphamind.data_sources.polygon.client import PolygonClient
from alphamind.persistence.models import OptionsContracts, OptionsContractSnapshots

# ALP-289: bulk INSERT...ON CONFLICT DO UPDATE replaced per-row sess.merge.
# Chunk size keeps each statement well below SQLite's 32_766 host-parameter
# limit (16 cols x 500 rows = 8000 params).
_UPSERT_CHUNK_SIZE = 500

_SNAPSHOT_UPDATE_COLS: tuple[str, ...] = (
    "underlying_ticker",
    "open_interest",
    "volume_today",
    "last_price",
    "bid",
    "ask",
    "implied_volatility",
    "delta",
    "gamma",
    "theta",
    "vega",
    "rho",
    "underlying_price",
    "source",
    "ingested_at",
)


def collect_options_chains(
    ticker_scope: list[str] | None = None,
    since: datetime | None = None,
    *,
    _client: Any = None,
    _session_factory: Any = None,
    _repo: Any = None,
    _snapshot_ts: str | None = None,
) -> None:
    """
    Pull full chain snapshot per universe ticker and write to:
    - ``options_contracts`` (UPSERT: insert new, update ``last_seen_at`` on known)
    - ``options_contract_snapshots`` (one row per contract per snapshot timestamp)

    Parameters
    ----------
    ticker_scope:
        Underlying tickers whose chains to collect.  Defaults to active
        equity-universe tickers (benchmarks excluded — options are equity-only).
    since:
        Accepted for interface uniformity; unused (options are forward-only
        snapshot collection).
    _snapshot_ts:
        Override the snapshot timestamp (injected in tests for idempotency).
    """
    del since  # accepted for runner-contract compatibility; ignored by snapshot semantics
    if _client is None:
        _client = PolygonClient()
    _session_factory = _session_factory or default_session_factory()
    if ticker_scope is None:
        ticker_scope = active_universe_tickers(
            include_benchmarks=False, session_factory=_session_factory
        )

    snapshot_ts = _snapshot_ts if _snapshot_ts is not None else datetime.now(UTC).isoformat()
    ingested_at = datetime.now(UTC).isoformat()

    @with_retries(RetryShape.important, _sleep=lambda _: None)
    def _fetch_chain(underlying: str) -> list[Any]:
        return _client.list_snapshot_options_chain(underlying)  # type: ignore[no-any-return]

    with track_run("polygon.options", _repo=_repo) as run:
        rows_written = 0
        single = len(ticker_scope) == 1

        for underlying in ticker_scope:
            try:
                snapshots = _fetch_chain(underlying)
            except Exception:
                if single:
                    raise
                continue

            contract_rows, snapshot_rows = _build_rows(
                snapshots, underlying, snapshot_ts, ingested_at
            )
            _upsert_contracts(_session_factory, contract_rows, snapshot_ts)
            _upsert_snapshots(_session_factory, snapshot_rows)
            rows_written += len(snapshot_rows)

        run.rows_written = rows_written


def _build_rows(
    snapshots: list[Any],
    underlying: str,
    snapshot_ts: str,
    ingested_at: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Convert a Polygon snapshot list to row-dicts for both options tables."""
    contract_rows: list[dict[str, Any]] = []
    snapshot_rows: list[dict[str, Any]] = []
    for snap in snapshots:
        details = snap.details
        greeks = snap.greeks
        day = snap.day
        lq = snap.last_quote
        lt = snap.last_trade
        ua = snap.underlying_asset

        contract_rows.append(
            {
                "contract_ticker": details.ticker,
                "underlying_ticker": underlying,
                "expiration_date": details.expiration_date,
                "strike_price": float(details.strike_price),
                "contract_type": details.contract_type,
                "first_seen_at": snapshot_ts,
                "last_seen_at": snapshot_ts,
                "source": "polygon",
            }
        )
        rho_val = getattr(snap, "rho", None)
        snapshot_rows.append(
            {
                "snapshot_ts": snapshot_ts,
                "contract_ticker": details.ticker,
                "underlying_ticker": underlying,
                "open_interest": (
                    int(snap.open_interest) if snap.open_interest is not None else None
                ),
                "volume_today": int(day.volume) if day and day.volume is not None else None,
                "last_price": float(lt.price) if lt and lt.price is not None else None,
                "bid": float(lq.bid) if lq and lq.bid is not None else None,
                "ask": float(lq.ask) if lq and lq.ask is not None else None,
                "implied_volatility": float(snap.implied_volatility)
                if snap.implied_volatility is not None
                else None,
                "delta": float(greeks.delta) if greeks and greeks.delta is not None else None,
                "gamma": float(greeks.gamma) if greeks and greeks.gamma is not None else None,
                "theta": float(greeks.theta) if greeks and greeks.theta is not None else None,
                "vega": float(greeks.vega) if greeks and greeks.vega is not None else None,
                "rho": float(rho_val) if rho_val is not None else None,
                "underlying_price": float(ua.price) if ua and ua.price is not None else None,
                "source": "polygon",
                "ingested_at": ingested_at,
            }
        )
    return contract_rows, snapshot_rows


def _upsert_contracts(session_factory: Any, rows: list[dict[str, Any]], snapshot_ts: str) -> None:
    """Bulk-upsert per contract.

    INSERT new contracts (keeping ``first_seen_at`` from the inbound row);
    on conflict refresh ``last_seen_at`` only so the audit trail of when a
    contract was first observed is preserved.
    """
    del snapshot_ts  # not needed: last_seen_at is already set in each row
    if not rows:
        return
    with session_factory() as sess:
        for i in range(0, len(rows), _UPSERT_CHUNK_SIZE):
            chunk = rows[i : i + _UPSERT_CHUNK_SIZE]
            stmt = sqlite_insert(OptionsContracts).values(chunk)
            stmt = stmt.on_conflict_do_update(
                index_elements=[OptionsContracts.contract_ticker],
                set_={"last_seen_at": stmt.excluded.last_seen_at},
            )
            sess.execute(stmt)
        sess.commit()


def _upsert_snapshots(session_factory: Any, rows: list[dict[str, Any]]) -> None:
    """Bulk-upsert snapshot rows keyed on ``(snapshot_ts, contract_ticker)``.

    Re-running with the same primary key replaces the row's payload so
    duplicate or re-fired runs at a given snapshot timestamp are idempotent.
    """
    if not rows:
        return
    with session_factory() as sess:
        for i in range(0, len(rows), _UPSERT_CHUNK_SIZE):
            chunk = rows[i : i + _UPSERT_CHUNK_SIZE]
            stmt = sqlite_insert(OptionsContractSnapshots).values(chunk)
            stmt = stmt.on_conflict_do_update(
                index_elements=[
                    OptionsContractSnapshots.snapshot_ts,
                    OptionsContractSnapshots.contract_ticker,
                ],
                set_={col: getattr(stmt.excluded, col) for col in _SNAPSHOT_UPDATE_COLS},
            )
            sess.execute(stmt)
        sess.commit()


def bootstrap_options_chains() -> None:
    """No-op: options chains start fresh per lifecycle.md § Bootstrap."""
