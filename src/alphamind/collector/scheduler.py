"""
APScheduler-based collector runner.

Constructs a ``BlockingScheduler`` with one ``ThreadPoolExecutor(max_workers=1)``
per vendor, then registers one cron job per entry in ``collector_schedule.yaml``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import yaml  # type: ignore[import-untyped]
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

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
from alphamind.data_sources.kalshi.contracts import collect_snapshots as kalshi_collect_snapshots
from alphamind.data_sources.marketaux.news import collect_news as marketaux_news_collect_news
from alphamind.data_sources.polygon.corporate_actions import collect_corporate_actions
from alphamind.data_sources.polygon.equity import collect_universe_bars
from alphamind.data_sources.polygon.options import collect_options_chains
from alphamind.data_sources.polygon.reference import collect_reference
from alphamind.data_sources.polymarket.contracts import (
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
}

# Vendors — each gets its own single-worker executor
_VENDORS = [
    "polygon",
    "fred",
    "eia",
    "bls",
    "treasury",
    "finnhub",
    "marketaux",
    "sec_edgar",
    "polymarket",
    "kalshi",
    "finra",
    "iborrowdesk",
]

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
    """Construct a ``BlockingScheduler`` with one executor per vendor."""
    executors = {v: ThreadPoolExecutor(max_workers=1) for v in _VENDORS}
    return BlockingScheduler(executors=executors)


def _make_job(cid: str, callable_fn: Callable[..., object]) -> Callable[[], None]:
    def _job() -> None:
        log.info("collector=%s start", cid)
        try:
            callable_fn()
            log.info("collector=%s done", cid)
        except Exception:
            log.exception("collector=%s error", cid)

    return _job


def register_jobs(scheduler: BlockingScheduler) -> None:
    """Register one cron job per entry in ``collector_schedule.yaml``."""
    with _SCHEDULE_PATH.open() as fh:
        config: dict = yaml.safe_load(fh)

    timezone: str = config.get("timezone", "US/Eastern")
    collectors_cfg: dict[str, dict] = config.get("collectors", {})

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
        )


def start_blocking() -> None:
    """Build scheduler, register all jobs, and start (blocks until interrupted)."""
    _configure_logging()
    sched = build_scheduler()
    register_jobs(sched)
    log.info("collector scheduler starting")
    sched.start()
