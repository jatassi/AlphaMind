#!/usr/bin/env python3
"""Validate the asset universe — and discover candidates that would qualify.

Implements the procedure in docs/design/asset-universe-validation.md.

Two modes:
- Validate (default): runs the five criteria against every ticker in
  `config/assets.yaml`, surfaces fails for operator action.
- Discover (--discover): fetches the holdings of each sector's discovery ETF,
  filters out names already in the universe, runs the criteria against the
  remainder, surfaces passing names as add candidates.

Neither mode edits any files; the operator decides which failures or
candidates warrant a YAML edit.

Usage:
    uv run python scripts/validate_universe.py
    uv run python scripts/validate_universe.py --ticker AAPL
    uv run python scripts/validate_universe.py --discover

Environment:
    POLYGON_API_KEY    required
    FINNHUB_API_KEY    required
"""

from __future__ import annotations

import argparse
import io
import os
import re
import statistics
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
import yaml
from dotenv import load_dotenv

# --- Thresholds (mirror docs/design/asset-universe-validation.md) -----------

ADV_LOOKBACK_TRADING_DAYS = 60
ADV_MIN_SHARES = 2_000_000
ADV_MIN_NOTIONAL_USD = 50_000_000

ANALYST_COVERAGE_MIN = 10

BETA_LOOKBACK_TRADING_DAYS = 90
BETA_MIN = 0.60  # absolute-value floor — see asset-universe-validation.md § Beta
BETA_BENCHMARK = "SPY"

MARKET_CAP_MIN_USD = 10_000_000_000

OPTIONS_FRONT_MONTH_MAX_DAYS = 45
OPTIONS_NEAR_MONEY_PCT = 0.10
OPTIONS_OI_MIN = 5_000


# --- API config -------------------------------------------------------------

POLYGON_BASE = "https://api.polygon.io"
FINNHUB_BASE = "https://finnhub.io/api/v1"

FINNHUB_INTERVAL_S = 1.1  # ~55 req/min — under the 60/min free-tier cap

# ETF holdings sources used by --discover.
SPDR_HOLDINGS_URL = (
    "https://www.ssga.com/library-content/products/fund-data/etfs/us"
    "/holdings-daily-us-en-{etf_lower}.xlsx"
)
ISHARES_HOLDINGS_URL = (
    "https://www.ishares.com/us/products/{product_id}/x"
    "/1467271812596.ajax?fileType=csv&fileName={etf}_holdings&dataType=fund"
)
ETF_USER_AGENT = "Mozilla/5.0"  # both vendors reject empty/curl UAs


# --- Result types -----------------------------------------------------------


@dataclass
class CriterionResult:
    name: str
    passed: bool
    detail: str
    threshold: str = ""
    error: str | None = None

    @property
    def status(self) -> str:
        if self.error is not None:
            return "unknown"
        return "pass" if self.passed else "fail"


@dataclass
class TickerReport:
    ticker: str
    sector: str
    results: list[CriterionResult] = field(default_factory=list)

    @property
    def overall(self) -> str:
        labels = {r.name for r in self.results if r.status == "fail"}
        if labels:
            return f"fail: {', '.join(sorted(labels))}"
        unknowns = {r.name for r in self.results if r.status == "unknown"}
        if unknowns:
            return f"unknown: {', '.join(sorted(unknowns))}"
        return "pass"


# --- HTTP clients -----------------------------------------------------------


