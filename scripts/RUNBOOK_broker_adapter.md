# Broker Adapter Verification Runbook

Operator workflow for the ALP-393 verification artifact that proves the
broker adapter (ALP-121) is wired correctly against Alpaca's paper
environment: the adapter substrate (factory + retry + error mapping),
account-state queries, full equity / options / multi-leg order
lifecycles, the venue-configuration primitives in isolation, and the
disconnect-recovery routine.

Run after any change to `src/alphamind/execution/broker_adapter/`,
`src/alphamind/execution/venue_configuration/`, or
`src/alphamind/execution/oms/broker_dispatch.py`. Live paper account
required — this is the first verify in the AlphaMind pipeline that
exercises real broker submissions.

## Purpose

`scripts/verify_broker_adapter.py` exercises the broker adapter
end-to-end across seven phases plus a summary:

- **Phase 1 — Adapter substrate.** Builds `AlpacaClientFactory(venue,
  mode="paper")`, mints `TradingClient` + `TradingStream` instances,
  drives `submit_with_retry` through a first-try success and a
  transient-then-success retry, and probes `classify_alpaca_error`
  with synthetic 4xx / 5xx exceptions.
- **Phase 2 — Account state queries.** Drives every
  `AccountStateQueries` method against the live paper account:
  `get_account()` returns non-None equity / cash; `get_positions()`
  returns a tuple (may be empty); `get_asset("NVDA")` returns a
  populated `AssetSnapshot` with `tradable=True`;
  `get_asset("ZZNONEXISTENT")` returns `None`; `get_calendar(today,
  today+30)` returns ≥ 20 business-day records; `get_clock()` returns a
  sensible `MarketClock`; `get_orders(status="all")` paginates without
  raising.
- **Phase 3 — Equity order lifecycle.** Submits a paper-mode OPEN bracket
  via the OMS path (story 03e), waits for the entry fill via
  `trade_updates`, asserts the documented activity-log entries land,
  closes the position, asserts the close fill emits
  `position_closed`, then drives a CANCEL on a hypothetical pending
  order plus an ADJUST on a pending bracket leg.
- **Phase 4 — Options order lifecycle.** Submits a paper-mode OPEN on a
  single-leg long-call options instrument, asserts the OCC symbol
  construction, waits for the fill, then submits a CLOSE.
- **Phase 5 — Multi-leg strategy lifecycle.** Submits a paper-mode OPEN
  on a 2-leg vertical-spread strategy, asserts the mleg request shape
  (per-leg `position_intent`, `ratio_qty`), waits for all legs to fill,
  asserts the position transitions PENDING → OPEN only at the last leg.
- **Phase 6 — Venue configuration in isolation.** Asserts every constant
  exported from `venue_configuration.constants` matches its documented
  value; builds a `TradingCalendarCache` and confirms `is_market_open`
  matches `get_clock().is_open`; calls `business_day_offset(today, 1)`
  and asserts it skips weekends; calls `read_venue_account_state` and
  prints the resolved tier + headroom for operator inspection; calls
  `compute_settlement_date(today, "equity")` and asserts it equals the
  business day after the effective trade date.
- **Phase 7 — Disconnect recovery.** Calls `recover_missed_fills_since`
  with a 1-hour lookback against the live paper account; asserts it
  iterates without raising. Translator-side coverage of every Alpaca
  status → `FillReport.event_type` mapping lives in the unit-test
  suite at `tests/execution/broker_adapter/test_recovery.py`.
- **Summary.** One `[PASS]` / `[DEFERRED]` / `[FAIL]` line per phase plus
  a `RESULT: PASS (N pass, M deferred)` or
  `RESULT: FAIL (N pass, ..., M fail)` summary. DEFERRED phases (e.g.
  live-mode-only assertions in paper mode) count as not-failing for
  exit-code purposes.

### Subagent / CI versus operator runs

The test suite at `tests/scripts/test_verify_broker_adapter.py`
exercises every phase function with a mocked `TradingClient` (the same
`responses`-mocked pattern story 02a established for `queries.py`
tests). The "every phase reports PASS against a configured paper
account" acceptance criterion is **operator territory** — it requires
real Alpaca paper credentials and is verified by the operator running
this script against `--mode=paper` per the invocation block below. Subagent
worktree runs cannot reach this criterion because the paper credentials
are intentionally not provisioned in the worktree env.

