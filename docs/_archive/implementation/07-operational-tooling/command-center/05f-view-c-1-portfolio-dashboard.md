# 05f — View C-1 — Portfolio dashboard

## Goal

Ship the single-page portfolio dashboard from view group C: equity + P/L pane, cash + capital pane (including Reg T excess trailing window), exposure pane, positions table, pending orders table. Reads-only; no controls. Live-updates via the SSE stream on `monitor:fill_received` (positions row mutations) and on each invocation end (portfolio_summary refresh).

## Reading

* `docs/design/command-center.md` § Portfolio dashboard — full pane breakdown.
* `docs/design/05-execution-layer/regt-margin-attribution.md` — Reg T excess trailing-window aggregation; the cash+capital pane renders this.
* `src/alphamind/state/tables/positions.py`, `theses.py`, `cash_ledger.py`, `orders.py`, `fill_records.py` — table shapes the dashboard reads.
* `src/alphamind/portfolio_state/` — `portfolio_summary` and `thesis_quality_aggregates` derived aggregates (search for where these live in the package; they're refreshed at mutation time per the design).

## Depends on

* [ALP-666](<https://linear.app/alphamind-jatassi/issue/ALP-666>) (02) — foreign_reader_session.
* [ALP-670](<https://linear.app/alphamind-jatassi/issue/ALP-670>) (04d) — frontend foundation.

## Scope

Backend at `src/alphamind/command_center/views/portfolio.py` (the dashboard endpoint). Frontend at `frontend/src/routes/_authed/portfolio/index.tsx` + `frontend/src/views/portfolio/dashboard/` (one component per pane). Tests at `tests/command_center/views/test_portfolio_dashboard.py` + Vitest. Frontend toolchain is bun per [ALP-128](https://linear.app/alphamind-jatassi/issue/ALP-128/command-center) § Pre-resolved (J).

### 1\. Backend

`GET /api/views/portfolio/dashboard` — composed read of all five panes' data:

* `equity_and_pl`: total_value, high_water_mark, current_drawdown, daily_realized_pl, cumulative_realized_pl, total_unrealized_pl (from portfolio_summary).
* `cash_and_capital`: cash, settled_cash, reserved_capital, available_buying_power, margin_held, unsettled_proceeds_with_settlement_dates, regt_excess_trailing (30d, 90d, lifetime) — aggregated from fill_records.regt_margin_attribution per regt-margin-attribution.md.
* `exposure`: gross_exposure_pct, net_long_pct, net_short_pct, sector_breakdown (long + short separately), delta_adjusted_equivalents.
* `positions`: one row per open position with ticker, instrument type, direction, qty, market_value, unrealized_pl, thesis_status, age, distance_to_target, distance_to_nearest_invalidation (positions joined to theses).
* `pending_orders`: one row per order with status pending or partially-filled.

### 2\. Frontend

`/portfolio` route — five panes vertically stacked with summary + tables. Live updates via `useEventStream()` subscribing to `monitor:fill_received` (refetch the dashboard query) and `pipeline:invocation_ended` (same).

Position rows click → deep-link to position detail (`/portfolio/positions/$positionId`, story 05g). Order rows can't be clicked in v1 (no order detail view).

### 3\. Vitest tests

Each pane component renders documented fields against fixture data; live-update refetches on relevant SSE events.

## Acceptance criteria

- [ ] `GET /api/views/portfolio/dashboard` returns all five panes' data in one response.
- [ ] Reg T excess trailing-window calculation queries fill_records.regt_margin_attribution via single-query `json_extract` per regt-margin-attribution.md.
- [ ] Frontend `/portfolio` renders all five panes.
- [ ] Live updates fire on `monitor:fill_received` + `pipeline:invocation_ended`.
- [ ] Position rows deep-link to `/portfolio/positions/$positionId`.
- [ ] Vitest tests pass.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, `uv run lint-imports`, frontend `bun run typecheck && bun run lint && bun run test` all pass.

## Verification

Scoped pytest: `uv run pytest tests/command_center/views/ -n auto`. Frontend: `cd src/alphamind/command_center/frontend && bun run test`.