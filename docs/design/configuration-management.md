# Configuration management

All operator-tunable values live in YAML files under a single `config/` tree; secrets in `.env`. The surface organizes around four named bundles — **profile**, **regime**, **mode**, **overlay** — with a flat tail of independent knobs (scheduler, data sources, agents, venue, execution, guardrail metadata, LLM failure policy) that don't fit a natural bundle. A resolver runs at invocation start, composes the active bundles, and produces a resolved-config snapshot agents and the engine consume.

## Principles

**YAML is the operator interface.** Operators edit YAML only. Pydantic models in the loader provide typed access and parse-time validation.

**Cascade the named bundles, keep the tail flat.** Profile, regime, mode, and overlay are named concepts in [rules-and-limits.md](06-risk-guardrails/rules-and-limits.md), [regime-adaptation.md](06-risk-guardrails/regime-adaptation.md), and [state-delivery.md](06-risk-guardrails/state-delivery.md). Bundling correlated knobs under these names gives typo-proof composition and review surface.

**Reload at invocation boundary.** Per [mid-pipeline-failure-handling.md](mid-pipeline-failure-handling.md)'s fresh-context principle, each invocation re-reads config at start. Operator edits land at the next trigger.

**Secrets never live in YAML.** API keys, OAuth tokens, and credentials resolve through `.env` at process start. YAML references secrets by environment-variable name only.

## Scope

### In config
Values an operator adjusts between deployments or invocations: scheduler cron expressions, data-source registry (provider, tier, freshness SLA, rate limits, retry shape), agent model assignments, token and latency budgets, prompt file paths, venue parameters (Alpaca URLs, session hours), execution behavior (greeks refresh cadence, delta buffer, paper-harness coefficients, submission retry window), the 17 guardrail rule definitions with per-profile base values and per-regime multipliers, per-profile feature flags, mode behavioral contracts, overlay parameters, LLM failure retry policy.

### In code
Contracts and decision trees specified authoritatively elsewhere and read from multiple callers: JSON Schemas for analyst, strategist, PM, OMS command, engine envelope, and the reference-ID taxonomy they define; the OMS command ID template `{invocation_id}.{envelope_id}.{command_ordinal}.{attempt_seq}` from [oms-command-ids.md](oms-command-ids.md); the per-rule breach-response decision tree (immediate-engine vs. deferred-to-PM) from [breach-behavior.md](06-risk-guardrails/breach-behavior.md); composition-resolver logic; schema validation; cascade-closure semantics.

### Runtime state (database or per-invocation, not config)
Portfolio positions, open orders, activity log, thesis registry, per-ticker sentiment baselines, lead-lag estimates, rolling P/L windows, current regime classification, halt/emergency state, calibration histories.

## File layout

```
config/
  main.yaml                              # composition root
  scheduler.yaml                         # APScheduler cron triggers
  data_sources.yaml                      # per-provider + per-category registry
  agents.yaml                            # per-agent LLM configuration
  venue.yaml                             # Alpaca venue parameters
  execution.yaml                         # execution-layer behavior knobs
  guardrails.yaml                        # rule registry (metadata only)
  distillation.yaml                      # distillation-layer thresholds and windows
  assets.yaml                            # per-sector ticker list and benchmarks
  llm_failure.yaml                       # per-failure retry policy
  profiles/
    micro.yaml | small.yaml | medium.yaml | large.yaml
  regimes/
    low-vol.yaml | normal.yaml | elevated.yaml | crisis.yaml
  modes/
    normal.yaml | halt.yaml
  overlays/
    pre-event.yaml | stress.yaml
.env                                     # secrets, referenced by name from YAML
```

### `main.yaml`
The composition root. Pins the operator-selected identity dimensions and declares paths.

```yaml
active_profile: medium                   # manual selection per rules-and-limits.md
execution_mode: paper                    # paper | live
paths:
  database: '%USERPROFILE%\AlphaMind\data\alphamind.db'
  logs: '%USERPROFILE%\AlphaMind\logs'
  archive: '%USERPROFILE%\AlphaMind\archive'
  prompts: prompts/
```

