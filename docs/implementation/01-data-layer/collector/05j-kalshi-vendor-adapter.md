---
status: in_progress
completed_date:
commit_id:
---

# 05j — Kalshi vendor adapter

## Goal

Implement `src/alphamind/data_sources/kalshi/` to pull Qual3 prediction-market data from Kalshi's session-token-authenticated API into `prediction_market_contracts` and `prediction_market_snapshots`.

## Reading

- `docs/design/01-data-layer/collector/storage.md` — `prediction_market_contracts`, `prediction_market_snapshots`
- `docs/design/01-data-layer/collector/lifecycle.md` § Bootstrap — forward-only
- `docs/design/01-data-layer/api-key-checklist.md` § Kalshi — auth endpoint, market endpoints
- `docs/design/01-data-layer/external/qualitative.md` § 3 — contract category taxonomy
- `docs/design/01-data-layer/api-failure-handling.md` — Qual3 optional

## Depends on

- 04 (shared library `_common.py`)

## Scope

In scope: under `src/alphamind/data_sources/kalshi/` —
- `client.py` — HTTP wrapper around Kalshi's REST API. Auth: POST `/trade-api/v2/login` with email + password to obtain a session token (30-min expiry). Cache the token in-memory and refresh on 401 or proactively before expiry. Integrates `with_retries(optional)`, `RateLimiter`, `verify_connectivity()`.
- `contracts.py`:
  - `collect_snapshots(since)` — pulls events (`/trade-api/v2/events`), then markets per relevant event (`/trade-api/v2/markets?series_ticker=...`). UPSERTs into `prediction_market_contracts`; writes `prediction_market_snapshots` per active market.
  - `category` derivation: Kalshi's `series_ticker` is a structured prefix (`FED`, `CPI`, `OPEC`, `ELECTION`, etc.) — map directly to the storage category enum. Unknown defaults to `other` with a warn-log.
  - `contract_id` is Kalshi's `market_ticker` (uniquely identifies a market).
  - No bootstrap function — forward-only.
- Unit tests with mocked HTTP responses (including the auth/refresh flow).

Out of scope:
- Order book detail (`/markets/{ticker}/orderbook`) — defer.
- Historical fills.

## Notes

Auth refresh: the session token expires after 30 minutes. `client.py` should detect expiry by 401 response and re-authenticate transparently within the same call. Track the token's issued-at timestamp and proactively refresh ~5 minutes before expiry to avoid mid-batch token failures.

Email + password live in `.env` per `api-key-checklist.md` (`KALSHI_EMAIL`, `KALSHI_PASSWORD`).

`yes_probability` mapping: Kalshi markets are binary YES/NO. Kalshi reports `yes_bid` and `yes_ask` (in cents 0–100); `yes_probability = (yes_bid + yes_ask) / 200`. Also write `bid` and `ask` directly (in dollars: `yes_bid / 100.0`, `yes_ask / 100.0`).

`liquidity_usd` from Kalshi's `volume` × `last_price` proxy or directly from open interest if exposed; document the choice in the module docstring.

Rate limit: not publicly documented. Set `data_sources.yaml.providers.kalshi.rate_limit_per_minute = 60` conservatively.

`resolution_outcome` updated on closed markets — Kalshi's market record carries a `status` field (`closed`/`finalized`) and a `result` field; map to `yes`/`no`/`undecided`.

## Acceptance criteria

- [ ] `client.py` performs login on first call and caches the session token.
- [ ] `client.py` re-authenticates transparently on 401 or proactive expiry.
- [ ] `client.py` has `verify_connectivity()` that exits 0 on successful login.
- [ ] `client.py` integrates `with_retries(optional)`, `RateLimiter`, and `track_run`.
- [ ] `contracts.collect_snapshots()` UPSERTs `prediction_market_contracts` rows for in-scope categories.
- [ ] `prediction_market_snapshots` rows include `yes_probability` derived from `(yes_bid + yes_ask) / 200`, plus raw `bid` and `ask`.
- [ ] Closed markets update `resolution_outcome` on the contract row.
- [ ] Re-running on the same window produces no duplicate snapshot rows.
- [ ] On failure, `collection_runs` records `failed`; no data rows.
- [ ] Unit tests with mocked HTTP responses cover the auth flow, refresh on 401, and the collection flow.
- [ ] `uv run pytest` passes.
