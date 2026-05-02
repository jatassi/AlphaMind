# API key & vendor inventory

Inventory of every external data provider AlphaMind reads from: authentication model, AlphaMind data domains served, and the specific endpoints each adapter touches. Used by the data-layer collector and as the lookup when keys need rotation.

---

## Paid (API key required)

### 1. Polygon.io (Stocks Starter + Options Starter)
- **Cost:** $58/mo combined ($46/mo annual)
- **Sign up:** https://polygon.io/pricing
- **Domains served:** Q1 (Price & Volume), Q3 (Derivatives/Options), Q6f (Forex rates), Q8 (Commodity tickers via stock-like symbols), Q10 (Corporate actions — dividends, splits), Ref (reference data, ticker details)
- **Endpoints:**
  - `GET /v2/aggs/ticker/AAPL/range/1/minute/2025-01-02/2025-01-02` — 1-min bars
  - `GET /v2/aggs/ticker/AAPL/range/15/minute/2025-01-02/2025-01-02` — 15-min bars
  - `GET /v2/aggs/ticker/AAPL/range/1/day/2024-01-01/2025-01-01` — daily bars
  - `GET /v2/aggs/ticker/AAPL/range/1/week/2024-01-01/2025-01-01` — weekly bars
  - `GET /v2/aggs/ticker/AAPL/prev` — previous day bar
  - `GET /v2/snapshot/locale/us/markets/stocks/tickers/AAPL` — real-time snapshot
  - `GET /v3/snapshot/options/AAPL` — options chain snapshot (greeks, IV, OI)
  - `GET /v3/reference/options/contracts?underlying_ticker=AAPL` — options contract reference
  - `GET /v3/reference/tickers/AAPL` — ticker details (market cap, SIC, etc.)
  - `GET /v3/reference/tickers?market=stocks&active=true&limit=100` — ticker list
  - `GET /v3/reference/dividends?ticker=AAPL` — dividend history
  - `GET /v3/reference/stock_splits?ticker=AAPL` — split history
  - `GET /v2/last/trade/AAPL` — last trade
  - `GET /v2/last/nbbo/AAPL` — last quote (NBBO)
  - `GET /v1/open-close/AAPL/2025-01-02` — daily open/close
  - `GET /v2/aggs/ticker/AAPL/range/1/minute/2025-01-02/2025-01-02?adjusted=true&sort=asc` — extended hours test (check `otc` field / pre/post market inclusion)
  - `GET /v2/aggs/grouped/locale/us/market/stocks/2025-01-02` — grouped daily (all tickers, one day — for sector ranking)
  - `GET /v3/reference/exchanges` — exchange reference
  - `GET /v2/aggs/ticker/C:EURUSD/range/1/day/2024-01-01/2025-01-01` — forex pair
  - `GET /v2/aggs/ticker/X:BTCUSD/range/1/day/2024-01-01/2025-01-01` — crypto pair (if needed)

---

## Free (API key required)

### 2. FRED (Federal Reserve Economic Data)
- **Cost:** Free
- **Sign up:** https://fred.stlouisfed.org/docs/api/api_key.html
- **Domains served:** Q6 (Macro — yields, inflation, employment, credit spreads, DXY, VIX, fed funds), Q8d (gold price), Q11a (VIX)
- **Endpoints:**
  - `GET /fred/series/observations?series_id=DGS10` — 10-year Treasury yield
  - `GET /fred/series/observations?series_id=DGS2` — 2-year yield
  - `GET /fred/series/observations?series_id=T10Y2Y` — 10Y-2Y spread
  - `GET /fred/series/observations?series_id=T10YIE` — 10Y breakeven inflation
  - `GET /fred/series/observations?series_id=BAMLH0A0HYM2` — HY OAS spread
  - `GET /fred/series/observations?series_id=BAMLC0A0CM` — IG OAS spread
  - `GET /fred/series/observations?series_id=VIXCLS` — VIX close
  - `GET /fred/series/observations?series_id=DTWEXBGS` — DXY (Trade Weighted Dollar)
  - `GET /fred/series/observations?series_id=DCOILWTICO` — WTI crude
  - `GET /fred/series/observations?series_id=CPIAUCSL` — CPI
  - `GET /fred/series/observations?series_id=PCEPI` — PCE
  - `GET /fred/series/observations?series_id=STLFSI4` — Financial Stress Index
  - `GET /fred/series/observations?series_id=SOFR` — SOFR rate
  - `GET /fred/series/observations?series_id=DFEDTARU` — Fed Funds upper target
  - `GET /fred/series?search_text=yield+curve` — series search (for discovery)