class PolygonClient:
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._client = httpx.Client(timeout=30.0)

    def _get(self, url_or_path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        full = url_or_path if url_or_path.startswith("http") else f"{POLYGON_BASE}{url_or_path}"
        final = dict(params or {})
        final["apiKey"] = self._api_key
        resp = self._client.get(full, params=final)
        resp.raise_for_status()
        return resp.json()

    def _paginated(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        url: str | None = path
        next_params = params
        while url:
            data = self._get(url, next_params)
            out.extend(data.get("results", []))
            url = data.get("next_url")
            next_params = None  # next_url already carries query params
        return out

    def daily_bars(self, ticker: str, start: date, end: date) -> list[dict[str, Any]]:
        path = f"/v2/aggs/ticker/{ticker}/range/1/day/{start.isoformat()}/{end.isoformat()}"
        return self._paginated(path, {"adjusted": "true", "sort": "asc", "limit": 50000})

    def ticker_reference(self, ticker: str) -> dict[str, Any]:
        return self._get(f"/v3/reference/tickers/{ticker}").get("results", {})

    def options_chain_at_expiry(
        self,
        ticker: str,
        expiry: date,
        strike_min: float,
        strike_max: float,
    ) -> list[dict[str, Any]]:
        """Snapshot the options chain at a single expiry, narrowed by strike.

        Polygon's `/v3/snapshot/options/{ticker}` ignores `limit` on continuation
        pages (returns 10/page) regardless of URL filters, so a 45-day expiry
        range still paginates into hundreds of round-trips on liquid names.
        Querying a single expiry holds the result to one or two pages.
        """
        return self._paginated(
            f"/v3/snapshot/options/{ticker}",
            {
                "limit": 250,
                "expiration_date": expiry.isoformat(),
                "strike_price.gte": strike_min,
                "strike_price.lte": strike_max,
            },
        )


class FinnhubClient:
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._client = httpx.Client(timeout=30.0)
        self._last_at: float = 0.0

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        elapsed = time.monotonic() - self._last_at
        if elapsed < FINNHUB_INTERVAL_S:
            time.sleep(FINNHUB_INTERVAL_S - elapsed)
        final = dict(params or {})
        final["token"] = self._api_key
        resp = self._client.get(f"{FINNHUB_BASE}{path}", params=final)
        self._last_at = time.monotonic()
        resp.raise_for_status()
        return resp.json()

    def recommendations(self, symbol: str) -> list[dict[str, Any]]:
        return self._get("/stock/recommendation", {"symbol": symbol})


# --- ETF holdings fetchers (discovery mode) ---------------------------------


# US equity ticker shape: 1-5 uppercase letters with an optional .X share-class suffix.
# Used to reject ETF holdings rows that aren't equity holdings (cash/futures/disclaimers).
TICKER_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z]{1,2})?$")


def _fetch_spdr_holdings(etf: str) -> list[str]:
    """Fetch tickers held by a State Street SPDR sector-select ETF.

    The XLSX has a 4-row preamble (Fund Name, Ticker Symbol, Holdings as-of,
    blank) followed by a header row with `Name, Ticker, Identifier, ...`.
    """
    url = SPDR_HOLDINGS_URL.format(etf_lower=etf.lower())
    resp = httpx.get(url, timeout=30.0, follow_redirects=True,
                     headers={"User-Agent": ETF_USER_AGENT})
    resp.raise_for_status()
    df = pd.read_excel(io.BytesIO(resp.content), skiprows=4)
    return _filter_equity_tickers(df["Ticker"])


def _fetch_ishares_holdings(etf: str, product_id: str) -> list[str]:
    """Fetch tickers held by an iShares ETF via the holdings CSV download.

    The CSV has a 9-row preamble (fund name, as-of date, inception, allocation
    rows, blank) followed by a header row with `Ticker, Name, ..., Asset Class, ...`.
    The CSV trails into a multi-line legal disclaimer; filter by Asset Class
    to reject those rows along with cash placeholders and futures entries.
    """
    url = ISHARES_HOLDINGS_URL.format(product_id=product_id, etf=etf)
    resp = httpx.get(url, timeout=30.0, follow_redirects=True,
                     headers={"User-Agent": ETF_USER_AGENT})
    resp.raise_for_status()
    df = pd.read_csv(io.StringIO(resp.text), skiprows=9)
    if "Asset Class" in df.columns:
        df = df[df["Asset Class"].astype(str).str.strip() == "Equity"]
    return _filter_equity_tickers(df["Ticker"])


