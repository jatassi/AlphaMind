# Asset universe validation

How the asset universe is verified, persisted as machine-readable config, and updated over time. The selection criteria in [asset-universe.md](asset-universe.md) define what makes a ticker eligible (liquidity, coverage, beta, market cap, options chain). This doc makes those criteria operationally precise — what data backs each criterion, what threshold passes, how often the universe is re-evaluated, and how the operator adds or removes a ticker.

The companion [asset-universe.md](asset-universe.md) is descriptive (sector composition, rationale per sector, selection criteria as written prose). This doc is procedural (concrete measurement specifications, the YAML schema, and the operator workflow).

---

## Scope

**In scope.** Each criterion that gates a ticker's inclusion in the universe; the configuration file that holds the resolved ticker list; the validation procedure the operator runs to verify each ticker against the criteria; the cadence at which validation re-runs; the procedure for adding a candidate or removing a failing ticker, including held-position interaction.

**Out of scope** (owned by other docs, not duplicated here):

| Concern | Authoritative spec |
|---|---|
| Sector composition and selection rationale | [asset-universe.md](asset-universe.md) |
| Master sector list (the four sector names) | The `sectors:` keys in `config/assets.yaml` (this file) |
| Per-profile sector activation (`active_sectors`) | [profiles/{profile}.yaml](configuration-management.md#profilesmediumyaml), [rules-and-limits.md § Dual portfolio profiles](06-risk-guardrails/rules-and-limits.md#dual-portfolio-profiles) |
| Per-ticker data ingestion at invocation time | [01-data-layer/README.md](01-data-layer/README.md), [external/quantitative.md](01-data-layer/external/quantitative.md) |
| Adding or removing a sector wholesale | A Phase 4 universe-scope question, not a validation question |

---

## Where the universe lives

The authoritative ticker list is `config/assets.yaml`. Reloaded at the start of every invocation per [configuration-management.md § Reload model](configuration-management.md#reload-model), like every other YAML file under `config/`. Operator edits land at the next scheduled trigger.

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

The `discovery_sources` block names a sector ETF per sector to drive [Candidate discovery](#candidate-discovery). Each entry specifies the ETF ticker, the vendor (`spdr` or `ishares`), and — for iShares — the product ID encoded in the holdings-CSV download URL.

The universe is universe-wide — no per-profile composition. A profile selects which sectors are active via `active_sectors`; within an active sector, every ticker listed in `assets.yaml` is in scope. The dual portfolio profiles ([rules-and-limits.md § Dual portfolio profiles](06-risk-guardrails/rules-and-limits.md#dual-portfolio-profiles)) differ in capital and feature flags, not in which tickers exist.

`last_full_validation` records the calendar date the validation procedure last ran against every ticker and every ticker passed. Per-ticker history (when a name was added, when a single name was re-validated mid-cycle) lives in git, not in the YAML.

---

## Validation criteria

Five criteria gate inclusion. All thresholds are universe-wide. The five criteria apply to `sectors[*]` entries only — `benchmarks` are reference instruments for cross-asset distillation and are not subject to the selection criteria.

### Average daily volume

| Field | Value |
|---|---|
| Data source | Polygon `/v2/aggs/ticker/{ticker}/range/1/day/...` |
| Lookback | 60 trading days ending on the validation date |
| Computation | `mean(volume)` and `mean(volume * close)` over the window |
| Threshold | `mean(volume) > 2,000,000` shares OR `mean(volume * close) > $50,000,000` notional |
| Rationale | Either condition satisfies fill realism for the position sizes the system uses. The OR allows lower-priced names with sufficient share volume and higher-priced names with sufficient notional to qualify on the same rule. Sixty trading days smooths through earnings spikes and quiet weeks while remaining responsive to a structural change in liquidity. |

### Analyst coverage

| Field | Value |
|---|---|
| Data source | Finnhub `/api/v1/stock/recommendation?symbol={ticker}` |
| Lookback | Most recent month-end snapshot |
| Computation | `sum(strongBuy + buy + hold + sell + strongSell)` |
| Threshold | `total >= 10` |
| Rationale | Ten or more analysts ensures news density, earnings-estimate breadth, and revision flow sufficient for the analyst agent to find context. Below ten, sell-side coverage thins enough that a single house's view dominates the consensus signal — undesirable for an information-synthesis edge that depends on disagreement and revision flow. |

### Beta

| Field | Value |
|---|---|
| Data source | Polygon daily bars for `{ticker}` and SPY |
| Lookback | 90 trading days ending on the validation date |
| Computation | `cov(return_ticker, return_spy) / var(return_spy)` over the window, returns computed close-to-close |
| Threshold | `abs(beta) >= 0.6` |
| Rationale | The criterion's purpose is tradable intra-window dispersion at the system's 4–72h horizon, which is what absolute beta measures. Signed beta conflates dispersion magnitude with correlation direction — a name at β = −0.7 has the same tradable dispersion as one at +0.7, just moving opposite SPY, and is arguably more useful for a swing system (independent catalyst structure, hedge value). The 0.6 absolute floor drops names whose returns are genuinely small in either direction (typical of low-vol infrastructure and utility-like names) while admitting negatively-correlated names that carry their own catalyst structure (energy on commodity dynamics, defensive plays). |

### Market capitalization

| Field | Value |
|---|---|
| Data source | Polygon `/v3/reference/tickers/{ticker}` `market_cap` field |
| Lookback | None — point-in-time |
| Threshold | `market_cap >= $10,000,000,000` |
| Rationale | Ten-billion-dollar floor establishes institutional coverage density and absorbs the system's position sizes without per-trade market impact. The floor also filters small-caps where information environments are thinner and idiosyncratic news (single executive, single product line) dominates the signal. |

### Options chain liquidity

| Field | Value |
|---|---|
| Data source | Polygon `/v3/snapshot/options/{ticker}` |
| Reference expiry | The nearest standard monthly expiry within 45 calendar days of the validation date |
| Computation | Sum of open interest across all contracts (calls + puts) within ±10% of spot at the reference expiry |
| Threshold | `total_oi >= 5,000` contracts |
| Rationale | Five thousand near-the-money OI carries the options-flow signals (category 3 in [external/quantitative.md](01-data-layer/external/quantitative.md)) above the noise floor that single-counterparty hedging activity creates on sparse chains. The same threshold also implies tradable liquidity for the options-enabled profiles, sized for the position quantities those profiles allow. |

---

## Validation procedure

An offline operator workflow run on the cadence below. Per ticker in `config/assets.yaml`:

1. Operator pulls the data sources named in [Validation criteria](#validation-criteria).
2. Operator computes each criterion's value.
3. Operator compares each value against its threshold.
4. Operator records pass/fail per criterion with the computed value.

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

The report is the only output. The operator reads it and decides which failures warrant a YAML edit, which mirrors the operator-driven Class A review pattern in [02-distillation-layer/threshold-calibration.md § Update process](02-distillation-layer/threshold-calibration.md#update-process) — automated computation paired with manual judgment.

A data-source failure for one ticker (Polygon returns no data, Finnhub returns an error) is reported as `unknown` for that criterion. The ticker carries forward unchanged in the universe pending a successful re-validation; the operator does not act on a missing measurement.

When every ticker has passed in a single run, the operator updates `last_full_validation` to that run's date in the same YAML edit that lands any add/remove decisions.

---

## Re-evaluation cadence

The default cadence is **monthly during paper trading** and **quarterly once live**, plus on-demand whenever a structured trigger fires.

**Structured triggers — re-validate immediately when any of these are observed:**

- A held position experiences sustained liquidity deterioration (sustained widening of the bid-ask spread or volume below 50% of recent baseline) — re-validate that single ticker.
- A ticker's index inclusion changes (e.g., dropped from S&P 500), which often correlates with structural changes in coverage and liquidity.
- A corporate event affects the eligibility of an existing ticker: merger close, spin-off completion, going-private transaction, bankruptcy filing.
- The feedback loop ([project-tracker.md § Phase 4](../project-tracker.md#phase-4--maturation-before-live-transition)) shows a sector with persistently weak thesis quality — re-validate that sector for whether its current names match the rest of the universe's information density.
- A regime jump to crisis or back to low-vol — beta and ADV characteristics shift enough to warrant a check, especially for tickers near a threshold edge.

The cadence is the floor; structured triggers add re-validation events on top.

---

## Add and remove process

**Adding a candidate.** The operator nominates a ticker (typically by sector — the natural ask is "we should have ABC in financials"), runs the validation procedure against the candidate, and reads the report. If all five criteria pass, the operator edits `assets.yaml` to add the ticker to the appropriate sector array. The change takes effect at the next invocation reload.

**Removing a failing ticker that the system does not hold.** The operator deletes the ticker from `assets.yaml`. The change takes effect at the next reload, and downstream data ingestion stops scoping that ticker.

**Removing a failing ticker that the system holds.** Held positions complicate removal. The operator chooses between two paths based on the failing criterion:

- **Close-first.** When the failing criterion materially affects the trade's risk (e.g., liquidity deterioration that widens exit slippage, a market-cap drop that signals structural deterioration), the operator closes the position via the strategist or by direct intervention, then removes the ticker.
- **Carry-to-exit.** When the failure is a drift below threshold without immediate risk implication (e.g., beta dropped from 0.85 to 0.75 due to a calm regime), the operator can leave the ticker in place until the position closes naturally, then remove it.

The operator's calibration log records the decision and the rationale, alongside the YAML diff. The single source of truth for what is in scope is `assets.yaml`.

Sector-level edits (adding healthcare, dropping energy) are out of scope here — they reshape the `sectors:` keys in `assets.yaml`, the per-profile `active_sectors` arrays, the analysis-layer domain researcher catalog, and the prompts those researchers run.

---

## Candidate discovery

The validation procedure above is unidirectional — it surfaces names in the universe that have stopped qualifying. Candidate discovery is the inverse: it surfaces names *outside* the universe that *would* qualify if added. Run on a slower cadence than re-validation (operator-driven, typically alongside or following a regular validation cycle).

**Pool definition.** Each sector maps to a canonical sector ETF in `discovery_sources`. The ETF's current holdings define the discovery pool for that sector. The four defaults — XLK (SPDR Technology Select Sector), SOXX (iShares Semiconductor), XLF (SPDR Financial Select Sector), XLE (SPDR Energy Select Sector) — were chosen because each is the broadly-recognized index proxy for its sector, and their issuers publish daily holdings CSV/XLSX downloads at stable URLs. Holdings drift over time as the index rebalances; the pool reflects the index's view of sector membership at the time of the discovery run.

**Vendor support.** `vendor: spdr` resolves to the State Street XLSX endpoint; `vendor: ishares` resolves to the iShares CSV endpoint with the `ishares_product_id` encoded in the URL. The script's holdings parsers reject non-equity rows (cash, currency forwards, futures, legal disclaimer text) by ticker-shape regex and — for iShares — by the `Asset Class` column.

**Procedure.** For each sector with a `discovery_sources` entry:

1. Fetch the ETF's current holdings.
2. Filter out tickers already in the universe (across *all* sectors — a single sector ETF may overlap multiple universe sectors; e.g., XLK includes semiconductor names that we partition into our `semis` sector).
3. Run the five validation criteria against each remaining candidate.
4. Surface the passing candidates as add candidates; report the failing candidates as a one-line summary so the operator can see what almost qualified and why.

**Output is informational.** Discovery emits a report keyed by sector. The operator reviews passing candidates and decides which to add via the standard [Adding a candidate](#add-and-remove-process) procedure. The script never edits `assets.yaml`.

**When to run.** Quarterly during paper trading (alongside the regular re-validation), or whenever an [structured trigger](#re-evaluation-cadence) indicates the universe should be reconsidered as a whole. Discovery is heavier than validation (155+ candidates × 5 criteria), so it isn't a per-invocation operation.

---

## Validation invariants

Run as part of the configuration-management cross-reference and semantic self-test layers ([configuration-management.md § Validation](configuration-management.md#validation)) when `assets.yaml` is loaded. Failure aborts the invocation and alerts the operator.

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

The invariants confirm the file is well-formed. They do not re-run the five validation criteria at config load — those are validated by the offline procedure on the cadence above.

---

## Cross-references

- Sector composition and selection rationale: [asset-universe.md](asset-universe.md)
- Per-profile sector activation: [06-risk-guardrails/rules-and-limits.md § Dual portfolio profiles](06-risk-guardrails/rules-and-limits.md#dual-portfolio-profiles), [configuration-management.md § profiles/{profile}.yaml](configuration-management.md#profilesmediumyaml)
- Where `assets.yaml` reloads: [configuration-management.md § Reload model](configuration-management.md#reload-model)
- Data sources used by the validation procedure: [01-data-layer/external/quantitative.md](01-data-layer/external/quantitative.md), [01-data-layer/api-key-checklist.md](01-data-layer/api-key-checklist.md)
- Analogous operator-driven calibration pattern: [02-distillation-layer/threshold-calibration.md § Update process](02-distillation-layer/threshold-calibration.md#update-process)
- Phase 4 feedback loop (when shipped): [project-tracker.md § Phase 4](../project-tracker.md#phase-4--maturation-before-live-transition)