### 3. BLS (Bureau of Labor Statistics) API v2
- **Cost:** Free
- **Sign up:** https://data.bls.gov/registrationEngine/
- **Domains served:** Q6d (Employment detail — NFP, claims, ADP supplements)
- **Endpoints:**
  - `POST /publicAPI/v2/timeseries/data/` with body `{"seriesid": ["CES0000000001"], "startyear": "2024", "endyear": "2025"}` — Total nonfarm payrolls
  - `POST /publicAPI/v2/timeseries/data/` with body `{"seriesid": ["LNS14000000"]}` — Unemployment rate
  - `POST /publicAPI/v2/timeseries/data/` with body `{"seriesid": ["CUSR0000SA0"]}` — CPI-U (detailed)

### 4. EIA (Energy Information Administration)
- **Cost:** Free
- **Sign up:** https://www.eia.gov/opendata/register.php
- **Domains served:** Q8a (Crude oil inventory/production), Q8b (Natural gas storage), Q8e (Crack spreads/refinery), Qual6b (Energy catalysts)
- **Endpoints:**
  - `GET /v2/petroleum/stoc/wstk/data/?api_key={key}&frequency=weekly&data[0]=value&facets[product][]=EPC0&sort[0][column]=period&sort[0][direction]=desc&length=10` — Weekly crude inventory
  - `GET /v2/natural-gas/stor/wkly/data/?api_key={key}&frequency=weekly&data[0]=value&length=10` — Weekly gas storage
  - `GET /v2/petroleum/pri/spt/data/?api_key={key}&frequency=daily&data[0]=value&facets[series][]=RWTC&length=10` — WTI spot price
  - `GET /v2/petroleum/pnp/wiup/data/?api_key={key}&frequency=weekly&data[0]=value&length=5` — Refinery utilization

### 5. Alpaca Markets
- **Cost:** Free (paper trading account, no deposit)
- **Sign up:** https://app.alpaca.markets/signup
- **Domains served:** Q1 (backup OHLCV), Execution layer (paper trading — future use)
- **Endpoints:**
  - `GET /v2/stocks/AAPL/bars?timeframe=1Min&start=2025-01-02T00:00:00Z&end=2025-01-02T23:59:59Z` — 1-min bars
  - `GET /v2/stocks/AAPL/bars?timeframe=1Day&start=2024-01-01&end=2025-01-01` — daily bars
  - `GET /v2/stocks/AAPL/trades/latest` — latest trade
  - `GET /v2/stocks/AAPL/quotes/latest` — latest quote
  - `GET /v2/stocks/AAPL/snapshot` — snapshot

### 6. Finnhub
- **Cost:** Free tier
- **Sign up:** https://finnhub.io/register
- **Domains served:** Q5b (Earnings calendar), Q5f (Analyst ratings), Events:CAL (unified macro + policy event calendar), Q10d (Investor events), Qual1a (News)
- **Endpoints:**
  - `GET /api/v1/calendar/earnings?from=2025-01-01&to=2025-03-01` — earnings calendar
  - `GET /api/v1/stock/recommendation?symbol=AAPL` — analyst recommendations
  - `GET /api/v1/news?category=general` — general market news
  - `GET /api/v1/company-news?symbol=AAPL&from=2025-01-01&to=2025-01-31` — company news
  - `GET /api/v1/stock/insider-transactions?symbol=AAPL` — insider transactions
  - `GET /api/v1/calendar/ipo?from=2025-01-01&to=2025-06-01` — IPO calendar
  - `GET /api/v1/calendar/economic` — economic calendar
  - `GET /api/v1/stock/peers?symbol=AAPL` — company peers
  - `GET /api/v1/sec-filings?symbol=AAPL` — SEC filings
  - `GET /api/v1/fda-advisory-committee-calendar` — FDA calendar