def _filter_equity_tickers(col: pd.Series) -> list[str]:
    """Keep only entries that look like US equity ticker symbols."""
    out: list[str] = []
    for raw in col.dropna():
        sym = str(raw).strip().upper()
        if TICKER_RE.match(sym):
            out.append(sym)
    return out


def fetch_etf_holdings(spec: dict[str, Any]) -> list[str]:
    vendor = spec.get("vendor")
    etf = spec.get("etf")
    if not etf:
        raise ValueError(f"discovery source missing `etf`: {spec}")
    if vendor == "spdr":
        return _fetch_spdr_holdings(etf)
    if vendor == "ishares":
        pid = spec.get("ishares_product_id")
        if not pid:
            raise ValueError(f"iShares discovery source missing `ishares_product_id`: {spec}")
        return _fetch_ishares_holdings(etf, str(pid))
    raise ValueError(f"unknown discovery vendor: {vendor!r}")


# --- Formatting helpers -----------------------------------------------------


def _fmt_int(n: float) -> str:
    return f"{n:,.0f}"


def _fmt_usd(n: float) -> str:
    return f"${n:,.0f}"


# --- Per-criterion checks ---------------------------------------------------


def check_adv(polygon: PolygonClient, ticker: str, as_of: date) -> CriterionResult:
    start = as_of - timedelta(days=120)
    bars = polygon.daily_bars(ticker, start, as_of)
    threshold = f">{_fmt_int(ADV_MIN_SHARES)} shares OR >{_fmt_usd(ADV_MIN_NOTIONAL_USD)} notional"
    if len(bars) < ADV_LOOKBACK_TRADING_DAYS:
        return CriterionResult(
            "ADV",
            False,
            f"only {len(bars)} trading days available",
            threshold,
            error=f"need {ADV_LOOKBACK_TRADING_DAYS} bars",
        )
    window = bars[-ADV_LOOKBACK_TRADING_DAYS:]
    mean_shares = statistics.mean(b["v"] for b in window)
    mean_notional = statistics.mean(b["v"] * b["c"] for b in window)
    passed = mean_shares > ADV_MIN_SHARES or mean_notional > ADV_MIN_NOTIONAL_USD
    detail = f"{_fmt_int(mean_shares)} shares  /  {_fmt_usd(mean_notional)} notional"
    return CriterionResult("ADV", passed, detail, threshold)


def check_analyst_coverage(finnhub: FinnhubClient, ticker: str) -> CriterionResult:
    recs = finnhub.recommendations(ticker)
    threshold = f">={ANALYST_COVERAGE_MIN}"
    if not recs:
        return CriterionResult(
            "Analyst coverage", False, "no recommendations returned",
            threshold, error="empty response",
        )
    latest = recs[0]  # Finnhub returns most-recent first
    total = sum(latest.get(k, 0) for k in ("strongBuy", "buy", "hold", "sell", "strongSell"))
    passed = total >= ANALYST_COVERAGE_MIN
    return CriterionResult("Analyst coverage", passed, f"{total} analysts", threshold)


def _close_to_close_returns(bars: list[dict[str, Any]]) -> list[float]:
    closes = [b["c"] for b in bars]
    return [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes))]


def _covariance(x: list[float], y: list[float]) -> float:
    mx = statistics.mean(x)
    my = statistics.mean(y)
    return sum((xi - mx) * (yi - my) for xi, yi in zip(x, y, strict=True)) / (len(x) - 1)