## Prerequisites

1. **`uv sync` completed.** `uv run` is the entry point.
2. **Alpaca paper credentials sourced into the environment.** Set both
   `ALPACA_PAPER_KEY` and `ALPACA_PAPER_SECRET`. The canonical workflow
   on macOS / Linux:

   ```bash
   set -a && source .env && set +a
   ```

   On the Windows production machine, the env vars are persisted via
   the operator's user profile; no per-shell sourcing required. If
   either var is unset, the script exits 1 with the message
   `Alpaca paper credentials not set: environment variable(s)
   ['ALPACA_PAPER_KEY'] are unset or empty`.
3. **`config/main.yaml` set to `execution_mode: paper`.** The default
   ships paper-mode; confirm with
   `grep execution_mode config/main.yaml`. Live-mode runs require both
   `ALPACA_LIVE_KEY` and `ALPACA_LIVE_SECRET` and are out of scope for
   this story.
4. **Fresh tmp DB or the production migration head applied.** Phases 3
   / 4 / 5 write through to the OMS Phase 2 path which lands real
   broker acks in the activity log. Run against a tmp DB to avoid
   polluting the production state ledger:

   ```bash
   mkdir -p /tmp/alphamind-verify-broker
   rm -f /tmp/alphamind-verify-broker/alphamind.db
   uv run alembic -c alembic.ini \
       -x db=/tmp/alphamind-verify-broker/alphamind.db upgrade head
   ```

   The `-x db=<path>` arg is the `alembic env.py` contract; plain
   `-x db_url=…` is silently ignored.
5. **`uv run pytest -n auto` is green** before invoking the verify. A
   broken substrate test means the broker adapter isn't internally
   consistent — running the live-paper drive will produce confusing
   failures.
6. **The work tree's integration branch (`jackson/alp-121-broker-adapter`)
   is reachable from HEAD** — every story (ALP-378 through ALP-392) must
   have landed.

## Invocation

```bash
uv run python -m alphamind.scripts.verify_broker_adapter
```

Equivalent forms:

```bash
uv run python scripts/verify_broker_adapter.py
```

CLI flags:

- `--mode {paper,live}` — Execution mode. Defaults to `paper`. Live mode
  requires `ALPACA_LIVE_KEY` + `ALPACA_LIVE_SECRET`. Live-mode-specific
  assertions DEFER per the design.
- `--phase NAME` — Run a single phase by name (one of `phase_1` …
  `phase_7`). Useful for re-running one phase after a fix without
  re-driving the others.
- `--verbose` — Print extra diagnostic detail (e.g. resolved venue tier
  + day-trade headroom in Phase 6).
- `--config-dir PATH` — Override the configuration directory (default:
  `./config`).
- `--db PATH` — Path to the SQLite DB phases 3/4/5 (equity / options /
  mleg lifecycles) write through. **Required for those phases to RUN**
  (otherwise they DEFER with a clear diagnostic). Set this to the tmp
  DB you just created in Prerequisites step 4 (e.g.
  `--db=/tmp/alphamind-verify-broker/alphamind.db`).

Exit code: `0` on full pass (DEFERRED counts as not-failing), `1` on
any phase failure.

Expected runtime under 2 minutes during a US market session. The bulk
of wall-clock is paper-mode fill latency in Phases 3 / 4 / 5 (typically
1–10 seconds per OPEN / CLOSE round-trip during US market hours).
Off-hours runs DEFER Phases 3 / 4 / 5 up front and finish in seconds
without submitting any orders.

## Expected output

A clean run prints (exit code 0):

```
======================================================================
AlphaMind Broker Adapter Verification
======================================================================
  [PASS] Phase 1 — Adapter substrate
  [PASS] Phase 2 — Account state queries
  [PASS] Phase 3 — Equity order lifecycle
  [PASS] Phase 4 — Options order lifecycle
  [PASS] Phase 5 — Mleg strategy lifecycle
  [PASS] Phase 6 — Venue config in isolation
  [PASS] Phase 7 — Disconnect recovery
======================================================================
RESULT: PASS (7 pass)
======================================================================
```

On any phase failure the script exits 1, the failed phase prints
`[FAIL]` followed by an indented diagnostic line (e.g. `get_account
raised: APIError(403): account locked`), and the summary reads
`RESULT: FAIL (N pass, ..., M fail)` with the failing phase labels
listed underneath.