### 7. StockTwits
- **Cost:** Free
- **Sign up:** https://stocktwits.com/ (create account) then request API access
- **Domains served:** Qual2a (Social sentiment)
- **Endpoints:**
  - `GET /api/2/streams/symbol/AAPL.json` — ticker stream with sentiment
  - `GET /api/2/trending/symbols.json` — trending tickers
  - `GET /api/2/streams/trending.json` — trending messages

### 8. Marketaux
- **Cost:** Free tier (100 req/day)
- **Sign up:** https://www.marketaux.com/register
- **Domains served:** Qual1a (News with pre-computed sentiment scores)
- **Endpoints:**
  - `GET /v1/news/all?symbols=AAPL&filter_entities=true&api_token={token}` — ticker news with sentiment
  - `GET /v1/news/all?countries=us&api_token={token}` — US market news

### 9. Kalshi
- **Cost:** Free (no deposit required)
- **Sign up:** https://kalshi.com/sign-up
- **Domains served:** Qual3a (Monetary policy odds), Qual3b (Regulatory/political outcomes), Qual3c (Geopolitical)
- **Endpoints:**
  - `POST /trade-api/v2/login` — get session token
  - `GET /trade-api/v2/events` — list events
  - `GET /trade-api/v2/markets?series_ticker=FED` — Fed-related markets
  - `GET /trade-api/v2/markets/{ticker}` — single market detail
  - `GET /trade-api/v2/series` — event series
  - `GET /trade-api/v2/markets/{ticker}/orderbook` — order book

### 10. Google Trends API
- **Cost:** Free (1,500 req/day)
- **Sign up:** https://console.cloud.google.com/ → enable Google Trends API
- **Domains served:** Qual2b (Alternative sentiment — search interest)
- **Endpoints:**
  - Interest over time for ticker-related terms
  - Related queries / rising queries

---

## No authentication (open access)

### 11. Treasury Fiscal Data
- **Auth:** None
- **Domains served:** Q6a (Treasury auction results, daily yield curve)
- **Endpoints:**
  - `GET /services/api/fiscal_service/v1/accounting/od/avg_interest_rates`
  - `GET /services/api/fiscal_service/v2/accounting/od/auctions_query`

### 12. SEC EDGAR
- **Auth:** User-Agent header only (format: `CompanyName email@address`)
- **Domains served:** Q5a-c (XBRL financials), Q5g (Insider transactions), Q10b-c-e (Filings), Qual5b (Regulatory actions)
- **Endpoints:**
  - `GET /api/xbrl/companyfacts/CIK0000320193.json` — Apple XBRL facts
  - `GET /cgi-bin/browse-edgar?action=getcompany&CIK=AAPL&type=10-K&dateb=&owner=include&count=5&search_text=&action=getcompany` — filing search
  - `GET /cgi-bin/browse-edgar?action=getcompany&CIK=AAPL&type=4&dateb=&owner=include&count=10` — Form 4 (insider)

### 13. CFTC COT Reports
- **Auth:** None
- **Domains served:** Q3c (Index futures positioning), Q8f (Commodity positioning)
- **Endpoints:**
  - Bulk CSV download from CFTC website
  - Public Reporting Environment API

### 14. FINRA (Short Interest + ATS Transparency)
- **Cost:** Free
- **Auth:** None — `User-Agent` header is the only requirement (CDN file downloads)
- **Domains served:** Q4a (Short interest), Q4c (Daily short volume), Q2c (Dark pool volume — deferred)
- **Endpoints:**
  - Daily short volume (Reg SHO): `https://cdn.finra.org/equity/regsho/daily/{PREFIX}shvol{YYYYMMDD}.txt` — pipe-delimited; published by 6pm ET on the trade date. Prefixes: `CNMS` (consolidated NMS), `FNRA` (TRF Carteret), `FNYX` (TRF NYSE), `FNQC` (TRF Chicago), `FORF` (ORF), `ADF` (ADF).
  - Bi-weekly short interest: `https://cdn.finra.org/equity/otcmarket/biweekly/shrt{YYYYMMDD}.csv` — CSV; published ~7–10 days after settlement (typically Tuesdays). FINRA's data glossary at `https://www.finra.org/finra-data/browse-catalog/equity-short-interest/glossary` documents the field set.
  - File layout reference: `https://www.finra.org/sites/default/files/DailyShortSaleVolumeFileLayout.pdf`.

