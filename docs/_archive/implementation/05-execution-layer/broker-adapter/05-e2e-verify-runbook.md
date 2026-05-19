# 05 — E2E verify + runbook

## Goal

Ship `src/alphamind/scripts/verify_broker_adapter.py` exercising the broker adapter end-to-end against Alpaca's paper environment, plus `scripts/RUNBOOK_broker_adapter.md` with operator prerequisites + invocation + expected output + failure-mode triage, and an insertion into `scripts/RUNBOOK_end_to_end_verification.md` placing the broker adapter into the dependency-ordered phase list. The verify script exercises three instrument tracks (equity / options / mleg), the venue-config primitives in isolation, and the disconnect-recovery routine, producing a pass/fail summary an operator can read.

## Reading

* `scripts/RUNBOOK_end_to_end_verification.md` — the central runbook this story extends with the broker-adapter phase.
* `scripts/RUNBOOK_oms_commands.md`, `scripts/RUNBOOK_state_persistence.md`, `scripts/RUNBOOK_position_thesis_model.md` — sibling runbooks; mirror their structure (purpose, prerequisites, invocation, phases, expected output, failure-mode triage).
* `src/alphamind/scripts/verify_oms_commands.py`, `verify_state_persistence.py` — sibling verify scripts for structure + reporting conventions.
* All <issue id="e12a677b-a183-4f5d-bc48-5669337441c1">ALP-121</issue> child stories' acceptance criteria (this story exercises each).
* `docs/design/05-execution-layer/broker-adapter.md` § entire — the contract being verified.
* `docs/design/05-execution-layer/venue-configuration.md` § entire — the venue-config contract.
* <issue id="e12a677b-a183-4f5d-bc48-5669337441c1">ALP-121</issue> parent body § Pre-resolved configuration decisions (G), (I), (J) — Phase 1 extensions covered, env-var prereqs, isolation-style venue-config verify.

## Depends on

* <issue id="c2950555-2a1c-4bc4-b07a-03999713ebdf">ALP-385</issue> (02g — Venue constants module).
* <issue id="9c9aacfd-3da4-4968-9061-4fad9defd0ea">ALP-386</issue> (03a — Trading calendar / clock cache).
* <issue id="0e86cadb-c99c-427e-a1ea-367771badf18">ALP-387</issue> (03b — Account-derived venue state surfacer).
* <issue id="37d69658-5765-4630-b70b-64745af141bb">ALP-389</issue> (03d — Disconnect recovery).
* <issue id="d9834605-9ab3-42ce-af93-4db8358a55c4">ALP-390</issue> (03e — Engine-stub coordinated swap).
* <issue id="9f69a900-7683-47d7-b7a1-d3039c385abd">ALP-391</issue> (04a — Settlement-date calculator).
* <issue id="5cb87113-0f45-4380-9ba1-19774dd9c07c">ALP-392</issue> (04b — Strategy/mleg-position Phase 1 fill integration).

(Wave 2 stories are transitively required via wave-3 / wave-4 dependencies; no need to list 02a–g explicitly.)

## Scope

Source: `src/alphamind/scripts/verify_broker_adapter.py` (new). Runbook: `scripts/RUNBOOK_broker_adapter.md` (this story replaces the stub from story 01). Central-runbook insertion: `scripts/RUNBOOK_end_to_end_verification.md` (existing — edit).

### 1. `verify_broker_adapter.py` structure

Mirror existing verify-script conventions: argparse for flag config + paper/live + verbose, an outer `main()` that loads config + builds the factory, then a sequence of `phase_*()` functions each returning a `PhaseResult` (PASS / FAIL / DEFERRED), then a summary printer that returns nonzero on any FAIL.

Phases:

#### Phase 1 — Adapter substrate (story 01)

* Build `AlpacaClientFactory(venue, mode="paper")`.
* Build a `TradingClient` and `TradingStream` from the factory.
* Call `submit_with_retry` with a no-op success callable; assert `Submitted` returned with `attempt_count=1`.
* Call `submit_with_retry` with a callable that raises a transient exception twice then returns; assert retry behavior.
* Call `classify_alpaca_error` with synthetic 422 / 403 / 5xx exceptions; assert each maps correctly.