DEFERRED phases (e.g. live-mode-only assertions in paper mode) render
as `[DEFERRED] Phase N — <label>` with the deferral rationale on the
indented line, and count as not-failing for exit-code purposes.

## Failure-mode triage

| Phase | Typical failure | Diagnostic | Likely fix |
|---|---|---|---|
| (boot) | `Alpaca paper credentials not set: environment variable(s) ['ALPACA_PAPER_KEY'] are unset or empty` | The factory could not resolve credentials from the environment. | Source `.env` per Prerequisites step 2; confirm with `echo $ALPACA_PAPER_KEY`. |
| (boot) | `Alpaca live credentials not set: environment variable(s) ['ALPACA_LIVE_KEY'] are unset or empty` | Operator passed `--mode=live` without the live env vars. | Either drop `--mode=live` (paper is the default) or set the live vars. Story 05 / ALP-393 ships paper-only verification. |
| (boot) | `configuration load failed: …` | `venue.yaml` / `execution.yaml` failed Pydantic validation. | Read the diagnostic; confirm the config files match the published schemas. The most common failure is a malformed env-var name (must match `^[A-Z][A-Z0-9_]*$`). |
| 1 — Adapter substrate | `factory failed to build clients: …` | `AlpacaClientFactory.build_trading_client()` raised. | Inspect the wrapped exception; commonly an invalid REST URL in `venue.yaml` or a missing alpaca-py dependency. |
| 1 — Adapter substrate | `submit_with_retry first-try success returned … expected Submitted` | The retry helper's first-try success path regressed. | Re-read `retry.py § submit_with_retry`. The first-try outcome must be a `Submitted` record with `attempt_count=1`. |
| 1 — Adapter substrate | `submit_with_retry transient-retry attempt_count=N, expected 3` | The classifier no longer treats `OSError` as transient, OR the retry loop is bailing too early. | Re-read `errors.py § is_transient` (network errors should fall through `_http_status` returning `None` → transient → retry). |
| 1 — Adapter substrate | `classify_alpaca_error(403, …) returned … expected code='insufficient_buying_power'` | The 4xx classification table drifted from the documented `broker-adapter.md § Order submission` rejection reasons. | Re-read `errors.py § _FOUR_OH_THREE_RULES` and align with the design doc. |
| 2 — Account state queries | `get_account raised: APIError(403): …` | The paper account is locked or the credentials are wrong. | Check the Alpaca dashboard for the paper account's status; rotate keys if needed. Most often: credentials sourced from `.env` are stale after a `claude setup-token` rotation. |
| 2 — Account state queries | `get_asset('NVDA') returned None — NVDA should be in Alpaca's universe` | Alpaca's universe shrunk (improbable) OR the queries wrapper's 404-mapping regressed. | Re-read `queries.py § get_asset` — the 404 → None mapping is the documented contract for unknown symbols, not for known symbols. |
| 2 — Account state queries | `get_calendar returned N entries, expected ≥ 20 over 30 days` | Calendar fetch returned fewer business days than US market reality has in 30 calendar days. | Re-read `queries.py § get_calendar`; commonly the start/end filtering regressed and the wrapper returned only a single day. |
| 2 — Account state queries | `get_clock returned incomplete record: …` | The clock SDK call returned a partially-populated record. | Inspect the alpaca-py response for an SDK regression; this is rare in practice. |
| 2 — Account state queries | `get_orders pagination raised: …` | The pagination cursor logic raised mid-iteration. | Re-read `queries.py § get_orders` — the cursor advances via the oldest order's `submitted_at`; an empty page exits the loop. |
| 3 — Equity order lifecycle | `submit_equity_open returned GatewaySubmissionFailed: …` | The retry window expired without a successful submission. | Inspect the diagnostic; commonly a network blip or paper-API 5xx. Re-run; persistent failures suggest paper-API outage. |
| 3 — Equity order lifecycle | Submission rejected with `code='insufficient_buying_power'` | Paper account's buying power is too low for the verify's order size. | Reset the paper account from the Alpaca dashboard or shrink the verify's order size temporarily. |
| 3 — Equity order lifecycle | `[DEFERRED] Market closed (US session 09:30-16:00 ET); …` | Off-hours run — the lifecycle defer-gates on `MarketClock.is_open` because off-session fills don't arrive within the 30s wait budget. | Re-run during US market session (09:30-16:00 ET). |
| 3 — Equity order lifecycle | Fill never arrives within the timeout | Even during session, fill latency on a degraded paper venue can exceed the verify's 30s wait budget. | Confirm the paper venue is healthy via the Alpaca status page; re-run. Persistent failures suggest paper-API outage. |
| 4 — Options order lifecycle | `[DEFERRED] Market closed (US session 09:30-16:00 ET); …` | Off-hours run; same defer-gate as Phase 3. | Re-run during US market session. |
| 4 — Options order lifecycle | `[DEFERRED] No listed call contracts on NVDA for any business day in the next 60 days; …` | Every candidate expiration in the search window returned an empty chain — most likely an Alpaca paper-environment data gap. | Re-try in a few minutes; if it persists, switch the verify underlying to another optionable name with a known-active chain. |
| 4 — Options order lifecycle | Permanent rejection `code='options_level_not_approved'` | The paper account has no options-trading approval. | Enable options trading on the Alpaca paper account from the dashboard's Account Settings. |
| 4 — Options order lifecycle | Permanent rejection `code='contract_expired'` | The chain query returned a contract whose expiration is somehow already past. | Investigate Phase 2's `get_calendar` (the calendar drives candidate dates) and `queries.get_option_contracts` (the chain enumerator). |
| 5 — Mleg strategy lifecycle | `[DEFERRED] No expiration on NVDA in the next 60 days has ≥ 2 listed call strikes; …` | Every candidate expiration's chain has < 2 strikes — too sparse to build a vertical spread. | Re-try later or switch underlying. See Phase 4's "no listed contracts" row above. |
| 5 — Mleg strategy lifecycle | Permanent rejection `code='invalid_legs'` | The mleg request's leg structure violates Alpaca's constraints (leg count outside [2,4], ratios not GCD=1, mixed underlyings). | Re-read `order_mleg.py § _validate_alpaca_constraints` and `_simplify_ratios`; the verify's vertical-spread fixture should produce 2 legs with ratios (1, 1). |
| 5 — Mleg strategy lifecycle | Position transitions to OPEN before the last leg fills | The Phase 1 fill-integration logic for strategies regressed. | Re-read `state_persistence/write_paths/phase1_strategy.py`; the documented contract is "PENDING → OPEN only at the last-leg fill". |
| 6 — Venue config | `venue constant SETTLEMENT_DAYS_BY_INSTRUMENT[equity] = N, expected 1` | A constant in `venue_configuration/constants.py` drifted from the documented value. | Re-read `venue-configuration.md § Settlement` and align the constant. T+1 settlement landed in the SEC rule effective 2024-05-28. |
| 6 — Venue config | `is_market_open(now)=True disagrees with get_clock().is_open=False` | The local calendar cache and the live clock disagree. | Most often a stale cache — the cache TTL is 24 hours; force a refresh by re-instantiating the cache or wait for TTL expiry. Sustained disagreement suggests Alpaca's `/v2/clock` and `/v2/calendar` are out of sync (rare). |
| 6 — Venue config | `business_day_offset(today, 1) returned <weekend date>` | The cache's business-day offset logic mis-skipped weekends. | Re-read `calendar_cache.py § business_day_offset`; the loop should only count days present in `_days` (sessions Alpaca returned). |
| 6 — Venue config | `compute_settlement_date(...) disagrees with business_day_offset(effective_trade_date, 1)` | The settlement calculator's roll-forward-to-business-day logic regressed. | Re-read `settlement.py § compute_settlement_date`; the documented contract is "roll trade_date forward to next business day, then offset by SETTLEMENT_DAYS_BY_INSTRUMENT[instrument_class]". |
| 7 — Disconnect recovery | `recover_missed_fills_since raised: …` | The recovery primitive failed mid-iteration. | Inspect the wrapped exception; commonly a `get_orders` cursor failure (see Phase 2 triage row). |