### `scheduler.yaml`
Full cron expressions for APScheduler. Overlap-dedup and `max_instances` are the scheduler's safety net.

```yaml
timezone: US/Eastern
max_instances: 1
overlap_dedup_lookback_minutes: 30
triggers:
  market_hours_rolling: "30 9,11,13,15 * * mon-fri"
  off_hours_rolling:    "0 0,4,8,20 * * mon-fri"
  pre_open:             "0 9 * * mon-fri"
  pre_close:            "30 15 * * mon-fri"
  weekend:              "0 */6 * * sat,sun"
```

### `data_sources.yaml`
Per-provider entries (rate limit, retry shape, secret reference) and per-category entries (criticality tier, freshness SLA, primary vendor, failover vendors).

```yaml
providers:
  polygon:
    api_key_env: POLYGON_API_KEY
    rate_limit_per_minute: 100
    retry_shape: critical                # references retry_shapes below
  fred:
    api_key_env: FRED_API_KEY
    rate_limit_per_minute: 120
    retry_shape: critical
  marketaux:
    api_key_env: MARKETAUX_API_KEY
    rate_limit_per_day: 100
    retry_shape: optional

retry_shapes:
  critical:   { attempts: 3, backoff: exponential, failover: true  }
  important:  { attempts: 2, backoff: exponential, failover: false }
  optional:   { attempts: 1, backoff: none,        failover: false }

categories:
  q1_price_volume:
    tier: critical
    freshness_max_seconds: 300
    primary: polygon
    failover: [alpaca]
  qual2_social:
    tier: optional
    freshness_max_seconds: null
    primary: stocktwits
    failover: []
```

### `agents.yaml`
Per-agent configuration. The tool allowlist is a list of tool names that must resolve to registered tools in code.

```yaml
agents:
  analyst:
    model: claude-opus-4
    prompt: prompts/decision/analyst.md
    latency_budget_seconds: 180
    context_token_budget: 8000
    output_token_budget: 2000
    tools: [retrieve_brief, validate_guardrail]
  adaptive_researcher:
    model: claude-sonnet-4
    prompt: prompts/analysis/adaptive.md
    latency_budget_seconds: 300
    cumulative_tool_call_limit: 25
    cumulative_tool_token_budget: 4000
    tool_caps:
      news_search: 10
      ticker_deep_pull: 5
      # ...
```

### `venue.yaml`
Alpaca-specific parameters. The paper/live split is resolved by `execution_mode` in `main.yaml`.

```yaml
alpaca:
  paper:
    rest_url: https://paper-api.alpaca.markets
    ws_url:   wss://paper-api.alpaca.markets/stream
    api_key_env: ALPACA_PAPER_KEY
    api_secret_env: ALPACA_PAPER_SECRET
  live:
    rest_url: https://api.alpaca.markets
    ws_url:   wss://api.alpaca.markets/stream
    api_key_env: ALPACA_LIVE_KEY
    api_secret_env: ALPACA_LIVE_SECRET
  rate_limit_per_minute: 200
session_hours:
  regular:   { open: "09:30", close: "16:00" }
  pre_market:  { open: "04:00", close: "09:30" }
  after_hours: { open: "16:00", close: "20:00" }
```

### `execution.yaml`
Execution-layer behavior knobs that aren't venue-dictated.

```yaml
greeks_refresh:
  scheduled_interval_minutes: 15
  move_trigger_pct: 2.0
conservative_delta_buffer_pct: 10
submission_retry_window_seconds: 30
paper_harness:
  spread_buffer_pct: 10
  impact_coefficients:
    market: 0.5
    limit:  0.25
    stop:   0.75
pl_target_margin_pct: 5
```

### `guardrails.yaml`
Rule registry: metadata only. Values live in profile files; multipliers live in regime files.

