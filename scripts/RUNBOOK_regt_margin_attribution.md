# Reg T Margin Attribution Verification Runbook

Operator workflow for the ALP-430 verification artifact that proves the Reg T
margin attribution work tree (ALP-126) is internally consistent: the Phase 1
wedge populates each fill's `regt_attribution_json` with eight finite fields,
the headline algebra
(`regt_excess_over_pm == regt_marginal_consumption − pm_marginal_consumption`)
holds within `1e-6`, and the snapshot assembler's Step 11 trailing-30d
aggregate equals the sum of the per-fill excess values.

Run after any change to `src/alphamind/execution/regt_margin_attribution/`,
`src/alphamind/execution/state_persistence/write_paths/phase1.py` (specifically
the per-fill attribution wedge added by ALP-428), or
`src/alphamind/portfolio_state/assembler.py` Step 11 enrichment.

## Purpose

`scripts/verify_regt_margin_attribution.py` is a pure in-process integration
check against a freshly-migrated SQLite DB. No SDK calls; no live broker
contact. Sub-second runtime.

What "PASS" tells the operator: the per-fill Reg T attribution math, its
JSON persistence path, the trailing-window aggregation, and the assembler's
delivery surface (`CashLedger.regt_excess_trailing_*`) are wired correctly
end-to-end on this codebase head.

The script seeds a representative four-position portfolio (long NVDA equity,
short AMD equity, long SPY call, NVDA bull-call-spread strategy) so the
pre-fill Reg T and PM-equivalent margins are non-trivial; then seeds two
unprocessed fills targeting separate scaffolded orders (a BUY entry fill on a
PENDING NVDA long position, and a SELL partial-exit fill on an OPEN AMD long
position); drives `process_unprocessed_fills` inside an `InvocationContext`;
rehydrates the per-fill `RegTMarginAttribution` records; invokes the snapshot
assembler so its Step 11 enrichment lands on `CashLedger`; and asserts the
three documented invariants.

The AMD-side fill is a partial exit on a long sub-position rather than a
`SELL_TO_OPEN` against the seeded AMD short because Phase 1 raises
`NotImplementedError` on SHORT-side entry fills (per
`RUNBOOK_state_persistence.md § Operational caveats`). The seeded portfolio
still includes the AMD short so the pre-fill margin state covers both directions.

## Prerequisites

1. **`.env` loaded.** Run `source .env` (or the equivalent shell hook) before
   invoking; the script reads no secrets directly but
   `load_regt_margin_attribution_config()` resolves
   `config/regt_margin_attribution.yaml` from the repository root, and the
   default `make_engine(...)` resolution chain consults `DATABASE_PATH` if set.
2. **`uv sync` completed** — `uv run` is the entry point.
3. **Python `>=3.13`** — matches the project's declared minimum.
4. The work tree's integration branch (`jackson/alp-126-reg-t-margin-attribution`)
   must be reachable from HEAD (all six predecessor stories landed:
   ALP-421 through ALP-429).
5. A freshly-migrated SQLite DB. The script creates one in a
   `tempfile.mkdtemp` directory if `--db-path` is omitted. For a stable
   on-disk DB at a known location:

   ```python
   from pathlib import Path
   import alphamind.execution.state_persistence.tables  # noqa: F401
   from alphamind.persistence.models import Base
   from alphamind.persistence.session import make_engine

   db_path = Path("/tmp/regt_verify.db")
   engine = make_engine(str(db_path))
   Base.metadata.create_all(engine)
   engine.dispose()
   ```

## Invocation

```bash
source .env
uv run python scripts/verify_regt_margin_attribution.py \
    --db-path /tmp/regt_verify.db \
    --invocation-id verify-regt-001
```

CLI flags:

- `--db-path PATH` — Path to a freshly-migrated SQLite DB. When omitted, the
  script creates a temp directory and migrates a fresh DB to head.
- `--invocation-id ID` — Invocation id stamped on the seeded Phase 1 run.
  Defaults to `verify-regt-001`.
- `--verbose` — Print the resolved DB path and `pm_model_version` /
  `risk_free_rate_annual` from `config/regt_margin_attribution.yaml` before
  running.

Exit code: `0` on PASS, `1` on FAIL.

