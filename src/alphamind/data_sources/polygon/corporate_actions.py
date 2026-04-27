"""
Polygon corporate actions collection (dividends + splits).

Exports
-------
collect_corporate_actions(ticker_scope, since, ...)
bootstrap_corporate_actions(...)
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

from alphamind.data_sources._common import RetryShape, track_run, with_retries
from alphamind.data_sources.polygon.client import PolygonClient
from alphamind.persistence.models import CorporateActions

_BOOTSTRAP_DAYS = 252


def collect_corporate_actions(
    ticker_scope: list[str],
    since: datetime | None = None,
    *,
    _client: Any = None,
    _session_factory: Any = None,
    _repo: Any = None,
) -> None:
    """
    Fetch dividends and splits for each ticker and UPSERT into
    ``corporate_actions``.

    Parameters
    ----------
    ticker_scope:
        Universe tickers (not benchmarks — corporate actions are equity-only).
    since:
        Lower bound for ``ex_date`` / ``execution_date`` filter.
    """
    if _client is None:
        _client = PolygonClient()
    if _session_factory is None:
        from alphamind.persistence.models import Base
        from alphamind.persistence.session import make_engine, make_session_factory

        engine = make_engine()
        Base.metadata.create_all(engine)
        _session_factory = make_session_factory(engine)

    since_str = since.strftime("%Y-%m-%d") if since is not None else None
    ingested_at = datetime.now(UTC).isoformat()

    @with_retries(RetryShape.important, _sleep=lambda _: None)
    def _fetch_dividends(ticker: str) -> list[Any]:
        return list(_client.list_dividends(ticker, ex_dividend_date_gte=since_str))

    @with_retries(RetryShape.important, _sleep=lambda _: None)
    def _fetch_splits(ticker: str) -> list[Any]:
        return list(_client.list_splits(ticker, execution_date_gte=since_str))

    with track_run("polygon.corporate_actions", _repo=_repo) as run:
        rows_written = 0
        single = len(ticker_scope) == 1

        for ticker in ticker_scope:
            try:
                dividends = _fetch_dividends(ticker)
            except Exception:
                if single:
                    raise
                continue

            splits = _fetch_splits(ticker)

            rows: list[CorporateActions] = []
            for d in dividends:
                action_id = d.id or _hash_id("polygon", ticker, d.ex_dividend_date, "dividend")
                rows.append(
                    CorporateActions(
                        action_id=action_id,
                        ticker=ticker,
                        action_type="cash_dividend",
                        declaration_date=d.declaration_date or None,
                        ex_date=d.ex_dividend_date,
                        record_date=d.record_date or None,
                        payable_date=d.pay_date or None,
                        cash_amount_per_share=float(d.cash_amount) if d.cash_amount else None,
                        source="polygon",
                        ingested_at=ingested_at,
                    )
                )

            for s in splits:
                action_id = s.id or _hash_id("polygon", ticker, s.execution_date, "split")
                split_from = float(s.split_from) if s.split_from else 1.0
                split_to = float(s.split_to) if s.split_to else 1.0
                ratio = split_to / split_from if split_from != 0 else None
                rows.append(
                    CorporateActions(
                        action_id=action_id,
                        ticker=ticker,
                        action_type="split",
                        ex_date=s.execution_date,
                        ratio=ratio,
                        source="polygon",
                        ingested_at=ingested_at,
                    )
                )

            _upsert_actions(_session_factory, rows)
            rows_written += len(rows)

        run.rows_written = rows_written


def _hash_id(source: str, ticker: str, date: str, action: str) -> str:
    """Fallback stable ID when the vendor doesn't provide one."""
    key = f"{source}:{ticker}:{date}:{action}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _upsert_actions(session_factory: Any, rows: list[CorporateActions]) -> None:
    """UPSERT corporate action rows keyed on ``action_id``."""
    if not rows:
        return
    with session_factory() as sess:
        for row in rows:
            sess.merge(row)
        sess.commit()


def bootstrap_corporate_actions(
    *,
    _client: Any = None,
    _session_factory: Any = None,
    _repo: Any = None,
) -> None:
    """
    Bootstrap corporate actions for 252 trading days.

    Reads active universe tickers (excluding benchmarks) from DB.
    """
    from alphamind.persistence.models import AssetUniverse
    from alphamind.persistence.session import make_engine, make_session_factory

    if _session_factory is None:
        engine = make_engine()
        _session_factory = make_session_factory(engine)

    with _session_factory() as sess:
        rows = (
            sess.query(AssetUniverse)
            .filter(AssetUniverse.is_active == 1, AssetUniverse.asset_role == "universe")
            .all()
        )
    tickers = [r.ticker for r in rows]
    since = datetime.now(UTC) - timedelta(days=_BOOTSTRAP_DAYS)

    collect_corporate_actions(
        ticker_scope=tickers,
        since=since,
        _client=_client,
        _session_factory=_session_factory,
        _repo=_repo,
    )