def check_beta(polygon: PolygonClient, ticker: str, as_of: date) -> CriterionResult:
    threshold = f"|β|>={BETA_MIN:.2f}"
    start = as_of - timedelta(days=160)
    ticker_bars = polygon.daily_bars(ticker, start, as_of)
    spy_bars = polygon.daily_bars(BETA_BENCHMARK, start, as_of)
    if not ticker_bars or not spy_bars:
        return CriterionResult("Beta", False, "missing daily bars", threshold, error="empty bars")

    spy_by_t = {b["t"]: b for b in spy_bars}
    aligned_t: list[dict[str, Any]] = []
    aligned_s: list[dict[str, Any]] = []
    for b in ticker_bars:
        match = spy_by_t.get(b["t"])
        if match:
            aligned_t.append(b)
            aligned_s.append(match)

    needed = BETA_LOOKBACK_TRADING_DAYS + 1  # one extra for the first return
    aligned_t = aligned_t[-needed:]
    aligned_s = aligned_s[-needed:]
    if len(aligned_t) < needed:
        return CriterionResult(
            "Beta",
            False,
            f"only {len(aligned_t) - 1} aligned trading days",
            threshold,
            error=f"need {BETA_LOOKBACK_TRADING_DAYS} returns",
        )

    r_t = _close_to_close_returns(aligned_t)
    r_s = _close_to_close_returns(aligned_s)
    var_s = statistics.variance(r_s)
    if var_s == 0:
        return CriterionResult(
            "Beta", False, "SPY zero variance", threshold, error="degenerate variance",
        )
    beta = _covariance(r_t, r_s) / var_s
    return CriterionResult("Beta", abs(beta) >= BETA_MIN, f"{beta:.2f}", threshold)


def check_market_cap(polygon: PolygonClient, ticker: str) -> CriterionResult:
    threshold = f">={_fmt_usd(MARKET_CAP_MIN_USD)}"
    ref = polygon.ticker_reference(ticker)
    market_cap = ref.get("market_cap")
    if not market_cap:
        return CriterionResult(
            "Market cap", False, "market_cap missing", threshold, error="no field",
        )
    return CriterionResult(
        "Market cap", market_cap >= MARKET_CAP_MIN_USD, _fmt_usd(market_cap), threshold,
    )


def _next_third_friday_within(as_of: date, max_days: int) -> date | None:
    """Compute the nearest 3rd-Friday standard monthly expiry on/after `as_of`,
    inside `as_of + max_days`. Returns None if no monthly expiry fits the window.
    """
    cutoff = as_of + timedelta(days=max_days)
    # The 3rd Friday of any month falls on day 15-21. Check the current month
    # first; if it has already passed (or is past the cutoff), try next month.
    for month_offset in range(3):
        year = as_of.year
        month = as_of.month + month_offset
        while month > 12:
            year += 1
            month -= 12
        first = date(year, month, 1)
        # weekday(): Mon=0, Fri=4 → days until first Friday
        first_friday_offset = (4 - first.weekday()) % 7
        third_friday = first + timedelta(days=first_friday_offset + 14)
        if third_friday >= as_of and third_friday <= cutoff:
            return third_friday
    return None


def check_options_oi(polygon: PolygonClient, ticker: str, as_of: date) -> CriterionResult:
    threshold = f">={_fmt_int(OPTIONS_OI_MIN)} contracts NTM"

    target_expiry = _next_third_friday_within(as_of, OPTIONS_FRONT_MONTH_MAX_DAYS)
    if target_expiry is None:
        return CriterionResult(
            "Options OI (NTM)",
            False,
            f"no standard monthly expiry within {OPTIONS_FRONT_MONTH_MAX_DAYS} days",
            threshold,
            error="no qualifying expiry",
        )

    recent_bars = polygon.daily_bars(ticker, as_of - timedelta(days=10), as_of)
    if not recent_bars:
        return CriterionResult(
            "Options OI (NTM)", False, "no recent price for spot", threshold, error="no spot",
        )
    spot = recent_bars[-1]["c"]
    lo = spot * (1 - OPTIONS_NEAR_MONEY_PCT)
    hi = spot * (1 + OPTIONS_NEAR_MONEY_PCT)

    contracts = polygon.options_chain_at_expiry(ticker, target_expiry, lo, hi)
    if not contracts:
        return CriterionResult(
            "Options OI (NTM)", False,
            f"no contracts at {target_expiry.isoformat()} within ±{OPTIONS_NEAR_MONEY_PCT:.0%}",
            threshold, error="empty chain",
        )

    total_oi = sum(int(c.get("open_interest") or 0) for c in contracts)
    detail = f"{_fmt_int(total_oi)} contracts (exp {target_expiry.isoformat()}, spot {spot:.2f})"
    return CriterionResult("Options OI (NTM)", total_oi >= OPTIONS_OI_MIN, detail, threshold)


