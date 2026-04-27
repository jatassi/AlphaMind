"""
Polygon reference data collection.

Exports
-------
collect_reference(ticker_scope, ...)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from alphamind.data_sources._common import (
    RetryShape,
    active_universe_tickers,
    default_session_factory,
    track_run,
    with_retries,
)
from alphamind.data_sources.polygon.client import PolygonClient
from alphamind.persistence.models import AssetUniverse


def collect_reference(
    ticker_scope: list[str] | None = None,
    since: Any = None,
    *,
    _client: Any = None,
    _session_factory: Any = None,
    _repo: Any = None,
) -> None:
    """
    Fetch ticker details from ``/v3/reference/tickers/{ticker}`` and update
    mutable fields in ``asset_universe``:
    ``market_cap_usd``, ``shares_outstanding``, ``float_shares``, ``last_updated``.

    Run weekly. No bootstrap-specific function — same call serves both.

    Parameters
    ----------
    ticker_scope:
        Both universe and benchmark tickers are accepted.  Defaults to all
        active tickers (universe + benchmarks) from ``asset_universe``.
    since:
        Accepted for runner-contract compatibility; reference data is
        always current state so the value does not affect collection.
    """
    del since  # accepted for runner-contract compatibility; ignored
    if _client is None:
        _client = PolygonClient()
    _session_factory = _session_factory or default_session_factory()
    if ticker_scope is None:
        ticker_scope = active_universe_tickers(
            include_benchmarks=True, session_factory=_session_factory
        )

    @with_retries(RetryShape.critical, _sleep=lambda _: None)
    def _fetch_details(ticker: str) -> Any:
        return _client.get_ticker_details(ticker)

    with track_run("polygon.reference", _repo=_repo) as run:
        rows_written = 0
        single = len(ticker_scope) == 1

        for ticker in ticker_scope:
            try:
                details = _fetch_details(ticker)
            except Exception:
                if single:
                    raise
                continue

            last_updated = datetime.now(UTC).isoformat()
            market_cap = float(details.market_cap) if details.market_cap is not None else None
            shares_outstanding = (
                int(details.weighted_shares_outstanding)
                if details.weighted_shares_outstanding is not None
                else None
            )
            float_shares = (
                int(details.share_class_shares_outstanding)
                if details.share_class_shares_outstanding is not None
                else None
            )

            with _session_factory() as sess:
                row = sess.query(AssetUniverse).filter_by(ticker=ticker).first()
                if row is not None:
                    row.market_cap_usd = market_cap
                    row.shares_outstanding = shares_outstanding
                    row.float_shares = float_shares
                    row.last_updated = last_updated
                    sess.commit()
                    rows_written += 1

        run.rows_written = rows_written