## Expected output

A clean run prints (exit code 0):

```
======================================================================
AlphaMind Reg T Margin Attribution Verification
======================================================================
Per-fill RegTMarginAttribution:
  Fill verify-regt-fill-nvda-buy:
      regt_margin_before          =     <numeric>
      regt_margin_after           =     <numeric>
      regt_marginal_consumption   =     <numeric>
      pm_equivalent_before        =     <numeric>
      pm_equivalent_after         =     <numeric>
      pm_marginal_consumption     =     <numeric>
      regt_excess_over_pm         =     <numeric>
      pm_model_version            = occ_tims_v1_2026Q2
  Fill verify-regt-fill-amd-sell:
      regt_margin_before          =     <numeric>
      regt_margin_after           =     <numeric>
      regt_marginal_consumption   =     <numeric>
      pm_equivalent_before        =     <numeric>
      pm_equivalent_after         =     <numeric>
      pm_marginal_consumption     =     <numeric>
      regt_excess_over_pm         =     <numeric>
      pm_model_version            = occ_tims_v1_2026Q2
----------------------------------------------------------------------
Trailing-window aggregates (from assembled CashLedger):
  regt_excess_trailing_30d_usd =     <numeric>
  regt_excess_trailing_90d_usd =     <numeric>
  regt_excess_lifetime_usd     =     <numeric>
======================================================================
PASS
======================================================================
```

On any failure, the script exits 1, the trailer reads `FAIL`, and each
failing assertion appears on its own `- ` line naming the failure mode in
the triage-table vocabulary below.

## Failure-mode triage

| Failure message | Root cause | Likely fix |
|---|---|---|
| `IvLookupError: no IV available for <UNDERLYING> ...` raised during Phase 1 | One of the option-leg `(strike, expiration)` combinations is missing from the seeded `FixtureIvProvider` surface. The spread legs use strikes 900 / 950 against expiration 2026-08-21; the SPY call uses 545 against 2026-07-17. The PM-equivalent stress revaluation also probes neighbouring strikes on the shock grid. | Extend `_build_iv_provider()` with the missing strike/expiration entry, OR add a realized-vol fallback for the underlying. |
| `KeyError: '<SYMBOL>'` raised during Phase 1 | A position's underlying is missing from `MarketInputs.underlying_prices`. The seeded portfolio's underlyings are NVDA, AMD, SPY; verify each is in `_PRICES_USD`. | Extend `_PRICES_USD` with the missing entry; the price provider passed to the assembler reads the same map. |
| `regt_attribution_json IS NULL` on a processed fill | The Phase 1 wedge regressed — `process_unprocessed_fills` no longer threads `compute_attribution`'s result into the fill row before the surrounding context commits. | Re-read `src/alphamind/execution/state_persistence/write_paths/phase1.py § process_unprocessed_fills`; confirm the `row.regt_attribution_json = attribution.model_dump_json()` assignment is in place inside the merged-events loop's `FillRecord` branch. |
| `expected 2 processed fills with regt_attribution_json, got N` | Either Phase 1 quarantined a fill (validation rejected a non-positive quantity — the fixtures use positive quantities so a regression in `_quarantine_invalid` is the suspect) or one of the scaffolded orders/positions/brackets was not seeded. | Inspect the `fill_records` table state under the test DB; cross-check `_quarantine_invalid` did not flag a fixture. |
| `attribution fields not all finite for fill_id=<id>` | One of the eight numeric attribution fields is NaN or +/-inf. Typical cause: a Black-Scholes call with degenerate inputs (zero IV, expiration-in-the-past) — the stress shock grid widens strikes by ±asset-class shock, so the IV surface must cover the widened range. | Confirm the IV surface returns positive IV for every strike in the shock-grid neighbourhood; if the PM-equivalent stress path fell back to realized-vol with a zero scalar, supply a realized-vol entry. |
| `regt_excess_over_pm algebra mismatch for fill_id=<id>: <a> != <b>` | `compute_attribution` returned a `regt_excess_over_pm` value that does not equal `regt_marginal_consumption - pm_marginal_consumption` — a regression in the orchestrator's straight-line step sequence. | Re-read `src/alphamind/execution/regt_margin_attribution/orchestrator.py § compute_attribution`; step 7 is the headline algebra and must match the eight straight-line steps documented in its docstring. |
| `trailing-30d aggregate <a> does not equal sum of per-fill regt_excess_over_pm <b>` | Either `SqlPortfolioStateRepository.get_regt_excess_aggregates` regressed (windowed sum no longer matches the `processing_timestamp` filter), OR the assembler's Step 11 enrichment is not wired to copy the three fields onto `CashLedger`. The 30-day cutoff is `now - 30d`; the verify script's `now` is sampled wall-time-near just after Phase 1 stamps `processing_timestamp`, so both fills' timestamps fall safely inside the window. | Inspect `src/alphamind/execution/state_persistence/repository/sql_repository.py § get_regt_excess_aggregates` for the `processing_timestamp >= cutoff_30d` filter; confirm `src/alphamind/portfolio_state/assembler.py § Step 11` copies the three fields onto the enriched `CashLedger`. |
| `pm_model_version` mismatch across fills or against the runbook expected value | The config file `config/regt_margin_attribution.yaml` was refreshed without bumping `pm_model_version`, OR `compute_attribution` returned a different version string than the loaded config. | Re-load the config; bump `pm_model_version` if the methodology snapshot changed; rerun. |