# --- Orchestration ----------------------------------------------------------


def validate_ticker(
    polygon: PolygonClient,
    finnhub: FinnhubClient,
    ticker: str,
    sector: str,
    as_of: date,
) -> TickerReport:
    report = TickerReport(ticker=ticker, sector=sector)
    runners: list[tuple[str, Callable[[], CriterionResult]]] = [
        ("ADV",               lambda: check_adv(polygon, ticker, as_of)),
        ("Analyst coverage",  lambda: check_analyst_coverage(finnhub, ticker)),
        ("Beta",              lambda: check_beta(polygon, ticker, as_of)),
        ("Market cap",        lambda: check_market_cap(polygon, ticker)),
        ("Options OI (NTM)",  lambda: check_options_oi(polygon, ticker, as_of)),
    ]
    for label, fn in runners:
        try:
            report.results.append(fn())
        except httpx.HTTPStatusError as e:
            report.results.append(
                CriterionResult(label, False, "API error", error=f"HTTP {e.response.status_code}")
            )
        except httpx.RequestError as e:
            report.results.append(
                CriterionResult(label, False, "network error", error=type(e).__name__),
            )
    return report


def format_report(report: TickerReport) -> str:
    lines = [f"{report.ticker}  [{report.overall}]"]
    label_width = max(len(r.name) for r in report.results) + 2
    detail_width = max(len(r.detail) for r in report.results) + 2
    for r in report.results:
        label = (r.name + ":").ljust(label_width)
        detail = r.detail.ljust(detail_width)
        suffix = r.status
        if r.status == "fail" and r.threshold:
            suffix += f" (need {r.threshold})"
        if r.error:
            suffix += f" [{r.error}]"
        lines.append(f"  {label}{detail}{suffix}")
    return "\n".join(lines)


def format_one_line(report: TickerReport) -> str:
    """Compact one-line summary for the suppressed-fails section in discovery."""
    fails = [
        f"{r.name}={r.detail}" + (f" [{r.error}]" if r.error else "")
        for r in report.results if r.status != "pass"
    ]
    return f"  {report.ticker:<6} {' | '.join(fails)}"


