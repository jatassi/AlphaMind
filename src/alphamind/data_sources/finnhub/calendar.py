"""
Finnhub calendar collectors — Qual5.

Collects earnings, economic, IPO, and FDA advisory calendar events.
Writes ``event_calendar`` and (for earnings) ``earnings_event_details``.

Economic event-type mapping
---------------------------
Finnhub's ``event`` field is free-text.  The mapping below covers the most
common US macro releases.  Unknown events fall back to ``"other"``.
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from datetime import date as date_type
from typing import Any

import finnhub

from alphamind.data_sources._common import RetryShape, track_run, with_retries
from alphamind.persistence.models import (
    Base,
    EarningsEventDetails,
    EventCalendar,
)
from alphamind.persistence.session import make_engine, make_session_factory

_PROVIDER = "finnhub"
_BOOTSTRAP_DAYS = 90

# Mapping from Finnhub's free-text event names to storage event_type values.
_ECONOMIC_EVENT_MAP: dict[str, str] = {
    "CPI": "cpi_release",
    "Core CPI": "cpi_release",
    "PPI": "ppi_release",
    "Core PPI": "ppi_release",
    "Nonfarm Payrolls": "nfp_release",
    "Non-Farm Payrolls": "nfp_release",
    "FOMC Statement": "fomc",
    "FOMC Minutes": "fomc",
    "Fed Interest Rate Decision": "fomc",
    "GDP": "gdp_release",
    "GDP Growth Rate": "gdp_release",
    "ISM Manufacturing PMI": "ism_release",
    "ISM Non-Manufacturing PMI": "ism_release",
    "ISM Services PMI": "ism_release",
}


def _get_api_key() -> str:
    return os.environ.get("FINNHUB_API_KEY", "")


def _event_id(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()


def _upsert_event(
    sess: Any,
    event_id: str,
    event_type: str,
    scheduled_at: str,
    ticker: str | None,
    description: str | None,
) -> bool:
    """Insert event_calendar row; return True if inserted, False if already existed."""
    existing = sess.get(EventCalendar, event_id)
    if existing is not None:
        return False
    now = datetime.now(UTC).isoformat()
    sess.add(
        EventCalendar(
            event_id=event_id,
            event_type=event_type,
            ticker=ticker,
            scheduled_at=scheduled_at,
            description=description,
            status="scheduled",
            source=_PROVIDER,
            ingested_at=now,
            last_updated=now,
        )
    )
    return True


# ---------------------------------------------------------------------------
# Earnings calendar
# ---------------------------------------------------------------------------


@with_retries(RetryShape.important, _sleep=lambda _: None)
def _fetch_earnings_calendar(sdk: finnhub.Client, from_date: str, to_date: str) -> list[dict]:
    result = sdk.earnings_calendar(_from=from_date, to=to_date, symbol="") or {}
    return result.get("earningsCalendar") or []


def _ingest_earnings(
    sess: Any,
    items: list[dict],
) -> int:
    rows_written = 0
    for item in items:
        ticker = item.get("symbol") or ""
        date = item.get("date") or ""
        hour = item.get("hour") or ""
        quarter = item.get("quarter")
        year = item.get("year")

        if not date:
            continue

        scheduled_at = f"{date}T00:00:00+00:00"
        evt_id = _event_id(_PROVIDER, "earnings", ticker, date)

        inserted = _upsert_event(sess, evt_id, "earnings", scheduled_at, ticker or None, None)
        if not inserted:
            continue

        # Flush so the event_calendar FK is satisfied for earnings_event_details
        sess.flush()

        # Map fiscal quarter
        fiscal_period = f"Q{quarter}" if quarter else "Q?"

        # earnings_event_details
        sess.add(
            EarningsEventDetails(
                event_id=evt_id,
                ticker=ticker,
                fiscal_period=fiscal_period,
                fiscal_year=year or 0,
                expected_call_time=hour or None,
                eps_consensus=item.get("epsEstimate"),
                eps_actual=item.get("epsActual"),
                revenue_consensus_usd=item.get("revenueEstimate"),
                revenue_actual_usd=item.get("revenueActual"),
                source=_PROVIDER,
            )
        )
        rows_written += 1
    return rows_written


def collect_earnings_calendar(
    since: datetime | None = None,
    *,
    _engine: Any = None,
    _session_factory: Any = None,
    _repo: Any = None,
) -> None:
    """Pull earnings calendar from ``since`` to today and persist it."""
    engine = _engine or make_engine()
    sf = _session_factory or make_session_factory(engine)
    Base.metadata.create_all(engine)

    sdk = finnhub.Client(api_key=_get_api_key())
    now = datetime.now(UTC)
    from_date = (since or now).strftime("%Y-%m-%d")
    to_date = now.strftime("%Y-%m-%d")

    with track_run("finnhub.earnings_calendar", _repo=_repo) as run:
        items = _fetch_earnings_calendar(sdk, from_date, to_date)
        with sf() as sess:
            run.rows_written = _ingest_earnings(sess, items)
            sess.commit()


def bootstrap_earnings_calendar(
    *,
    _engine: Any = None,
    _session_factory: Any = None,
    _repo: Any = None,
) -> None:
    """Pull earnings calendar forward 90 days from today."""
    engine = _engine or make_engine()
    sf = _session_factory or make_session_factory(engine)
    Base.metadata.create_all(engine)

    sdk = finnhub.Client(api_key=_get_api_key())
    now = datetime.now(UTC)
    from_date = now.strftime("%Y-%m-%d")
    to_date = (now + timedelta(days=_BOOTSTRAP_DAYS)).strftime("%Y-%m-%d")

    with track_run("finnhub.bootstrap_earnings_calendar", _repo=_repo) as run:
        items = _fetch_earnings_calendar(sdk, from_date, to_date)
        with sf() as sess:
            run.rows_written = _ingest_earnings(sess, items)
            sess.commit()


# ---------------------------------------------------------------------------
# Economic calendar
# ---------------------------------------------------------------------------


@with_retries(RetryShape.important, _sleep=lambda _: None)
def _fetch_economic_calendar(sdk: finnhub.Client, from_date: str, to_date: str) -> list[dict]:
    result = sdk.calendar_economic(_from=from_date, to=to_date) or {}
    return result.get("economicCalendar") or []


def _ingest_economic(sess: Any, items: list[dict]) -> int:
    rows_written = 0
    for item in items:
        event_name = item.get("event") or ""
        time_str = item.get("time") or ""

        if not time_str:
            continue

        # Finnhub returns ISO-8601 datetime strings like "2026-05-01T08:30:00"
        try:
            dt = datetime.fromisoformat(time_str)
            scheduled_at = dt.replace(tzinfo=UTC).isoformat()
        except ValueError:
            scheduled_at = f"{time_str}+00:00"

        evt_type = _ECONOMIC_EVENT_MAP.get(event_name, "other")
        evt_id = _event_id(_PROVIDER, "economic", event_name, time_str)

        inserted = _upsert_event(sess, evt_id, evt_type, scheduled_at, None, event_name or None)
        if inserted:
            rows_written += 1
    return rows_written


def collect_economic_calendar(
    since: datetime | None = None,
    *,
    _engine: Any = None,
    _session_factory: Any = None,
    _repo: Any = None,
) -> None:
    """Pull economic calendar from ``since`` to today and persist it."""
    engine = _engine or make_engine()
    sf = _session_factory or make_session_factory(engine)
    Base.metadata.create_all(engine)

    sdk = finnhub.Client(api_key=_get_api_key())
    now = datetime.now(UTC)
    from_date = (since or now).strftime("%Y-%m-%d")
    to_date = now.strftime("%Y-%m-%d")

    with track_run("finnhub.economic_calendar", _repo=_repo) as run:
        items = _fetch_economic_calendar(sdk, from_date, to_date)
        with sf() as sess:
            run.rows_written = _ingest_economic(sess, items)
            sess.commit()


def bootstrap_economic_calendar(
    *,
    _engine: Any = None,
    _session_factory: Any = None,
    _repo: Any = None,
) -> None:
    """Pull economic calendar forward 90 days from today."""
    engine = _engine or make_engine()
    sf = _session_factory or make_session_factory(engine)
    Base.metadata.create_all(engine)

    sdk = finnhub.Client(api_key=_get_api_key())
    now = datetime.now(UTC)
    from_date = now.strftime("%Y-%m-%d")
    to_date = (now + timedelta(days=_BOOTSTRAP_DAYS)).strftime("%Y-%m-%d")

    with track_run("finnhub.bootstrap_economic_calendar", _repo=_repo) as run:
        items = _fetch_economic_calendar(sdk, from_date, to_date)
        with sf() as sess:
            run.rows_written = _ingest_economic(sess, items)
            sess.commit()


# ---------------------------------------------------------------------------
# IPO calendar
# ---------------------------------------------------------------------------


@with_retries(RetryShape.important, _sleep=lambda _: None)
def _fetch_ipo_calendar(sdk: finnhub.Client, from_date: str, to_date: str) -> list[dict]:
    result = sdk.ipo_calendar(_from=from_date, to=to_date) or {}
    return result.get("ipoCalendar") or []


def collect_ipo_calendar(
    since: datetime | None = None,
    *,
    _engine: Any = None,
    _session_factory: Any = None,
    _repo: Any = None,
) -> None:
    """Pull IPO calendar from ``since`` to today and persist it."""
    engine = _engine or make_engine()
    sf = _session_factory or make_session_factory(engine)
    Base.metadata.create_all(engine)

    sdk = finnhub.Client(api_key=_get_api_key())
    now = datetime.now(UTC)
    from_date = (since or now).strftime("%Y-%m-%d")
    to_date = (now + timedelta(days=_BOOTSTRAP_DAYS)).strftime("%Y-%m-%d")

    with track_run("finnhub.ipo_calendar", _repo=_repo) as run:
        items = _fetch_ipo_calendar(sdk, from_date, to_date)
        rows_written = 0
        with sf() as sess:
            for item in items:
                name = item.get("name") or item.get("symbol") or ""
                date = item.get("date") or ""
                if not date:
                    continue
                scheduled_at = f"{date}T00:00:00+00:00"
                evt_id = _event_id(_PROVIDER, "ipo", name, date)
                # IPO tickers are not in asset_universe yet — ticker must be None
                inserted = _upsert_event(sess, evt_id, "other", scheduled_at, None, name or None)
                if inserted:
                    rows_written += 1
            sess.commit()
        run.rows_written = rows_written


# ---------------------------------------------------------------------------
# FDA advisory calendar
# ---------------------------------------------------------------------------


@with_retries(RetryShape.important, _sleep=lambda _: None)
def _fetch_fda_calendar(sdk: finnhub.Client) -> list[dict]:
    result = sdk.fda_calendar() or {}
    return result.get("fdaCalendar") or []


def collect_fda_calendar(
    since: datetime | None = None,
    *,
    _engine: Any = None,
    _session_factory: Any = None,
    _repo: Any = None,
) -> None:
    """Pull FDA advisory committee calendar and persist it."""
    engine = _engine or make_engine()
    sf = _session_factory or make_session_factory(engine)
    Base.metadata.create_all(engine)

    sdk = finnhub.Client(api_key=_get_api_key())

    with track_run("finnhub.fda_calendar", _repo=_repo) as run:
        items = _fetch_fda_calendar(sdk)
        rows_written = 0

        cutoff = (since or datetime.now(UTC)).date()

        with sf() as sess:
            for item in items:
                date = item.get("date") or ""
                drug_name = item.get("drug_name") or item.get("drugName") or ""
                if not date:
                    continue

                try:
                    if date_type.fromisoformat(date) < cutoff:
                        continue
                except ValueError:
                    pass

                scheduled_at = f"{date}T00:00:00+00:00"
                evt_id = _event_id(_PROVIDER, "fda_advisory", drug_name, date)
                inserted = _upsert_event(
                    sess, evt_id, "fda_advisory", scheduled_at, None, drug_name or None
                )
                if inserted:
                    rows_written += 1
            sess.commit()
        run.rows_written = rows_written