## Operational caveats

**The script exits before live drives if credentials are missing.** The
boot path validates env vars via `AlpacaClientFactory` before invoking
any phase function. A missing-credential failure surfaces as the
naming `RuntimeError` from the factory, with the script returning 1
before any network call.

**Phases 3 / 4 / 5 defer when the market is closed.** Each lifecycle
phase reads `MarketClock.is_open` after the `--db` gate; if the market
is closed it DEFERs with `Market closed (US session 09:30-16:00 ET);
re-run during a session for low-latency paper fills.` rather than
submitting orders that can't fill within the per-phase wait budget
(equity 30s, options 60s, mleg 120s). Schedule the operator run for a
US market day, ideally mid-session.

**Phases 4 / 5 pick option strikes from the live chain.** The script
queries `get_option_contracts` for each near-term business-day
candidate until it finds an expiration with a non-empty active call
chain, then picks the median listed strike (Phase 4) or median + next
listed strike up (Phase 5). No strike is hardcoded — the run is
resilient to expirations whose listed strike sets shift between days.
DEFERs cleanly if no expiration in the 60-day search window has any
listed contracts.

**Phase 6 reads the live `/v2/clock` and `/v2/calendar`.** A network
partition during Phase 6 surfaces as `calendar/clock interaction
raised: <error>`. Re-run after the partition resolves.