def run_discovery(
    polygon: PolygonClient,
    finnhub: FinnhubClient,
    config: dict[str, Any],
    as_of: date,
) -> int:
    """Pull holdings from each sector's discovery ETF, evaluate names not
    already in the universe, surface passing candidates."""
    discovery: dict[str, dict[str, Any]] = config.get("discovery_sources") or {}
    sectors: dict[str, list[str]] = config.get("sectors") or {}
    if not discovery:
        print(
            "error: no `discovery_sources` block in config — see asset-universe-validation.md",
            file=sys.stderr,
        )
        return 2

    print(f"Asset universe discovery — as of {as_of.isoformat()}")
    print(f"Source: {Path(config.get('__source_path__', 'config/assets.yaml'))}")
    print()

    # Dedup candidates against the whole universe, not just the same-sector list,
    # because a single sector ETF may overlap multiple of our sectors (e.g. XLK
    # covers GICS Information Technology, which spans our tech and semis).
    in_universe: set[str] = {t for tickers in sectors.values() for t in tickers}

    total_pass = 0
    total_fail = 0
    for sector, spec in discovery.items():
        try:
            holdings = fetch_etf_holdings(spec)
        except (httpx.HTTPError, ValueError, KeyError) as e:
            print(f"=== {sector}: discovery source unreachable: {type(e).__name__}: {e}")
            print()
            continue

        candidates = [t for t in holdings if t not in in_universe]
        print(f"=== {sector} (via {spec['etf']}, {len(holdings)} holdings, "
              f"{len(candidates)} not in universe) ===")
        print()

        passes: list[TickerReport] = []
        fails: list[TickerReport] = []
        for ticker in candidates:
            report = validate_ticker(polygon, finnhub, ticker, sector, as_of)
            if report.overall == "pass":
                passes.append(report)
            else:
                fails.append(report)

        if passes:
            print(f"✓ Add candidates ({len(passes)}):\n")
            for r in passes:
                print(format_report(r))
                print()
            total_pass += len(passes)

        if fails:
            print(f"✗ Did not qualify ({len(fails)}):")
            for r in fails:
                print(format_one_line(r))
            print()
            total_fail += len(fails)

    print("---")
    print(f"Summary: {total_pass} add candidates, {total_fail} did not qualify")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config", default="config/assets.yaml", type=Path,
        help="Path to assets.yaml (default: config/assets.yaml)",
    )
    parser.add_argument(
        "--date", default=None,
        help="Validation date YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--ticker", default=None,
        help="Validate a single ticker (must be present in assets.yaml)",
    )
    parser.add_argument(
        "--discover", action="store_true",
        help="Discovery mode: evaluate ETF holdings not in universe, surface candidates",
    )
    args = parser.parse_args()
    if args.discover and args.ticker:
        print("error: --discover and --ticker are mutually exclusive", file=sys.stderr)
        return 2

    sys.stdout.reconfigure(line_buffering=True)  # stream per-ticker progress when piped
    load_dotenv()  # picks up .env at the repo root if present

    polygon_key = os.environ.get("POLYGON_API_KEY")
    finnhub_key = os.environ.get("FINNHUB_API_KEY")
    missing = [
        n for n, v in [("POLYGON_API_KEY", polygon_key), ("FINNHUB_API_KEY", finnhub_key)]
        if not v
    ]
    if missing:
        print(f"error: missing environment variables: {', '.join(missing)}", file=sys.stderr)
        return 2

    if not args.config.exists():
        print(f"error: config not found at {args.config}", file=sys.stderr)
        return 2

    with args.config.open() as f:
        config = yaml.safe_load(f) or {}
    config["__source_path__"] = str(args.config)
    sectors: dict[str, list[str]] = config.get("sectors") or {}
    if not sectors:
        print(f"error: no sectors in {args.config}", file=sys.stderr)
        return 2

    as_of = date.fromisoformat(args.date) if args.date else date.today()

    polygon = PolygonClient(polygon_key)
    finnhub = FinnhubClient(finnhub_key)

    if args.discover:
        return run_discovery(polygon, finnhub, config, as_of)

    pairs: list[tuple[str, str]] = [
        (t, sector) for sector, tickers in sectors.items() for t in tickers
        if not args.ticker or t == args.ticker
    ]
    if not pairs:
        print(f"error: ticker {args.ticker!r} not found in {args.config}", file=sys.stderr)
        return 2

    print(f"Asset universe validation — as of {as_of.isoformat()}")
    print(f"Source: {args.config}")
    last = config.get("last_full_validation")
    if last:
        print(f"Previous full validation: {last}")
    print(f"Tickers under review: {len(pairs)}")
    print()

    pass_n = fail_n = unknown_n = 0
    for ticker, sector in pairs:
        report = validate_ticker(polygon, finnhub, ticker, sector, as_of)
        overall = report.overall
        if overall == "pass":
            pass_n += 1
        elif overall.startswith("unknown"):
            unknown_n += 1
        else:
            fail_n += 1
        print(format_report(report))
        print()

    print("---")
    print(f"Summary: {pass_n} pass, {fail_n} fail, {unknown_n} unknown")
    if fail_n == 0 and unknown_n == 0 and not args.ticker:
        print(f"Suggested: update last_full_validation to {as_of.isoformat()} in {args.config}")
    return 0 if fail_n == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