## Operational caveats

**Phase 1 SHORT-entry support.** Phase 1 raises `NotImplementedError` on
SHORT-side equity entry fills. The verify script models the AMD-side fill
as a partial exit on a separate long sub-position to keep the script's
golden path inside Phase 1's supported subset; the seeded AMD short
contributes to the pre-fill margin state but is not the target of either
unprocessed fill. The remaining narrowed path lands with the SHORT-equity
follow-up story.

**Always-on activation.** Per `regt-margin-attribution.md § Activation`,
the module is always-on in both paper and live mode. The verify script's
in-process golden path exercises the same code path used in production;
no paper/live branching applies.

**IV surface coverage.** The `FixtureIvProvider` in this script supplies IV
quotes for the seeded option legs' strikes plus a small neighbourhood on the
shock grid. The PM-equivalent stress revaluation widens the underlying by
±asset-class shock per `regt-margin-attribution.md § Per-class-group stress`;
strikes outside the fixture's coverage band fall back to realized-vol (and
raise `IvLookupError` if no realized-vol entry exists). The fixture's
neighbourhood is wide enough for the seeded portfolio's strikes; widening
the seeded fixture is the operator action if the IV-surface coverage
contract changes.

**No `MarketInputs` production wiring.** The verify script constructs a
`MarketInputs` inline; production wiring lives with the continuous-monitor
work tree (ALP-123). Wiring `MarketInputs` into a production scheduler is
out of scope here.

**Refresh contract.** When the IBKR shock-parameter table or the
methodology version changes, refresh `config/regt_margin_attribution.yaml`
and bump `pm_model_version`. Historical aggregates remain filterable by
version. The verify script reads the file at every invocation so
hot-reloading config is just a re-run.

## References

- `scripts/RUNBOOK_end_to_end_verification.md` — central e2e runbook; this
  script runs as the Reg T margin attribution phase after corporate-actions
  and before continuous-monitor.
- `scripts/RUNBOOK_state_persistence.md` — predecessor phase's runbook;
  documents the Phase 1 write-path the wedge piggybacks on, and the
  `NotImplementedError` boundary on SHORT-entry fills the verify script
  navigates around.
- `docs/design/05-execution-layer/regt-margin-attribution.md` — design doc
  the verification asserts conformance to.
- ALP-126 parent issue — work-tree overview, dependency graph,
  pre-resolved decisions, and § Surfacing conditions (used as the basis
  for the failure-mode triage table above).
- ALP-421 through ALP-429 — predecessor stories whose deliverables this
  script integrates.
- `src/alphamind/execution/regt_margin_attribution/` — the package the
  verification asserts is internally consistent.
- `src/alphamind/portfolio_state/assembler.py` § Step 11 — the
  trailing-window delivery surface this script reads back.
- `tests/scripts/test_verify_regt_margin_attribution.py` — unit tests for
  this script's phase functions; run with
  `uv run pytest tests/scripts/test_verify_regt_margin_attribution.py -n auto`.