#### Phase 2 — Account state queries (story 02a)

* `queries.get_account()` against live paper Alpaca; assert non-None equity, cash, etc.
* `queries.get_positions()` returns a tuple (may be empty if no positions).
* `queries.get_asset("NVDA")` returns a populated `AssetSnapshot` with `tradable=True`.
* `queries.get_asset("ZZNONEXISTENT")` returns `None`.
* `queries.get_calendar(start=today, end=today+30)` returns 20+ business-day records.
* `queries.get_clock()` returns a sensible `MarketClock`.
* `async for o in queries.get_orders(status="all", since=today-7d)` paginates and yields any historical paper orders.

#### Phase 3 — Equity order lifecycle (stories 02b, 02e, 02f, 03c, 03e via OMS)

* Submit a paper-mode OPEN command for an equity bracket order via the OMS path (story 03e). Assert `Submitted` with real Alpaca order ID.
* Wait briefly (paper Alpaca usually fills in seconds); subscribe to `trade_updates` and collect events until the entry fills.
* Assert the `position_opened`, `bracket_activated`, `cash_debited` activity-log entries appear in the DB.
* Submit a CLOSE command for the position; collect the close fill via the websocket; assert `position_closed` with `realized_pl` populated.
* Submit a CANCEL on a hypothetical pending order; assert `submit_cancel` succeeds.
* Submit an ADJUST on a pending bracket leg; assert `submit_replace` produces a new Alpaca order ID and `alpaca_order_id_chain` extends.

#### Phase 4 — Options order lifecycle (story 02c, 03c, 03e)

* Submit a paper-mode OPEN command for a single-leg long-call options instrument. Assert OCC symbol construction matches expected. Assert `OrderClass.SIMPLE` and `TimeInForce.DAY`.
* Wait for fill; assert `position_opened` with options-specific detail.
* Submit a CLOSE; assert `position_closed`.

#### Phase 5 — Mleg order lifecycle (story 02d, 04b, 03e)

* Submit a paper-mode OPEN command for a 2-leg vertical-spread strategy. Assert mleg request shape (legs, position_intent, ratio_qty).
* Wait for all-legs-filled; assert per-leg correlation in the websocket events; assert the position transitions PENDING → OPEN only at the last leg.
* Assert `position_opened` activity-log carries strategy-level net cost basis.

#### Phase 6 — Venue config in isolation (stories 02g, 03a, 03b, 04a)

* Assert all expected constants from `02g` exist with documented values.
* Build a `TradingCalendarCache(queries)`; call `is_market_open(now)` and assert the answer matches `queries.get_clock().is_open`.
* Call `business_day_offset(today, 1)`; assert it skips weekends.
* Call `read_venue_account_state(queries)`; print resolved tier + headroom for operator inspection.
* Call `compute_settlement_date(today, "equity", calendar=cache)`; assert it equals `business_day_offset(today, 1)`.

#### Phase 7 — Disconnect recovery (story 03d)

* Submit an order, get the Alpaca order ID.
* Call `recover_missed_fills_since(queries, since=today-1h)`; assert the just-submitted order's events are yielded.
* Construct a synthetic order set across statuses (filled, canceled, replaced) and assert each yields the right `FillReport.event_type`.

### 2. `RUNBOOK_broker_adapter.md`

Replace the story-01 stub with full content:

* Title + purpose ("Verify the broker adapter end-to-end against Alpaca paper").
* Prerequisites:
  * `ALPACA_PAPER_KEY`, `ALPACA_PAPER_SECRET` environment variables set (instructions to source `.env` or set inline).
  * `config/main.yaml` set to `execution_mode: paper`.
  * Fresh tmp DB or fresh state-persistence migration applied.
  * `uv run pytest -n auto` is green before running verify.
