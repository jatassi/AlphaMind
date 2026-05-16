"""
APScheduler-based collector runner.

Constructs a ``BlockingScheduler`` with one ``ThreadPoolExecutor(max_workers=1)``
per vendor, then registers one cron job per entry in ``collector_schedule.yaml``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

import yaml
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from alphamind.analysis.news_clustering.clustering import refresh_news_clusters
from alphamind.data_sources._common import default_session_factory
from alphamind.data_sources.bls.macro import collect_series as bls_macro_collect_series
from alphamind.data_sources.eia.energy import collect_series as eia_energy_collect_series
from alphamind.data_sources.finnhub.calendar import (
    collect_earnings_calendar,
    collect_economic_calendar,
    collect_fda_calendar,
    collect_ipo_calendar,
)
from alphamind.data_sources.finnhub.estimate_revisions import (
    collect_estimate_revisions as finnhub_estimate_revisions,
)
from alphamind.data_sources.finnhub.news import collect_news as finnhub_news_collect_news
from alphamind.data_sources.finra.short_interest import (
    collect_short_interest as finra_short_interest,
)
from alphamind.data_sources.finra.short_volume import collect_short_volume as finra_short_volume
from alphamind.data_sources.fred.macro import collect_series as fred_macro_collect_series
from alphamind.data_sources.iborrowdesk.borrow_cost import (
    collect_borrow_cost as iborrowdesk_collect_borrow_cost,
)
from alphamind.data_sources.marketaux.news import collect_news as marketaux_news_collect_news
from alphamind.data_sources.polygon.corporate_actions import collect_corporate_actions
from alphamind.data_sources.polygon.equity import collect_universe_bars
from alphamind.data_sources.polygon.options import collect_options_chains
from alphamind.data_sources.polygon.reference import collect_reference
from alphamind.data_sources.prediction_market.kalshi.contracts import (
    collect_snapshots as kalshi_collect_snapshots,
)
from alphamind.data_sources.prediction_market.polymarket.contracts import (
    collect_snapshots as polymarket_collect_snapshots,
)
from alphamind.data_sources.sec_edgar.rss import collect_8k_filings
from alphamind.data_sources.treasury.auctions import collect_auctions

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# finnhub.calendar fanout wrapper
# ---------------------------------------------------------------------------


def _finnhub_calendar_fanout(since: object = None) -> None:
    """Invoke all four Finnhub calendar collectors in sequence.

    Accepts ``since`` for runner-contract compatibility; the underlying
    sub-collectors each have their own resume-from-latest defaults so the
    value is not propagated.
    """
    del since  # accepted for runner-contract compatibility
    collect_earnings_calendar()
    collect_economic_calendar()
    collect_ipo_calendar()
    collect_fda_calendar()


def _news_clustering_refresh(since: object = None) -> None:
    """Run the headline-clustering pipeline against the configured database.

    Opens a session against the default database, calls
    :func:`alphamind.data_sources.news.clustering.refresh_news_clusters`
    with ``as_of`` pinned to the current UTC time, and closes the session.
    The pre-market and market-hours cron entries both route to this single
    callable — the cadence difference lives entirely in
    ``config/collector_schedule.yaml``.
    """
    from datetime import UTC, datetime

    from alphamind.data_sources._common import default_session_factory

    del since  # accepted for runner-contract compatibility
    factory = default_session_factory()
    with factory() as sess:
        refresh_news_clusters(sess, as_of=datetime.now(UTC))


# ---------------------------------------------------------------------------
# Collector registry — maps YAML collector ID → callable
# ---------------------------------------------------------------------------

COLLECTORS: dict[str, Callable[..., object]] = {
    "polygon.equity": collect_universe_bars,
    "polygon.equity_offhrs": collect_universe_bars,
    "polygon.options": collect_options_chains,
    "polygon.corporate_actions": collect_corporate_actions,
    "polygon.reference": collect_reference,
    "fred.macro": fred_macro_collect_series,
    "eia.energy": eia_energy_collect_series,
    "bls.macro": bls_macro_collect_series,
    "treasury.auctions": collect_auctions,
    "finnhub.news": finnhub_news_collect_news,
    "finnhub.calendar": _finnhub_calendar_fanout,
    "marketaux.news": marketaux_news_collect_news,
    "sec_edgar.rss": collect_8k_filings,
    "polymarket": polymarket_collect_snapshots,
    "kalshi": kalshi_collect_snapshots,
    "finra.short_volume": finra_short_volume,
    "finra.short_interest": finra_short_interest,
    "iborrowdesk.borrow_cost": iborrowdesk_collect_borrow_cost,
    "finnhub.estimate_revisions": finnhub_estimate_revisions,
    "news.clustering": _news_clustering_refresh,
    "news.clustering_premarket": _news_clustering_refresh,
}

# Per-vendor executor sizing. Default is 1 (serialise to respect rate limits).
# polygon needs 2: polygon.equity holds the executor 6-13 min per market-hours
# fire while polygon.options is supposed to fire every 30 min; with one worker
# polygon.options gets misfired every cycle. polygon's 100/min API budget
# easily absorbs two concurrent collectors.
_VENDOR_WORKERS: dict[str, int] = {
    "polygon": 2,
    "fred": 1,
    "eia": 1,
    "bls": 1,
    "treasury": 1,
    "finnhub": 1,
    "marketaux": 1,
    "sec_edgar": 1,
    "polymarket": 1,
    "kalshi": 1,
    "finra": 1,
    "iborrowdesk": 1,
    "news": 1,
}

_SCHEDULE_PATH = Path(__file__).parents[3] / "config" / "collector_schedule.yaml"


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------


def _configure_logging() -> None:
    """Configure TimedRotatingFileHandler with daily rotation, 30-day retention."""
    log_dir = Path(os.environ.get("USERPROFILE") or str(Path.home())) / "AlphaMind" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "collector.log"

    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger("alphamind")
    root.setLevel(level)

    if not any(isinstance(h, TimedRotatingFileHandler) for h in root.handlers):
        handler = TimedRotatingFileHandler(
            log_file,
            when="midnight",
            interval=1,
            backupCount=30,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        root.addHandler(handler)


# ---------------------------------------------------------------------------
# Scheduler construction
# ---------------------------------------------------------------------------


def build_scheduler() -> BlockingScheduler:
    """Construct a ``BlockingScheduler`` with a per-vendor executor."""
    executors = {v: ThreadPoolExecutor(max_workers=w) for v, w in _VENDOR_WORKERS.items()}
    return BlockingScheduler(executors=executors)


def _make_job(cid: str, callable_fn: Callable[..., object]) -> Callable[[], None]:
    def _job() -> None:
        log.info("collector=%s start", cid)
        try:
            callable_fn()
            log.info("collector=%s done", cid)
        except Exception:
            # Per-job supervisor per runtime §G1: one collector raising must
            # not unsubscribe the rest. ``BaseException`` (``KeyboardInterrupt``)
            # propagates so the scheduler can shut down cleanly.
            log.exception("collector=%s error", cid)

    return _job


def register_jobs(scheduler: BlockingScheduler) -> None:
    """Register one cron job per entry in ``collector_schedule.yaml``."""
    with _SCHEDULE_PATH.open(encoding="utf-8") as fh:
        config: dict[str, Any] = yaml.safe_load(fh)

    timezone: str = config.get("timezone", "US/Eastern")
    collectors_cfg: dict[str, dict[str, Any]] = config.get("collectors", {})

    for collector_id, entry in collectors_cfg.items():
        cron_expr: str = entry["cron"]
        fn = COLLECTORS[collector_id]
        vendor = collector_id.split(".")[0]
        scheduler.add_job(
            _make_job(collector_id, fn),
            trigger=CronTrigger.from_crontab(cron_expr, timezone=timezone),
            id=collector_id,
            executor=vendor,
            max_instances=1,
            # ``coalesce`` collapses any backlog of missed fires (e.g. after a
            # service restart) into a single run. ``misfire_grace_time`` drops
            # fires that are >60s late so a stalled previous run does not
            # trigger a catch-up storm once it finally completes. (ALP-289.)
            coalesce=True,
            misfire_grace_time=60,
        )


def mark_orphan_runs(session_factory: Any = None) -> int:
    """Mark any pre-existing ``status='running'`` rows as ``failed``.

    Called at scheduler startup so a hard-killed run (whose ``track_run``
    handler could not write the failure status before the process died) does
    not leave stale ``running`` rows in ``collection_runs``. Returns the count
    of rows updated. (ALP-289.)
    """
    from alphamind.persistence.models import CollectionRuns

    factory = session_factory or default_session_factory()
    now_iso = datetime.now(UTC).isoformat()
    with factory() as sess:
        rows = sess.query(CollectionRuns).filter(CollectionRuns.status == "running").all()
        for row in rows:
            row.status = "failed"
            row.error_summary = "orphaned by scheduler restart"
            row.completed_at = now_iso
        sess.commit()
        return len(rows)


def start_blocking() -> None:
    """Build scheduler, register all jobs, and start (blocks until interrupted)."""
    _configure_logging()
    n_orphans = mark_orphan_runs()
    if n_orphans:
        log.info("scheduler startup: marked %d orphan run(s) as failed", n_orphans)
    sched = build_scheduler()
    register_jobs(sched)
    log.info("collector scheduler starting")
    sched.start()
