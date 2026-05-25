## Context

The categorizer logic in `src/alphamind/data_sources/polymarket/contracts.py` and `src/alphamind/data_sources/kalshi/contracts.py` is broken in production. All \~50,955 `prediction_market_contracts` rows currently carry `category='other'` despite descriptions clearly being about elections, Fed policy, OPEC, etc. This blocks [ALP-274](https://linear.app/alphamind-jatassi/issue/ALP-274/wire-prediction-market-contract-scope-into-distillation-orchestrator)'s category-allowlist approach to scoping the distillation orchestrator's prediction-market refresh — the allowlist would resolve to zero contracts.

## Findings (from grilling)

**Polymarket** `/markets` endpoint. The `tags` field is literally `null` in every API response. Categorizer reads `market.get("tags") or []`, gets empty list, falls through to `'other'`. Production logs show 205,290+ warnings of `tags=[]` in a single day. Even the documented `category` field at the market level is null in live API responses.

**Polymarket** `/events` endpoint. Has rich `tags[].label` populated on every event ("Politics", "Elections", "Geopolitics", "Sports", "Crypto", "Trump", "Tech", "Finance", ...). `/events` also embeds `markets[]` but **strips fields critical to snapshots** — `outcomePrices` missing on 47% of markets, `liquidity` missing on 16%, `bestBid` missing on 23%, `volume24hr` missing on 27%. Cannot replace `/markets` loop wholesale.

**Polymarket** `outcome` field. Universally `null` even on closed markets. The actual resolution lives in `outcomePrices` array (e.g., `["0", "0.99..."]` = resolved NO). Result: 0 of 50,149 Polymarket rows ever have `resolution_outcome` set.

**Kalshi** `/events` endpoint. Exposes `event.category` directly with vendor's own taxonomy ("Politics", "Elections", "Economics", "World", "Financials", "Companies", "Entertainment", "Climate and Weather", "Sports", "Science and Technology", "Health", "Social", "Transportation"). Already in the response we fetch — one `dict.get` away. Existing `SERIES_CATEGORY_MAP` keys (`FED`, `FOMC`, `OPEC`, `ELECTION`, ...) match zero of the real `KX*`-prefixed series tickers in the live catalog.

## Scope

**(1) Package consolidation.** Restructure `src/alphamind/data_sources/polymarket/` and `src/alphamind/data_sources/kalshi/` under a new `src/alphamind/data_sources/prediction_market/` package with `polymarket/`, `kalshi/`, and `categories.py` siblings. Update all import sites across the codebase.

**(2) Shared categorizer module.** New `src/alphamind/data_sources/prediction_market/categories.py` exposing `derive_canonical_category(*, description: str, vendor_labels: tuple[str, ...]) -> str`. Returns one of 11 canonical AlphaMind categories: `monetary_policy`, `antitrust`, `trade`, `financial_regulation`, `fiscal_policy`, `election`, `opec`, `conflict`, `sanctions`, `china_policy`, `corporate_action`, plus sentinel `other`.

**(3) Hybrid categorization architecture.** Vendor labels act as a pre-filter — events/markets without any "relevant" vendor label become `other` immediately and skip keyword matching. Polymarket passthrough labels: Politics, Elections, US Election, Midterms, Senate midterms, Governor midterms, Global Elections, World, Geopolitics, Ukraine, Trump, Trump Presidency, Finance, Tech. Kalshi passthrough categories: Politics, Elections, Economics, World, Financials, Companies. Description-keyword rules then assign the fine-grained canonical category from the 11.

**(4) Polymarket fix.** Keep `/markets` as primary loop (preserves snapshot fidelity). After collecting markets, batch-fetch `/events?id=A&id=B&...` for unique event IDs to retrieve `tags[].label`. Build in-memory `event_id → vendor_labels` map. Pass `(market.question, vendor_labels_for_event)` to `derive_canonical_category`. Drop the broken `_derive_category(tags)` path entirely.

**(5) Kalshi fix.** Read `event.category` from the `/events` response we already fetch. Drop the stale `SERIES_CATEGORY_MAP`. Pass `(market.title, (event.category,))` to `derive_canonical_category`.

**(6) Polymarket resolution-outcome fix.** Replace `market.get("outcome")` (universally null per Gamma API) with derivation from `outcomePrices` array: when `closed=True` and max(prices) ≥ 0.99, set resolution to `"yes"` or `"no"` based on which index dominates; when both prices are 0, set to `"canceled"`; otherwise leave `None`.

**(7) Test rewrite.** Update `tests/data_sources/polymarket/test_contracts.py` and `tests/data_sources/kalshi/test_contracts.py` for the new package layout, vendor pre-filter, and 11-category outputs. Add new unit tests for `prediction_market/categories.py` covering each canonical bucket, pre-filter rejection, ambiguous descriptions, and empty vendor labels.

**(8) Categorize-only, don't skip ingestion.** Keep all rows in `prediction_market_contracts` regardless of category — rejected ones get `category='other'`. Orchestrator allowlist ([ALP-274](https://linear.app/alphamind-jatassi/issue/ALP-274/wire-prediction-market-contract-scope-into-distillation-orchestrator)) excludes `other`. Preserves snapshot history and lets us re-tune the taxonomy later without re-ingestion.

## Backfill

None required. Polymarket UPSERTs every contract every cycle, refreshing the `category` column. Convergence to the new taxonomy is automatic within 1–2 ingestion cycles.

## Acceptance criteria

* After two full Polymarket ingestion cycles post-deploy, `SELECT category, COUNT(*) FROM prediction_market_contracts WHERE platform='polymarket' GROUP BY category` shows multiple non-`other` categories with non-zero counts (today: zero).
* Kalshi categorization is non-`other` for events whose `event.category` matches one of the pre-filter passthroughs.
* `derive_canonical_category` unit tests cover all 11 canonical buckets plus pre-filter rejection paths.
* Polymarket `resolution_outcome` is non-null for at least some closed markets after re-ingestion.
* `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` clean.

## Out of scope — follow-up ticket

Periodic sweep of `closed=true` Polymarket markets to backfill `resolution_outcome` on the 38,088 aged-out rows currently universally `NULL`. Ingestor's liquidity-floor (`liquidity ≥ $10K`) silently prunes closed markets at ingestion, so they never get re-fetched after liquidity collapses. Separate ticket recommended.

## Blocks

[ALP-274](https://linear.app/alphamind-jatassi/issue/ALP-274/wire-prediction-market-contract-scope-into-distillation-orchestrator) (orchestrator wiring) — its category-allowlist approach requires this categorizer to populate non-`other` categories first.