```yaml
rules:
  - id: position_max_size
    enforcement_tiers: [T1, T2, T3]
    escalation_zones: { warning: 70, critical: 85, hard_block: 95 }
    breach_response: deferred_to_pm
    monitor_between_invocations: false
  - id: daily_drawdown
    enforcement_tiers: [T3]
    escalation_zones: { warning: 60, critical: 80, hard_block: 90 }
    breach_response: immediate_engine
    monitor_between_invocations: true
  - id: cumulative_drawdown
    enforcement_tiers: [T3]
    escalation_zones: { warning: 50, critical: 70, hard_block: 85 }
    breach_response: immediate_engine
    monitor_between_invocations: true
    progressive_tiers:
      - { trigger_pct: 8,  max_position_size_pct: 3, max_gross_pct: 80 }
      - { trigger_pct: 10, max_position_size_pct: 2, max_gross_pct: 60 }
      - { trigger_pct: 12, full_halt: true }
  # ...
emergency_invocation:
  cooldown_minutes: 30
  triggers:
    - regime_jump
    - multi_rule_breach_count_min: 3
    - daily_drawdown_velocity_pct_in_minutes: { pct: 60, minutes: 30 }
    - margin_call
```

### `distillation.yaml`
Distillation-layer thresholds and persistence windows. Universe-wide — does not compose with profile, regime, mode, or overlay. Resolver passes the section through to the distillation layer at invocation start. Per-threshold rationale and cold-start bootstrap policy in [02-distillation-layer/threshold-calibration.md](02-distillation-layer/threshold-calibration.md).

```yaml
anomaly_detection:
  volume_anomaly_sigma:                 2.5
  price_move_atr_multiple:              1.5
  options_low_oi_volume_multiple:       5.0
  block_trade_min_shares:               10000
  block_trade_min_notional_usd:         500000
  dark_pool_one_sided_window_minutes:   60
  earnings_revision_cluster_count:      3
  earnings_revision_cluster_days:       5
  macro_surprise_percentile:            90
  funding_stress_component_alert_count: 2
  funding_stress_component_percentile:  90
  market_liquidity_alert_percentile:    10
  news_price_divergence_window_hours:   12

regime_classification:
  regime_low_vol_vix_max:                        14.0
  regime_normal_vix_min:                         14.0
  regime_normal_vix_max:                         22.0
  regime_elevated_vix_min:                       22.0
  regime_elevated_vix_max:                       35.0
  regime_crisis_vix_min:                         35.0
  regime_term_structure_backwardation_threshold: 0.0
  regime_vvix_high_percentile:                   80
  regime_vvix_low_percentile:                    30

regime_transition:
  regime_transition_confirmed_invocations:   2
  regime_transition_indicator_agreement_min: 3
  regime_skip_emergency_trigger:             true

lead_lag:
  lead_lag_funding_to_credit_max_days:          3
  lead_lag_credit_to_equity_max_days:           3
  lead_lag_semis_to_tech_max_days:              2
  lead_lag_financials_to_market_max_days:       1
  lead_lag_commodity_to_energy_equity_max_days: 1
  lead_lag_overdue_lead_sigma:                  1.5

narrative_lag:
  narrative_lag_correlation_shift_sigma: 1.5
  narrative_lag_media_silence_hours:     12

persistence_windows:
  volume_baseline_days:             20
  atr_baseline_days:                14
  spread_baseline_days:             20
  correlation_short_days:           20
  correlation_long_days:            60
  sentiment_baseline_days:          60
  sentiment_min_observations:       30
  gap_fill_baseline_days:           252
  gap_fill_min_events:              30
  extended_hours_confirmation_days: 90
  extended_hours_min_events:        20
  prediction_market_history_days:   30
  funding_stress_baseline_days:     60
  market_liquidity_baseline_days:   60

prediction_market:
  prediction_market_delta_pp_threshold:           5.0
  prediction_market_low_liquidity_volume_min_usd: 10000
```

### `assets.yaml`
Per-sector ticker list and benchmark instruments scoping all external data collection. Universe-wide — does not compose with profile, regime, mode, or overlay. Validation procedure, per-criterion data sources and thresholds, and re-evaluation cadence in [asset-universe-validation.md](asset-universe-validation.md).