### 15. Polymarket
- **Auth:** None (read-only)
- **Domains served:** Qual3a-c (Prediction markets — FOMC, regulatory, geopolitical)
- **Endpoints:**
  - `GET /markets` (Gamma API)
  - `GET /books` (CLOB API)
  - `GET /prices` (CLOB API)

### 16. Yahoo Finance (yfinance library)
- **Auth:** None (Python library, unofficial)
- **Domains served:** Q5d (Earnings estimates — PRIMARY), Q5f (Analyst price targets), Backup for Q1/Q3a/Q5
- **Endpoints:**
  - Python: `yfinance.Ticker("AAPL").get_earnings_estimate()`
  - Python: `yfinance.Ticker("AAPL").get_eps_revisions()`
  - Python: `yfinance.Ticker("AAPL").get_analyst_price_targets()`
  - Python: `yfinance.Ticker("AAPL").options` + `option_chain()`

### 17. iBorrowDesk (undocumented JSON API)
- **Cost:** Free (the site's $10/mo "Patron" tier unlocks website features only — there is no sanctioned programmatic-access tier at any price)
- **Auth:** None — browser-like `User-Agent` header expected
- **Domains served:** Q4b (Borrow cost / fee rates)
- **Endpoints:**
  - `GET https://www.iborrowdesk.com/api/ticker/{TICKER}` — undocumented JSON endpoint. Returns ~65 KB per ticker: 1 trading year of EOD daily history (`daily` array), trailing 3–5 days of ~16-min intraday snapshots (`real_time` array), and `latest_*` summary fields. Apex `iborrowdesk.com` 301-redirects to `www.` — follow redirects.
  - 404 returns a structured `{"errors": [{"code": "not_found", ...}]}` body — handle gracefully (universe coverage gaps).
- **Operational notes:**
  - Aggressive rate limit. Empirical: ~4 calls in tight succession trips an `HTTP 444` (NGINX `return 444` — connection terminated) block; the block persists across subsequent requests for an unknown duration. Pace at ≥5s between requests; treat 444 and TCP empty-reply as "blocked, back off heavily" distinct from 4xx/5xx.
  - Single-call payload covers everything the schema needs (1y daily + recent intraday) — daily collector cadence is sufficient; on-demand single-ticker refresh handles fresher analyst-tool requests.
  - Endpoint is undocumented: monitor for breakage; no SLA exists.

### 18. Earnings Transcripts (Motley Fool / Quartr / YouTube)
- **Auth:** None (scraping-based)
- **Domains served:** Qual4a-b (Earnings call transcripts)
- **Endpoints:**
  - Scrape fool.com/earnings-call-transcripts/
  - Quartr API (if available)
  - YouTube webcast URLs + Whisper transcription

---

## Summary

| # | Source | Auth | Cost |
|---|--------|------|------|
| 1 | Polygon.io | API key | $58/mo |
| 2 | FRED | API key | Free |
| 3 | BLS | API key | Free |
| 4 | EIA | API key | Free |
| 5 | Alpaca | API key + secret | Free (paper) |
| 6 | Finnhub | API key | Free |
| 7 | StockTwits | API key / OAuth | Free |
| 8 | Marketaux | API token | Free |
| 9 | Kalshi | Email + password (session token) | Free |
| 10 | Google Trends | GCP API key / OAuth | Free |
| 11 | Treasury Fiscal Data | None | Free |
| 12 | SEC EDGAR | User-Agent header | Free |
| 13 | CFTC COT Reports | None | Free |
| 14 | FINRA | User-Agent header | Free |
| 15 | Polymarket | None | Free |
| 16 | Yahoo Finance (yfinance) | None | Free |
| 17 | iBorrowDesk | User-Agent header | Free |
| 18 | Earnings Transcripts | None (scraping) | Free |

Live keys are stored locally in `docs/design/01-data-layer/api-keys.md` (gitignored) and consumed via `.env` per `data_sources.yaml`.