* Invocation: `uv run python -m alphamind.scripts.verify_broker_adapter`.
* Expected output shape: phase-by-phase PASS/FAIL/DEFERRED markers.
* Failure-mode triage table: common failures and their proximate causes (env vars unset, wrong execution_mode, paper account locked, network issues, alpaca-py version mismatch).
* Reference to: <issue id="e12a677b-a183-4f5d-bc48-5669337441c1">ALP-121</issue> (parent), <issue id="ddc9b5a4-a59d-4d3e-93cd-2fd41b81d717">ALP-378</issue>–392 (child stories).

### 3. `RUNBOOK_end_to_end_verification.md` insertion

Add the broker-adapter phase to the central runbook's dependency-ordered phase list. The broker adapter sits AFTER state-persistence (already in the list) and BEFORE the as-yet-unwritten continuous-monitor / paper-evaluation-harness / corporate-actions / decision-layer-pipeline phases. Stage-artifact handoff: the broker adapter produces real Alpaca paper acks consumed by Phase 1 (state-persistence), so position state in the DB after broker-adapter phase reflects real paper positions.

### 4. Project-tracker update

Although the project-tracker.md edit is the post-Phase-7 commit (handled by the `/draft-user-stories` final pass, NOT this story), this story flags any related runbook edits the operator should know about — the central RUNBOOK insertion is the load-bearing one.

### Out of scope

* Live-mode verify — paper only.
* Continuous-monitor lifecycle wiring — <issue id="734df060-3ba0-4514-8e97-bfc009e6b677">ALP-123</issue>.
* Decision-layer composition (full pipeline runner threading PM envelope through OMS to broker) — separate Substantial entry in project-tracker.md.
* Paper-evaluation harness annotation of fills — <issue id="aa192802-32fd-4562-b48a-a8452298e176">ALP-130</issue>.

## Acceptance criteria

- [ ] `src/alphamind/scripts/verify_broker_adapter.py` exists and is invokable via `uv run python -m alphamind.scripts.verify_broker_adapter`.
- [ ] The script accepts `--mode {paper,live}` (default paper), `--verbose`, and `--phase <name>` (optional filter).
- [ ] Each of the 7 phases above is implemented as a `phase_*()` function returning a typed `PhaseResult`.
- [ ] Running with `--mode=paper` against a configured paper account: every phase reports PASS or DEFERRED (DEFERRED for any path the script intentionally skips, e.g., live-mode-specific assertions).
- [ ] The summary printer returns exit code 0 on all-pass, nonzero on any FAIL.
- [ ] When `ALPACA_PAPER_KEY` is unset: the script fails with a clear message naming the missing env var (per story 01's `RuntimeError`).
- [ ] When `config/main.yaml` is set to live but live credentials are unset: the script fails with a similar clear message.
- [ ] `scripts/RUNBOOK_broker_adapter.md` exists with: title, purpose, prerequisites (env vars, execution_mode, fresh DB), invocation command, expected output shape, failure-mode triage table.
- [ ] `scripts/RUNBOOK_end_to_end_verification.md` carries a broker-adapter section in the dependency-ordered phase list, placed between state-persistence and the not-yet-existing continuous-monitor phases.
- [ ] After running this verify against a fresh paper account, the SQLite DB contains real Alpaca paper-mode order records, fill records, and position records — not synthetic stubs.
- [ ] The verify script's options + mleg phases exercise the Phase 1 extensions from stories 03c + 04b (the `position_opened` events for those instrument types appear in the activity log).
- [ ] The verify script can be re-run idempotently against a fresh tmp DB — running twice in a row produces the same PASS summary.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` is clean.

## Verification

* Run `uv run python -m alphamind.scripts.verify_broker_adapter --mode=paper` against a live Alpaca paper account: every phase reports PASS.
* Run `uv run pytest -n auto`: full suite green.
* Verify the central runbook by reading `scripts/RUNBOOK_end_to_end_verification.md` end-to-end and confirming the broker-adapter phase is correctly positioned.
* Lint clean per CLAUDE.md.