**Phase 7's translator branch is unit-tested elsewhere.** The
end-to-end live drive in Phase 7 only proves the recovery primitive
runs without raising over a 1-hour lookback. Coverage of every
`OrderSnapshot.status` → `FillReport.event_type` mapping lives in
`tests/execution/broker_adapter/test_recovery.py`.

**Idempotency.** Running the verify twice in a row against the same
fresh paper-mode tmp DB produces identical PASS / DEFERRED summaries
(the acceptance-criteria idempotency requirement). Phase 3 / 4 / 5
will accumulate paper-account positions across runs; reset the paper
account between operator dry-runs if the position roster matters.

**Verify script does not pre-stamp wire-side fields.** Per the
orchestrator anti-pattern catalog, a verify script that fakes a
missing wire (e.g. pre-stamps `position_opened.timestamp` because the
producer doesn't write it) is a yellow flag. This script does not
fake any wire — every assertion reads what the production code path
actually wrote. If a phase fails because a producer didn't emit
something it should have, the fix is the producer, not the verify.

**Broker-roundtrip scope only — no OMS persistence integration.**
Phases 3 / 4 / 5 dispatch directly through `dispatch_command_to_broker`
and assert the round-trip completed at the broker (OPEN order
acknowledged → fill arrives → CLOSE order acknowledged → exit fill
arrives). They do **not** thread an `InvocationHandle` through the
OMS Phase 2 writeback machinery — the `--db` arg is a tmp DB the
phases require for the broker dispatcher's own preconditions, not a
target the verify writes a full OMS-side activity-log trail into. End-
to-end coverage of the OMS's `persist_envelope_outcome` integration
with real broker submissions is e2e-harness territory
(`scripts/RUNBOOK_end_to_end_verification.md` /
`verify_state_persistence.py`); this verify proves only the wiring
layer beneath it.

## References

- `scripts/RUNBOOK_end_to_end_verification.md` — central e2e runbook;
  this script runs as the broker-adapter phase between State persistence
  and the not-yet-existing continuous-monitor / decision-layer phases.
- `scripts/verify_state_persistence.py` + `RUNBOOK_state_persistence.md`
  — predecessor; the durable substrate this script's Phase 3 / 4 / 5
  write through to.
- `scripts/verify_oms_commands.py` + `RUNBOOK_oms_commands.md` —
  predecessor; the OMS commands surface this script exercises end-to-end
  via the OMS engine-stub coordinated swap (story 03e / ALP-390).
- `docs/design/05-execution-layer/broker-adapter.md` — the contract
  this verify covers (the entire document).
- `docs/design/05-execution-layer/venue-configuration.md` — the venue
  configuration contract Phase 6 verifies.
- ALP-121 parent issue — broker-adapter work-tree overview, dependency
  graph, pre-resolved decisions.
- ALP-378 through ALP-392 — predecessor stories whose deliverables this
  script integrates.
- `src/alphamind/execution/broker_adapter/` — the broker adapter
  package the verify exercises.
- `src/alphamind/execution/venue_configuration/` — the venue
  configuration package Phase 6 exercises.
- `src/alphamind/execution/oms/broker_dispatch.py` — the OMS-to-broker
  dispatcher Phases 3 / 4 / 5 drive end-to-end.
- `tests/scripts/test_verify_broker_adapter.py` — unit tests for this
  script's phase functions; run with
  `uv run pytest tests/scripts/test_verify_broker_adapter.py -n auto`.
