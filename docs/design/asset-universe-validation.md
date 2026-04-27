# Asset universe validation

How the asset universe is verified, persisted as machine-readable config, and updated over time. [asset-universe.md](asset-universe.md) defines eligibility criteria (liquidity, coverage, beta, market cap, options chain) descriptively. This doc makes them operationally precise — backing data per criterion, threshold values, re-evaluation cadence, operator add/remove workflow, YAML schema.

---

## Scope

**In scope.** Per-criterion measurement gates; the config file holding the resolved ticker list; validation procedure; re-evaluation cadence; add/remove procedure including held-position interaction.

**Out of scope** (owned elsewhere):

| Concern | Authoritative spec |
|---|---|
| Sector composition and selection rationale | [asset-universe.md](asset-universe.md) |
| Master sector list (the four sector names) | The `sectors:` keys in `config/assets.yaml` (this file) |
| Per-profile sector activation (`active_sectors`) | [profiles/{profile}.yaml](configuration-management.md#profilesmediumyaml), [rules-and-limits.md § Dual portfolio profiles](06-risk-guardrails/rules-and-limits.md#dual-portfolio-profiles) |
| Per-ticker data ingestion at invocation time | [01-data-layer/README.md](01-data-layer/README.md), [external/quantitative.md](01-data-layer/external/quantitative.md) |
| Adding or removing a sector wholesale | A Phase 4 universe-scope question, not a validation question |

---

## Where the universe lives

The authoritative ticker list is `config/assets.yaml`. Reloaded at every invocation start per [configuration-management.md § Reload model](configuration-management.md#reload-model). Operator edits land at the next scheduled trigger.

```yaml
last_full_validation: 2026-04-25

discovery_sources:
  tech:       { etf: XLK,  vendor: spdr }
  semis:      { etf: SOXX, vendor: ishares, ishares_product_id: "239705" }
  financials: { etf: XLF,  vendor: spdr }
  energy:     { etf: XLE,  vendor: spdr }

sectors:
  tech:        [AAPL, MSFT, GOOG, GOOGL, AMZN, META, TSLA, ORCL,
                PLTR, SNOW, SHOP, CRWD, PANW, DDOG, NET, ZS, COIN, XYZ]
  semis:       [NVDA, AMD, AVGO, TSM, MU, INTC, QCOM, MRVL,
                LRCX, AMAT, ASML]
  financials:  [JPM, BAC, GS, MS, C, WFC, SCHW, USB,
                V, MA, AXP, PYPL]
  energy:      [COP, EOG, DVN, OXY, SLB, BKR, LNG]

benchmarks:
  SPY:  { role: broad_market, description: "S&P 500 ETF" }
  QQQ:  { role: broad_market, description: "Nasdaq 100 ETF" }
  IWM:  { role: breadth,      description: "Russell 2000 ETF" }
  RSP:  { role: breadth,      description: "Equal-weight S&P 500" }
  XLK:  { role: sector_etf,   description: "Tech Select Sector SPDR" }
  # ...
  TLT:  { role: intermarket,  description: "20+ Year Treasury Bond ETF" }
  GLD:  { role: intermarket,  description: "SPDR Gold Shares" }
```

The `discovery_sources` block names a sector ETF per sector to drive [Candidate discovery](#candidate-discovery). Each entry specifies the ETF ticker, the vendor (`spdr` or `ishares`), and — for iShares — the product ID encoded in the holdings-CSV URL.

The universe is universe-wide. A profile selects which sectors are active via `active_sectors`; within an active sector, every ticker in `assets.yaml` is in scope. Dual portfolio profiles ([rules-and-limits.md § Dual portfolio profiles](06-risk-guardrails/rules-and-limits.md#dual-portfolio-profiles)) differ in capital and feature flags, not tickers.

`last_full_validation` records the calendar date validation last ran against every ticker and every ticker passed. Per-ticker history (when a name was added, when a single name was re-validated mid-cycle) lives in git.

---

## Validation criteria

Five criteria gate inclusion, all universe-wide. Apply to `sectors[*]` entries only — `benchmarks` are reference instruments for cross-asset distillation, not subject to selection criteria.

### Average daily volume

| Field | Value |
|---|---|
| Data source | Polygon `/v2/aggs/ticker/{ticker}/range/1/day/...` |
| Lookback | 60 trading days ending on the validation date |
| Computation | `mean(volume)` and `mean(volume * close)` over the window |
| Threshold | `mean(volume) > 2,000,000` shares OR `mean(volume * close) > $50,000,000` notional |
| Rationale | Either condition satisfies fill realism for the system's position sizes. The OR lets lower-priced names qualify on share volume and higher-priced names on notional. Sixty days smooths earnings spikes while remaining responsive to structural liquidity changes. |

### Analyst coverage

| Field | Value |
|---|---|
| Data source | Finnhub `/api/v1/stock/recommendation?symbol={ticker}` |
| Lookback | Most recent month-end snapshot |
| Computation | `sum(strongBuy + buy + hold + sell + strongSell)` |
| Threshold | `total >= 10` |
| Rationale | Ten-plus analysts ensures news density, earnings-estimate breadth, and revision flow sufficient for the analyst agent. Below ten, a single house's view dominates the consensus — undesirable for an information-synthesis edge that depends on disagreement and revision flow. |

### Beta

| Field | Value |
|---|---|
| Data source | Polygon daily bars for `{ticker}` and SPY |
| Lookback | 90 trading days ending on the validation date |
| Computation | `cov(return_ticker, return_spy) / var(return_spy)`, close-to-close returns |
| Threshold | `abs(beta) >= 0.6` |
| Rationale | The purpose is tradable intra-window dispersion at 4–72h, which absolute beta measures. β = −0.7 has the same tradable dispersion as +0.7 and is arguably more useful for a swing system (independent catalyst structure, hedge value). The 0.6 floor drops names with genuinely small returns in either direction (low-vol infrastructure, utility-like names) while admitting negatively-correlated names with their own catalyst structure (energy on commodities, defensives). |

### Market capitalization

| Field | Value |
|---|---|
| Data source | Polygon `/v3/reference/tickers/{ticker}` `market_cap` field |
| Lookback | None — point-in-time |
| Threshold | `market_cap >= $10,000,000,000` |
| Rationale | Ten-billion-dollar floor establishes institutional coverage density and absorbs the system's position sizes without per-trade impact. Filters small-caps where information environments are thin and idiosyncratic news (one executive, one product line) dominates. |

### Options chain liquidity

| Field | Value |
|---|---|
| Data source | Polygon `/v3/snapshot/options/{ticker}` |
| Reference expiry | Nearest standard monthly expiry within 45 calendar days of the validation date |
| Computation | Sum of open interest across all contracts (calls + puts) within ±10% of spot at the reference expiry |
| Threshold | `total_oi >= 5,000` contracts |
| Rationale | 5,000 NTM OI carries options-flow signals (category 3 in [external/quantitative.md](01-data-layer/external/quantitative.md)) above the noise floor of single-counterparty hedging on sparse chains. Implies tradable liquidity for options-enabled profiles at their position sizes. |

---

## Validation procedure

Offline operator workflow run on the cadence below. Per ticker in `config/assets.yaml`:

1. Pull the data sources named in [Validation criteria](#validation-criteria).
2. Compute each criterion's value.
3. Compare against its threshold.
4. Record pass/fail per criterion with the computed value.

The output is a per-ticker report with one row per criterion. Example:

```
AAPL  [pass]
  ADV:               39,847,000 shares  /  $7,243,000,000 notional   pass
  Analyst coverage:  47 analysts                                     pass
  Beta:              1.18                                            pass
  Market cap:        $3,540,000,000,000                              pass
  Options OI (NTM):  287,000 contracts                               pass

NFLX  [fail: beta]
  ADV:               4,127,000 shares   /  $2,820,000,000 notional   pass
  Analyst coverage:  42 analysts                                     pass
  Beta:              0.71                                            fail (< 0.80)
  Market cap:        $293,000,000,000                                pass
  Options OI (NTM):  118,000 contracts                               pass
```

The report is the only output. The operator reads it and decides which failures warrant a YAML edit — the operator-driven Class A review pattern from [02-distillation-layer/threshold-calibration.md § Update process](02-distillation-layer/threshold-calibration.md#update-process), automated computation plus manual judgment.

A data-source failure for one ticker (Polygon no data, Finnhub error) is reported as `unknown` for that criterion. The ticker carries forward unchanged pending successful re-validation; the operator does not act on a missing measurement.

When every ticker passes in a single run, the operator updates `last_full_validation` to that date in the same edit that lands any add/remove decisions.

---

## Re-evaluation cadence

Default: **monthly during paper trading**, **quarterly once live**. Plus on-demand whenever a structured trigger fires.

**Structured triggers — re-validate immediately:**

- A held position has sustained liquidity deterioration (widened spread or volume <50% of recent baseline) — re-validate that ticker.
- Ticker's index inclusion changes (e.g., dropped from S&P 500) — often correlates with coverage and liquidity changes.
- A corporate event affects eligibility: merger close, spin-off completion, going-private, bankruptcy.
- The feedback loop ([project-tracker.md § Phase 4](../project-tracker.md#phase-4--maturation-before-live-transition)) shows a sector with persistently weak thesis quality — re-validate that sector.
- A regime jump to crisis or back to low-vol — beta and ADV shift enough to warrant a check, especially near threshold edges.

Cadence is the floor; structured triggers stack on top.

---

## Add and remove process

**Adding a candidate.** Operator nominates a ticker (typically by sector — "we should have ABC in financials"), runs validation, reads the report. If all five pass, edit `assets.yaml` to add the ticker to the sector array. Effective at next invocation reload.

**Removing a failing ticker that the system does not hold.** Delete from `assets.yaml`. Effective at next reload; downstream data ingestion stops scoping that ticker.

**Removing a failing ticker the system holds.** Two paths based on the failing criterion:

- **Close-first.** When the failing criterion materially affects trade risk (liquidity deterioration widening exit slippage, market-cap drop signaling structural deterioration), close the position via the strategist or direct intervention, then remove.
- **Carry-to-exit.** When the failure is a drift without immediate risk implication (beta dropped from 0.85 to 0.75 in a calm regime), leave the ticker in place until the position closes naturally, then remove.

The operator's calibration log records the decision and rationale alongside the YAML diff. `assets.yaml` is the single source of truth for what is in scope.

Sector-level edits (adding healthcare, dropping energy) are out of scope — they reshape `sectors:` keys, per-profile `active_sectors`, the analysis-layer domain researcher catalog, and researcher prompts.

---

## Candidate discovery

The validation procedure surfaces names *in* the universe that stopped qualifying. Candidate discovery is the inverse: names *outside* the universe that *would* qualify. Operator-driven, slower cadence — typically alongside a regular validation cycle.

**Pool definition.** Each sector maps to a canonical sector ETF in `discovery_sources`. The ETF's current holdings define the discovery pool. The four defaults — XLK (SPDR Tech), SOXX (iShares Semis), XLF (SPDR Financials), XLE (SPDR Energy) — are the broadly-recognized index proxies, and their issuers publish daily holdings at stable URLs. Holdings drift as the index rebalances; the pool reflects sector membership at run time.

**Vendor support.** `vendor: spdr` resolves to the State Street XLSX endpoint; `vendor: ishares` resolves to the iShares CSV endpoint with `ishares_product_id` encoded in the URL. Holdings parsers reject non-equity rows (cash, currency forwards, futures, disclaimer text) by ticker-shape regex and, for iShares, by the `Asset Class` column.

**Procedure.** For each sector with a `discovery_sources` entry:

1. Fetch the ETF's holdings.
2. Filter out tickers already in the universe across *all* sectors — sector ETFs may overlap (e.g., XLK includes semiconductor names we partition into `semis`).
3. Run the five validation criteria against each remaining candidate.
4. Surface passing candidates; report failing candidates as a one-line summary so the operator sees what almost qualified.

**Output is informational.** Discovery emits a report keyed by sector. The operator reviews and adds via the standard [Adding a candidate](#add-and-remove-process) procedure. The script never edits `assets.yaml`.

**When to run.** Quarterly during paper trading (alongside re-validation), or on a [structured trigger](#re-evaluation-cadence). Heavier than validation (155+ candidates × 5 criteria) — not a per-invocation operation.

---

## Validation invariants

Run as part of the cross-reference and semantic self-test layers ([configuration-management.md § Validation](configuration-management.md#validation)) when `assets.yaml` loads. Failure aborts the invocation and alerts the operator.

| Invariant | Layer | Reason |
|---|---|---|
| Every ticker symbol matches `^[A-Z][A-Z0-9.]*$` | Parse-time | Symbol formatting is the only structurally valid form across data providers. |
| Every ticker symbol is unique across all `sectors[*]` AND `benchmarks` | Semantic | A ticker belongs to exactly one role for downstream attribution. |
| `last_full_validation`, if present, is a valid ISO calendar date no later than today | Parse-time + semantic | The field is optional: it is set only after a run in which every ticker passes (per [Validation procedure](#validation-procedure)), and absent otherwise. Future-dated validation is a structural error. |
| Every sector listed in any profile's `active_sectors` is a key in `assets.yaml`'s `sectors:` map | Cross-reference | An active sector must exist in the universe. |
| Every sector listed in any profile's `active_sectors` has at least one ticker | Semantic | An active sector with zero tickers produces empty per-sector data ingestion. |
| Every key in `discovery_sources`, if present, is also a key in `sectors:` | Cross-reference | A discovery source must correspond to an existing universe sector. |
| Every `discovery_sources[*].vendor` is in `{spdr, ishares}`; iShares entries carry an `ishares_product_id` | Parse-time | The fetcher dispatches on vendor and requires the product ID for iShares URLs. |
| Every benchmark `role`, if `benchmarks` is present, is in `{broad_market, breadth, sector_etf, intermarket}` | Parse-time | The role is consumed by the data-source layer to scope cross-asset queries. |

The invariants confirm the file is well-formed. They do not re-run the five validation criteria at config load — those are the offline procedure's job on the cadence above.

---

## Cross-references

- Sector composition and selection rationale: [asset-universe.md](asset-universe.md)
- Per-profile sector activation: [06-risk-guardrails/rules-and-limits.md § Dual portfolio profiles](06-risk-guardrails/rules-and-limits.md#dual-portfolio-profiles), [configuration-management.md § profiles/{profile}.yaml](configuration-management.md#profilesmediumyaml)
- Where `assets.yaml` reloads: [configuration-management.md § Reload model](configuration-management.md#reload-model)
- Data sources used by the validation procedure: [01-data-layer/external/quantitative.md](01-data-layer/external/quantitative.md), [01-data-layer/api-key-checklist.md](01-data-layer/api-key-checklist.md)
- Analogous operator-driven calibration pattern: [02-distillation-layer/threshold-calibration.md § Update process](02-distillation-layer/threshold-calibration.md#update-process)
- Phase 4 feedback loop (when shipped): [project-tracker.md § Phase 4](../project-tracker.md#phase-4--maturation-before-live-transition)
