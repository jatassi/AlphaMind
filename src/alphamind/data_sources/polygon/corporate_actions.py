"""
Polygon corporate actions collection (dividends, splits, delistings).

Exports
-------
collect_corporate_actions(ticker_scope, since, ...)
bootstrap_corporate_actions(...)
reconcile_delisted_tickers(session_factory)
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from polygon.exceptions import BadResponse

from alphamind.data_sources._common import (
    RetryShape,
    active_universe_tickers,
    default_session_factory,
    resume_since,
    track_run,
    with_retries,
)
from alphamind.data_sources.polygon.client import PolygonClient
from alphamind.persistence.models import AssetUniverse, CorporateActions

logger = logging.getLogger(__name__)

_BOOTSTRAP_DAYS = 252

# ``action_type`` values that mean the ticker no longer trades. A row of any
# of these types deactivates its ``asset_universe`` ticker. Polygon's feed
# only auto-populates ``delisting``; ``merger`` / ``acquisition`` rows (which
# carry the acquirer) are inserted by operator backfills.
_DEACTIVATING_ACTION_TYPES: frozenset[str] = frozenset({"merger", "acquisition", "delisting"})


def collect_corporate_actions(
    ticker_scope: list[str] | None = None,
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
        Defaults to active equity-universe tickers from ``asset_universe``.
    since:
        Lower bound for ``ex_date`` / ``execution_date`` filter.  Defaults to
        ``resume_since`` over ``corporate_actions`` with a 7-day lookback.
    """
    if _client is None:
        _client = PolygonClient()
    _session_factory = _session_factory or default_session_factory()
    if ticker_scope is None:
        ticker_scope = active_universe_tickers(
            include_benchmarks=False, session_factory=_session_factory
        )
    if since is None:
        since = resume_since(
            column=CorporateActions.ex_date,
            filters=(CorporateActions.source == "polygon",),
            default_lookback=timedelta(days=7),
            overlap=timedelta(days=1),
            session_factory=_session_factory,
        )

    since_str = since.strftime("%Y-%m-%d")
    ingested_at = datetime.now(UTC).isoformat()

    @with_retries(RetryShape.important, _sleep=lambda _: None)
    def _fetch_dividends(ticker: str) -> list[Any]:
        return list(_client.list_dividends(ticker, ex_dividend_date_gte=since_str))

    @with_retries(RetryShape.important, _sleep=lambda _: None)
    def _fetch_splits(ticker: str) -> list[Any]:
        return list(_client.list_splits(ticker, execution_date_gte=since_str))

    @with_retries(RetryShape.important, _sleep=lambda _: None)
    def _fetch_details(ticker: str) -> Any:
        return _client.get_ticker_details(ticker)

    with track_run("polygon.corporate_actions", _repo=_repo) as run:
        rows_written = 0
        single = len(ticker_scope) == 1

        for ticker in ticker_scope:
            try:
                dividends = _fetch_dividends(ticker)
                splits = _fetch_splits(ticker)
                details = _fetch_details(ticker)
            except (BadResponse, httpx.HTTPError) as exc:
                if single:
                    raise
                # A delisted ticker is exactly where get_ticker_details is
                # apt to fail — skip it rather than abort the whole run.
                logger.warning(
                    "polygon corporate-actions fetch failed for %s",
                    ticker,
                    exc_info=exc,
                )
                continue

            rows = _build_rows(ticker, dividends, splits, details, ingested_at)
            _upsert_actions(_session_factory, rows)
            rows_written += len(rows)

        run.rows_written = rows_written
        # Deactivate tickers newly flagged delisted so the next cycle no
        # longer queries phantom names.
        reconcile_delisted_tickers(_session_factory)


def _build_rows(
    ticker: str,
    dividends: list[Any],
    splits: list[Any],
    details: Any,
    ingested_at: str,
) -> list[CorporateActions]:
    """Build ORM rows from raw Polygon dividend, split, and ticker-detail data."""
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
    delisting = _delisting_row(ticker, details, ingested_at)
    if delisting is not None:
        rows.append(delisting)
    return rows


def _delisting_row(ticker: str, details: Any, ingested_at: str) -> CorporateActions | None:
    """Build a ``delisting`` row when Polygon ticker-details reports the ticker
    no longer trades, else ``None``.

    Polygon's reference feed exposes the delisting (``active`` flag and
    ``delisted_utc`` date) but not the acquirer, so ``acquirer_ticker`` is left
    NULL on auto-detected rows. The reconciler deactivates the ``asset_universe``
    row regardless of whether an acquirer is known — a NULL acquirer simply
    yields a ``removal_reason`` of ``"delisted"``.
    """
    if details.active or not details.delisted_utc:
        return None
    ex_date = str(details.delisted_utc)[:10]
    return CorporateActions(
        action_id=_hash_id("polygon", ticker, ex_date, "delisting"),
        ticker=ticker,
        action_type="delisting",
        ex_date=ex_date,
        description="ticker delisted per Polygon reference data",
        source="polygon",
        ingested_at=ingested_at,
    )


def _hash_id(source: str, ticker: str, date: str, action: str) -> str:
    """Fallback stable ID when the vendor doesn't provide one."""
    key = f"{source}:{ticker}:{date}:{action}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _removal_reason(action: CorporateActions) -> str:
    """Human-readable ``asset_universe.removal_reason`` for a deactivating action."""
    if action.acquirer_ticker:
        return f"acquired by {action.acquirer_ticker}"
    if action.action_type == "delisting":
        return "delisted"
    return f"{action.action_type} completed"


def reconcile_delisted_tickers(session_factory: Any = None) -> int:
    """Deactivate ``asset_universe`` rows for tickers that have stopped trading.

    For every ``corporate_actions`` row of a deactivating type
    (:data:`_DEACTIVATING_ACTION_TYPES`) whose ticker is still ``is_active=1``,
    flips ``is_active`` to ``0``, stamps ``removed_date`` with the action's
    ``ex_date``, and writes a ``removal_reason``.

    Idempotent: a ticker already inactive is skipped, so the call is safe to run
    every collection cycle. When a ticker carries multiple deactivating rows
    they are applied in ``ex_date`` order (earliest first, an acquirer-bearing
    row breaking an exact-date tie); the first wins and the rest fall through.
    Returns the count of rows newly deactivated.
    """
    session_factory = session_factory or default_session_factory()
    now_iso = datetime.now(UTC).isoformat()
    deactivated = 0
    with session_factory() as sess:
        actions = (
            sess.query(CorporateActions)
            .filter(CorporateActions.action_type.in_(_DEACTIVATING_ACTION_TYPES))
            .order_by(CorporateActions.ex_date, CorporateActions.acquirer_ticker.is_(None))
            .all()
        )
        if not actions:
            return 0
        # One query for every still-active target ticker; pop-on-use means a
        # ticker carrying multiple deactivating rows is flipped once — by the
        # first in sort order — and later rows fall through.
        active_rows = {
            row.ticker: row
            for row in sess.query(AssetUniverse)
            .filter(
                AssetUniverse.ticker.in_({a.ticker for a in actions}),
                AssetUniverse.is_active == 1,
            )
            .all()
        }
        for action in actions:
            row = active_rows.pop(action.ticker, None)
            if row is None:
                continue
            row.is_active = 0
            row.removed_date = action.ex_date
            row.removal_reason = _removal_reason(action)
            row.last_updated = now_iso
            logger.info("deactivated %s in asset_universe: %s", action.ticker, row.removal_reason)
            deactivated += 1
        sess.commit()
    return deactivated


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
