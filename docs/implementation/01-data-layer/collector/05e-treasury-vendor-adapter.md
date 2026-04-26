---
status: not_started
completed_date:
commit_id:
---

# 05e — Treasury vendor adapter

## Goal

Implement `src/alphamind/data_sources/treasury/` to pull Treasury auction results from the Treasury Fiscal Data API into `treasury_auctions`.

## Reading

- `docs/design/01-data-layer/collector/storage.md` — `treasury_auctions` schema
- `docs/design/01-data-layer/collector/lifecycle.md` § Bootstrap — 12-month depth
- `docs/design/01-data-layer/api-key-checklist.md` § Treasury Fiscal Data — endpoint inventory
- `docs/design/01-data-layer/api-failure-handling.md` — Q6 macro is critical

## Depends on

- 04 (shared library `_common.py`)

## Scope

In scope: under `src/alphamind/data_sources/treasury/` —
- `client.py` — HTTP wrapper around Treasury Fiscal Data API (no auth). Integrates `with_retries(critical)`, `RateLimiter` (token-bucket against a sane default since the API doesn't publish a hard limit), `verify_connectivity()`.
- `auctions.py`:
  - `collect_auctions(since)` and `bootstrap_auctions()` — pulls from `/services/api/fiscal_service/v2/accounting/od/auctions_query` filtered for tenors of interest (`2Y`, `5Y`, `10Y`, `30Y`).
  - Writes `treasury_auctions` rows. `auction_id` synthesized as `{auction_date}_{tenor}` (e.g., `2026-03-15_10Y`).
- Unit tests with mocked HTTP responses.

Out of scope:
- Daily yield curve series (those come from FRED in story 05b — `DGS2`, `DGS10`, etc.).
- Average interest-rate series (`/services/api/fiscal_service/v1/accounting/od/avg_interest_rates`) — not used by current distillation; defer.

## Notes

The Treasury Fiscal Data API uses query-string filters and pagination. Default page size is 100; enable pagination when a query window crosses page boundaries.

Auction tenors of interest: `2Y`, `5Y`, `10Y`, `30Y`. The API field is `security_term`; values like `2-Year`, `5-Year`, etc. — normalize to the storage schema's short form (`2Y`, etc.).

Field mapping (auctions endpoint → `treasury_auctions` columns):
- `record_date` → `auction_date`
- `high_yield` (or `high_investment_rate`) → `auction_yield_bp` (× 100 if reported as percent)
- `bid_to_cover_ratio` → `bid_to_cover`
- `tail_basis_point` → `tail_bp`
- `primary_dealer_amt_pct` → `primary_dealer_pct`
- `indirect_bidder_amt_pct` → `indirect_pct`
- `direct_bidder_amt_pct` → `direct_pct`
- `total_accepted_amt` (in millions, divide by 1000) → `auction_size_usd` (billions)

Verify field names against the live API response shape before writing — Treasury's field naming has historically shifted.

## Acceptance criteria

- [ ] `client.py` has `verify_connectivity()` that exits 0 (no auth required, just a reachability check).
- [ ] `client.py` integrates `with_retries(critical)`, `RateLimiter`, and `track_run`.
- [ ] `auctions.collect_auctions()` writes `treasury_auctions` rows for the four tenors of interest within the requested window.
- [ ] `auctions.bootstrap_auctions()` covers 12 months of history.
- [ ] `auction_id` is the documented `{auction_date}_{tenor}` form.
- [ ] Re-running on the same window produces no duplicate rows.
- [ ] On failure, `collection_runs` records `failed`; no data rows.
- [ ] Unit tests with mocked HTTP responses cover the above.
- [ ] `uv run pytest` passes.
