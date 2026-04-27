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

from alphamind.data_sources._common import RetryShape, track_run, with_retries
from alphamind.data_sources.polygon.client import PolygonClient
from alphamind.persistence.models import OptionsContracts, OptionsContractSnapshots


def collect_options_chains(
    ticker_scope: list[str],
    _since: datetime | None = None,
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
        Underlying tickers whose chains to collect.
    _snapshot_ts:
        Override the snapshot timestamp (injected in tests for idempotency).
    """
    if _client is None:
        _client = PolygonClient()

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

            if _session_factory is None:
                continue

            contract_rows: list[OptionsContracts] = []
            snapshot_rows: list[OptionsContractSnapshots] = []

            for snap in snapshots:
                details = snap.details
                contract_ticker = details.ticker
                exp_date = details.expiration_date
                strike = details.strike_price
                contract_type = details.contract_type

                greeks = snap.greeks
                delta = greeks.delta if greeks else None
                gamma = greeks.gamma if greeks else None
                theta = greeks.theta if greeks else None
                vega = greeks.vega if greeks else None

                iv = snap.implied_volatility
                oi = snap.open_interest
                day = snap.day
                volume = day.volume if day else None
                lq = snap.last_quote
                bid = lq.bid if lq else None
                ask = lq.ask if lq else None
                lt = snap.last_trade
                last_price = lt.price if lt else None
                ua = snap.underlying_asset
                underlying_price = ua.price if ua else None
                rho = getattr(snap, "rho", None)

                contract_rows.append(
                    OptionsContracts(
                        contract_ticker=contract_ticker,
                        underlying_ticker=underlying,
                        expiration_date=exp_date,
                        strike_price=float(strike),
                        contract_type=contract_type,
                        first_seen_at=snapshot_ts,
                        last_seen_at=snapshot_ts,
                        source="polygon",
                    )
                )
                snapshot_rows.append(
                    OptionsContractSnapshots(
                        snapshot_ts=snapshot_ts,
                        contract_ticker=contract_ticker,
                        underlying_ticker=underlying,
                        open_interest=int(oi) if oi is not None else None,
                        volume_today=int(volume) if volume is not None else None,
                        last_price=float(last_price) if last_price is not None else None,
                        bid=float(bid) if bid is not None else None,
                        ask=float(ask) if ask is not None else None,
                        implied_volatility=float(iv) if iv is not None else None,
                        delta=float(delta) if delta is not None else None,
                        gamma=float(gamma) if gamma is not None else None,
                        theta=float(theta) if theta is not None else None,
                        vega=float(vega) if vega is not None else None,
                        rho=float(rho) if rho is not None else None,
                        underlying_price=float(underlying_price)
                        if underlying_price is not None
                        else None,
                        source="polygon",
                        ingested_at=ingested_at,
                    )
                )

            _upsert_contracts(_session_factory, contract_rows, snapshot_ts)
            _upsert_snapshots(_session_factory, snapshot_rows)
            rows_written += len(snapshot_rows)

        run.rows_written = rows_written


def _upsert_contracts(session_factory: Any, rows: list[OptionsContracts], snapshot_ts: str) -> None:
    """
    For each contract: INSERT if new (preserving ``first_seen_at``),
    UPDATE ``last_seen_at`` if already present.
    """
    if not rows:
        return
    with session_factory() as sess:
        for row in rows:
            existing = sess.get(OptionsContracts, row.contract_ticker)
            if existing is None:
                sess.add(row)
            else:
                existing.last_seen_at = snapshot_ts
        sess.commit()


def _upsert_snapshots(session_factory: Any, rows: list[OptionsContractSnapshots]) -> None:
    """UPSERT snapshot rows keyed on (snapshot_ts, contract_ticker)."""
    if not rows:
        return
    with session_factory() as sess:
        for row in rows:
            sess.merge(row)
        sess.commit()


def bootstrap_options_chains() -> None:
    """No-op: options chains start fresh per lifecycle.md § Bootstrap."""