```yaml
last_full_validation: 2026-04-25
sectors:
  tech:        [AAPL, MSFT, GOOG, GOOGL, AMZN, META, TSLA, NFLX, ORCL,
                PLTR, NOW, CRM, SNOW, SHOP, CRWD, PANW, DDOG, NET, ZS, COIN, SQ]
  semis:       [NVDA, AMD, AVGO, TSM, MU, INTC, QCOM, MRVL,
                LRCX, KLAC, AMAT, ASML, ADI, CDNS, SNPS]
  financials:  [JPM, BAC, GS, MS, C, WFC, SCHW, USB,
                V, MA, AXP, PYPL, FIS, GPN]
  energy:      [XOM, CVX, COP, EOG, DVN, PXD, OXY, SLB, HAL, BKR,
                ET, EPD, LNG, KMI, WMB]

benchmarks:
  SPY:  { role: broad_market, description: "S&P 500 ETF" }
  QQQ:  { role: broad_market, description: "Nasdaq 100 ETF" }
  IWM:  { role: breadth,      description: "Russell 2000 ETF" }
  XLK:  { role: sector_etf,   description: "Tech Select Sector SPDR" }
  # ...
  TLT:  { role: intermarket,  description: "20+ Year Treasury Bond ETF" }
  GLD:  { role: intermarket,  description: "SPDR Gold Shares" }
```

### `profiles/medium.yaml`
Per-profile file: feature flags, active sectors, min position size, rule subset with base values, decision-agent token-budget ranges. Micro, small, large follow the same shape. Full per-profile rule values are authoritative in [rules-and-limits.md](06-risk-guardrails/rules-and-limits.md) and mirrored here.

```yaml
capital_range_usd: [25000, 50000]
risk_priority: exposure_management
feature_flags:
  options_enabled: true
  short_selling_enabled: true
  fractional_shares_required: false
active_sectors: [tech, semis, financials, energy]
min_position_size_usd: 75
rule_values:
  position_max_size_pct: 5
  position_max_loss_equity_pct: 30
  position_max_loss_options_pct: 80
  sector_concentration_pct: 25
  net_long_pct: 60
  net_short_pct: 30
  gross_exposure_pct: 120
  daily_drawdown_pct: 2.5
  cumulative_drawdown_pct: 8
  correlation_max: 0.70
  options_delta_pct: 40
  portfolio_theta_pct_per_day: 0.15
  portfolio_vega_pct_per_iv_point: 1.0
  total_short_pct: 30
  single_short_max_pct: 3
  borrow_cost_budget_pct_per_day: 0.05
  min_cash_reserve_pct: 10
  pending_order_capital_pct: 30
agent_token_budgets:
  strategist: { context: [1500, 2500], output: [1200, 2500] }
  pm:        { context: [1500, 2500], output: [1200, 2500] }
```

### `regimes/elevated.yaml`
Per-regime multipliers. Covers every rule across all profiles; rules absent from a profile (e.g., options rules at micro) are ignored at composition.

```yaml
vix_range: [22, 35]
multipliers:
  position_max_size_pct:           0.70
  position_max_loss_equity_pct:    0.83
  position_max_loss_options_pct:   0.88
  sector_concentration_pct:        0.80
  net_long_pct:                    0.75
  net_short_pct:                   0.83
  gross_exposure_pct:              0.75
  daily_drawdown_pct:              0.80
  cumulative_drawdown_pct:         0.75
  correlation_max:                 0.86    # 0.60 / 0.70
  options_delta_pct:               0.75
  portfolio_theta_pct_per_day:     0.67
  portfolio_vega_pct_per_iv_point: 0.70
  total_short_pct:                 0.83
  single_short_max_pct:            0.83
  borrow_cost_budget_pct_per_day:  0.80
  min_cash_reserve_pct:            1.50
transition:
  tighten_on_entry: immediate
  loosen_on_exit:   linear_over_invocations_3
```

### `modes/halt.yaml`
Per-mode behavioral contract — restricts action vocabulary, narrows guardrail state headers, raises hold thresholds. Behavioral transforms, not numeric multipliers.

```yaml
analyst:
  output_mode: watchlist                 # no sizing, no entry legs
strategist:
  output_mode: defensive_posture
  allowed_actions: [hold, reduce, close, adjust-bracket]
  pending_orders_default: cancel
pm:
  allowed_command_types: [CLOSE, ADJUST, CANCEL]
  emphasis: capital_preservation
```

### `overlays/pre-event.yaml`
Additive multiplier applied on top of the active regime. Activation is runtime-resolved from the event calendar.

```yaml
activation:
  windows_before_event: 2                # invocations
  events: [fomc, cpi, ppi, pce, nfp, earnings]
multipliers:
  position_max_size_pct: 0.80            # -20%
final_invocation_before_event:
  block_new_positions: true
```

### `llm_failure.yaml`
Retry policy per failure mode from [llm-agent-failure-handling.md](llm-agent-failure-handling.md).

```yaml
retries:
  model_api_error:   { attempts: 3, backoff: exponential }
  timeout:           { attempts: 1, strategy: doubled_latency_budget }
  malformed_output:  { attempts: 1, strategy: same_context_corrective }
  context_overflow:  { attempts: 0 }
  tool_use_error:    { attempts: 1, condition: idempotent_only }
```

### `.env`
One value per line, `KEY=value`. Loaded at process start via `python-dotenv`. Every `*_env` reference in YAML must resolve to a key present here at load time.

```
CLAUDE_CODE_OAUTH_TOKEN=...
ALPACA_PAPER_KEY=...
ALPACA_PAPER_SECRET=...
POLYGON_API_KEY=...
FRED_API_KEY=...
# ...
```

## Composition model

The resolver runs once at invocation start and produces a resolved-config snapshot. Agents and the engine consume the snapshot; they never read the YAML tree directly mid-invocation. The snapshot is also persisted to the filesystem with its SHA-256 hash and path recorded in the invocation record's provenance fields per [state-persistence.md § Invocation records](05-execution-layer/state-persistence.md), so the feedback loop can join past invocations to the exact composition that produced their behavior.

| Dimension | Selected by | Source files |
|---|---|---|
| Profile | Operator (pinned in `main.yaml`) | `profiles/{active_profile}.yaml` |
| Regime | Distillation layer per invocation | `regimes/{current_regime}.yaml` |
| Mode | Pipeline state (halt or normal) | `modes/{current_mode}.yaml` |
| Overlays | Event calendar, stress detector | `overlays/*.yaml` (zero or more active) |

Composition order for numeric rule limits: profile base → regime multiplier → active overlay multipliers (multiplicative). Composition for behavioral shape: mode transform applied last, restricting action vocabulary and guardrail state header sections.

The profile boundaries in [rules-and-limits.md](06-risk-guardrails/rules-and-limits.md#transitioning-between-profiles) are not monitored here: profile transitions are manual per that doc. A profile-boundary detector that emits an advisory when portfolio equity crosses a tier boundary is a candidate follow-up; it does not change the active profile.

## Runtime vs. deploy-time classification

**Deploy-time only (require process restart):** paths in `main.yaml`, SQLite pragmas set at connection open, Python/package versions, NSSM service definition, the `.env` file location. These are set once per deployment.

**Invocation-time reload (picked up at next scheduled trigger):** everything in the YAML tree. Rule values, regime multipliers, agent budgets, feature flags per profile, data-source registry entries, venue parameters, execution knobs, mode behavioral contracts, overlay parameters. Operator edits a file, the next invocation picks it up — no restart.

**Never config (in code):** schema files and the reference-ID taxonomy they define, the OMS command ID template, the breach-response decision tree per rule, the composition-resolver logic, schema validation procedures.

## Validation

Three layers, run in order at each invocation's config load:

**Parse-time.** Pydantic models enforce types, enums, required fields, and ranges. A type failure or missing required field is a structural error: load fails, invocation aborts, operator alerted. Same fail-closed shape as [llm-agent-failure-handling.md](llm-agent-failure-handling.md).

**Cross-reference.** Checks relationships between files: `active_profile` in `main.yaml` names a file that exists in `profiles/`; `active_sectors` in every profile is a subset of the sector keys in `assets.yaml`'s `sectors:` map; every rule ID referenced by a profile's `rule_values` exists in `guardrails.yaml`'s registry; every regime's `multipliers` covers every rule present in every profile; `api_key_env` and `api_secret_env` references resolve to keys present in `.env`; agent `model` values are in the allowed-models list; every tool name in an agent's `tools` list is a registered tool.

**Semantic self-test.** Invariants that require computation: cumulative-drawdown `progressive_tiers` are monotonically increasing in trigger percentage; no regime multiplier drives a rule limit to zero or negative for any profile; escalation zones are ordered `warning < critical < hard_block`; each profile's `capital_range_usd` does not overlap another profile's; every ticker symbol in `assets.yaml` is unique across all sectors and benchmarks and matches `^[A-Z][A-Z0-9.]*$`; each sector listed in any profile's `active_sectors` has at least one ticker in `assets.yaml`; `last_full_validation` in `assets.yaml` is no later than today; feature-flag closure — for each profile with `options_enabled: false`, no options rule appears in `rule_values`, no options-specific agent appears in `agents.yaml`'s active set, and no options-dependent field appears in guardrail state header configuration.

A failure at any layer aborts the invocation and alerts the operator.

## Feature flag semantics

Flags are declared in profile files and cascade through the resolved snapshot. The cascade is **closed**, not **masked**: when `options_enabled: false` at micro, the resolved config contains no options rules, no options greeks in guardrail state headers, and no options commands in the PM's action vocabulary.

The semantic self-test validates closure per flag per profile. A failure there indicates a drift between a profile's flag value and its `rule_values` or `agents.yaml` entries — a structural error the operator fixes before the invocation runs.

## Reload model

The scheduler re-reads the entire YAML tree before each invocation begins. Validation runs to completion before any agent is invoked or any engine action taken. Successful validation produces the resolved-config snapshot that persists for the duration of the invocation.

This aligns with the fresh-context principle from [mid-pipeline-failure-handling.md](mid-pipeline-failure-handling.md) and with APScheduler's `max_instances=1` guarantee that two invocations are never in flight simultaneously.

## Deferred items

**Per-run-type pipeline overlays** — the README TODO about tailored configs per invocation trigger (pre-open, intraday, pre-close, after-hours) — are deferred until after the base config is in place. The likely shape is a `run_types/` directory parallel to `modes/`, composed at resolution time based on the firing trigger, but the content and scoping rules are not designed here.

---

*Cross-references:*

- Rules and limits per profile: [06-risk-guardrails/rules-and-limits.md](06-risk-guardrails/rules-and-limits.md)
- Regime multipliers and transition mechanics: [06-risk-guardrails/regime-adaptation.md](06-risk-guardrails/regime-adaptation.md)
- Halt mode behavioral contract: [06-risk-guardrails/state-delivery.md](06-risk-guardrails/state-delivery.md)
- Breach classification per rule: [06-risk-guardrails/breach-behavior.md](06-risk-guardrails/breach-behavior.md)
- API key inventory: [01-data-layer/api-key-checklist.md](01-data-layer/api-key-checklist.md)
- Data source tiers and freshness SLAs: [01-data-layer/api-failure-handling.md](01-data-layer/api-failure-handling.md)
- Asset universe validation methodology: [asset-universe-validation.md](asset-universe-validation.md)
- LLM agent roster: [../architecture/llm-integration.md](../architecture/llm-integration.md)
- LLM failure policy: [llm-agent-failure-handling.md](llm-agent-failure-handling.md)
- Scheduler cron triggers: [../architecture/infrastructure.md](../architecture/infrastructure.md)
- Venue parameters: [05-execution-layer/venue-configuration.md](05-execution-layer/venue-configuration.md)
