# Project tracker

---

## Ready for implementation

Each entry is a feature ready to have its implementation requirements defined as user stories under `docs/implementation/`. Items are listed in rough dependency order — earlier items unblock later ones.

Status:

- _requirements pending_ — needs user stories drafted
- _stories drafted_ — user stories exist; implementation has not begun
- _in progress_ — some stories complete
- _done_ — all stories complete and verified

Cross-cutting policies and reference specs that constrain implementation but aren't features themselves — architecture decisions, failure-handling policies, asset universe methodology, source-to-target mappings, API key inventory, scenario tests, and the unit/integration test plans — live under `docs/architecture/` and `docs/design/` and are linked from the design docs of the features that consume them.

### Foundation

- [x] **Configuration management** — _done_ — [design](design/configuration-management.md), [stories](implementation/foundation/configuration/)

### Data layer

- [x] **Collector** — _done_ — [design](design/01-data-layer/collector/), [stories](implementation/01-data-layer/collector/)
- [x] **Portfolio state** — _done_ — [design](design/01-data-layer/internal/portfolio-state.md), [stories](implementation/01-data-layer/portfolio-state/)

### Distillation layer

- [*] **Distillation** — _Partially done_ — [external](design/02-distillation-layer/external.md), [internal](design/02-distillation-layer/internal.md), [stories](implementation/02-distillation-layer/)
- [*] **Threshold calibration framework** — _Partially done_ — [design](design/02-distillation-layer/threshold-calibration.md), stories 02 (config schema), 03 (state schema), 04 (bootstrap framework), 07 (Class B refresh), 09 (regime classification), 13 (verification incl. `verify_regime_transition.py`), 14a (audit trail + calibration log convention), 14b (no-magic-numbers audit), 15 (emergency-invocation review), 16 (flag-rate reporter), 17 (calibration-state snapshot + dashboard panel) under [implementation/02-distillation-layer/](implementation/02-distillation-layer/)
- [x] **Replay harness** — _done_ — [design](design/02-distillation-layer/replay-harness.md), [stories](implementation/02-distillation-layer/replay-harness/)

### Risk guardrails

- [x] **Rules & limits** — _done_ — [design](design/06-risk-guardrails/rules-and-limits.md), [stories](implementation/06-risk-guardrails/rules-and-limits/)
- [x] **Guardrail evaluation primitives** — _done_ — [design](design/06-risk-guardrails/guardrail-evaluation.md), [stories](implementation/06-risk-guardrails/guardrail-evaluation/)
- [x] **State delivery** — _done_ — [design](design/06-risk-guardrails/state-delivery.md), [stories](implementation/06-risk-guardrails/state-delivery/)
- [x] **Regime adaptation** — _done_ — [design](design/06-risk-guardrails/regime-adaptation.md), [stories](implementation/06-risk-guardrails/regime-adaptation/)
- [x] **Breach behavior** — _done_ — [design](design/06-risk-guardrails/breach-behavior.md), [stories](implementation/06-risk-guardrails/breach-behavior/)

### Analysis layer

- [x] **Domain researchers** — _done_ — [tech-semis](design/03-analysis-layer/domain-researchers/tech-semis.md), [financials](design/03-analysis-layer/domain-researchers/financials.md), [energy](design/03-analysis-layer/domain-researchers/energy.md)
- [ ] **Qualitative research** — _stories drafted_ — [design](design/03-analysis-layer/qualitative-research.md)
- [ ] **Adaptive research** — _requirements pending_ — [design](design/03-analysis-layer/adaptive-research.md)
- [ ] **Synthesizer** — _stories drafted_ — [design](design/03-analysis-layer/synthesizer.md), [stories](implementation/03-analysis-layer/synthesizer/)

### Decision layer

- [ ] **Analyst** — _requirements pending_ — [design](design/04-decision-layer/analyst.md), [output schema](design/04-decision-layer/analyst-output-schema.md)
- [ ] **Strategist** — _requirements pending_ — [design](design/04-decision-layer/strategist.md), [output schema](design/04-decision-layer/strategist-output-schema.md)
- [ ] **Proposal pre-processor** — _requirements pending_ — [design](design/04-decision-layer/proposal-pre-processor.md), [bundle schema](design/04-decision-layer/proposal-pre-processor-bundle-schema.md)
- [ ] **Portfolio manager** — _requirements pending_ — [design](design/04-decision-layer/portfolio-manager.md), [envelope schema](design/04-decision-layer/pm-envelope-schema.md), [submit_envelope tool schema](design/04-decision-layer/submit-envelope-tool-schema.md)

### Execution layer

- [ ] **Position & thesis model** — _requirements pending_ — [position model](design/05-execution-layer/position-model.md), [thesis model](design/05-execution-layer/thesis-model.md), [orders & brackets](design/05-execution-layer/orders-and-brackets.md)
- [ ] **State persistence** — _requirements pending_ — [design](design/05-execution-layer/state-persistence.md). _Distillation-track stories blocked on this substrate (the SQL `invocations` and `activity_log` tables and a transactional invocation context):_ [`14a-config-change-emission`](implementation/02-distillation-layer/14a-config-change-emission.md), [`15-emergency-invocation-review-report`](implementation/02-distillation-layer/15-emergency-invocation-review-report.md), [`16-flag-rate-empirical-reporter`](implementation/02-distillation-layer/16-flag-rate-empirical-reporter.md). When the substrate ships, set the three stories to `not_started` and dispatch.
- [ ] **OMS commands** — _requirements pending_ — [design](design/05-execution-layer/oms-commands.md), [command schema](design/05-execution-layer/oms-command-schema.md), [engine envelope schema](design/05-execution-layer/engine-envelope-schema.md), [command IDs](design/oms-command-ids.md)
- [ ] **Broker adapter** — _requirements pending_ — [design](design/05-execution-layer/broker-adapter.md), [venue configuration](design/05-execution-layer/venue-configuration.md)
- [ ] **Guardrail enforcement layer** — _requirements pending_ — [design](design/05-execution-layer/architecture.md)
- [ ] **Corporate actions** — _requirements pending_ — [design](design/05-execution-layer/corporate-actions.md)
- [ ] **Reg T margin attribution** — _requirements pending_ — [design](design/05-execution-layer/regt-margin-attribution.md)
- [ ] **Continuous monitor** — _requirements pending_ — [design](design/05-execution-layer/architecture.md)

### Operational tooling

- [ ] **LLM output validation** — _requirements pending_ — [design](design/testing/llm-output-validation.md)
- [ ] **Paper-evaluation harness** — _requirements pending_ — [design](design/05-execution-layer/paper-evaluation-harness.md)
- [ ] **Counterfactual replay engine** — _requirements pending_ — [design](design/05-execution-layer/counterfactual-replay-engine.md)
- [ ] **Command center** — _requirements pending_ — [design](design/command-center.md), [pipeline schema](design/pipeline-control-and-events-schema.md), [monitor schema](design/monitor-control-and-events-schema.md)
- [ ] **Feedback loop** — _requirements pending_ — [design](design/feedback-loop.md)

---


## To-do list

### Quick wins

_Single-line spec edits, config additions, doc cross-references, or deferrals._

#### Land analysis-layer prompts _(Configuration management)_

- [x] Land production-quality prompts at `prompts/analysis/{tech_semis_researcher,financials_researcher,energy_researcher,qualitative_researcher,adaptive_researcher,synthesizer}.md` so the path-existence validator in story 03i (`docs/implementation/foundation/configuration/03i-agents-yaml-md`) can pass. Each prompt implements the design-doc contract for its agent — role, operating context, inputs, task, method, tool policy (where applicable), output contract, worked example, anti-pattern constraints — at the same readiness as the existing `prompts/decision/{analyst,strategist,pm}.md`. Side-effect cleanup: `portfolio_analyst` was identified as a hallucinated agent (no design doc; references traced to the initial commit only) and struck from `architecture/llm-integration.md`, `architecture/system-characterization.md`, `design/configuration-management.md`, `design/cost-and-rate-limit-modeling.md`, `design/feedback-loop.md`, `design/05-execution-layer/state-persistence.md`, and the `03i` / `04e` configuration stories. The strategist subsumes the role the phantom would have played. _Source: discovered while orchestrating the configuration-management implementation work tree on 2026-04-27; resolved 2026-04-27._

### Moderate

_Schema additions, well-scoped multi-file edits, or single-component contributions._

#### Phase 1 enforcement-layer composition wiring _(Risk guardrails)_

- [ ] Build the per-invocation Phase 1 composition step that produces the final `ActiveRiskParameterSet` consumed by state-delivery's renderers and the engine's T3 check. Sequence (per `state-delivery.md § Portfolio state ingestion payload`): `regime_adaptation.resolve_regime_adaptation()` produces the regime-resolved + overlay-applied `ActiveRiskParameterSet`; `breach_behavior.classify_cumulative_drawdown_tier()` reads `DrawdownState.current_drawdown_pct`; `breach_behavior.apply_progressive_tier_overrides()` further-tightens position-size / gross-exposure values when a tier is active. The composed result is what state-delivery renders and what engine T3 checks against. _Source: surfaced by the risk-guardrails story-review audit on 2026-04-28; the regime-adaptation work tree's orchestrator (story 09) explicitly does NOT call into breach-behavior, and breach-behavior's `apply_progressive_tier_overrides` (story 04b) names no caller. Both stories carry an "integration site" note pointing here. Likely home: a new story under the execution-layer / continuous-monitor work tree once that work tree's scope is drafted. Until this lands, scenario tests and the breach-behavior E2E story 08 inline the composition._

### Substantial

_New infrastructure, cross-cutting consolidations, or UI surfaces._

### Deferred

_Items whose work is gated on an external trigger (vendor activation, downstream consumer materializing, sub-decision needed) rather than on operator capacity. Each entry names its trigger; promote to Quick wins / Moderate / Substantial when the trigger fires._

#### Mapping coverage — deferred per-category schemas _(Data layer)_

- [x] Q4 short selling and Q5 partial (earnings-estimate revisions) landed under the two-tier approach. Sources confirmed via POC: FINRA CDN (daily Reg SHO short volume, bi-weekly short interest) and iBorrowDesk's undocumented JSON endpoint (`/api/ticker/{TICKER}` — daily collector + on-demand single-ticker refresh, paced ≥5s to avoid the observed HTTP 444 rate-limit block). Storage tables `short_interest_snapshots`, `short_volume_daily`, `borrow_cost_daily`, `borrow_cost_intraday`, and `earnings_estimate_revisions` documented in [storage.md](design/01-data-layer/collector/storage.md); providers `finra` and `iborrowdesk` registered in [`data_sources.yaml`](../config/data_sources.yaml) (corrects the prior `q4_short_selling.primary: sec_edgar` misconfiguration); collector cron entries added to [`collector_schedule.yaml`](../config/collector_schedule.yaml); module skeletons listed in [data-sources.md § Module layout](design/01-data-layer/collector/data-sources.md). Implementation work split into user stories `05k-finra-vendor-adapter`, `05l-iborrowdesk-vendor-adapter`, `05m-finnhub-estimate-revisions` under [`docs/implementation/01-data-layer/collector/`](implementation/01-data-layer/collector/). The remaining five deferred categories convert to forward-trigger entries below; each lands when its consumer materializes per the storage doc's deferral reasons.

**Forward-trigger entries** _(promote when the trigger fires)_:

- **Q2 microstructure / order flow / dark pool.** Trigger: Polygon tick-data subscription activation. Entities `TradeFlowAggregate`, `LiquiditySnapshot`, `InstitutionalFlow`, `AuctionData`, `IntradayFlowPattern`, `ExtendedHoursFlow` already exist in `schema/order_flow.py`; storage tables land when the upstream tick feed comes online.
- **Q8 commodities specialized (futures curves, CFTC positioning).** Trigger: paper-trading evidence of commodity-positioning theses needing futures-curve / COT inputs. Sub-decision required first: CFTC bulk CSV vs. their Public Reporting Environment (PRE) API; CME futures-curve source pending.
- **Qual2 social sentiment.** Trigger: distillation layer requesting a `SocialSentimentScore` input. StockTwits provider already configured (`qual2_social.primary: stocktwits`); storage table lands when the consumer demands it.
- **Qual4 earnings transcripts.** Trigger: qualitative-research agent or analyst tool requesting transcript bodies. Hybrid pattern (metadata in DB, transcript text on disk) per Qual1's `news_articles`. Source choice (Motley Fool / Quartr / YouTube + Whisper) deferred until consumer requirements are clear.
- **Qual6 sector-specific qualitative catalysts.** Trigger: qualitative-research layer build-out. LLM-derived; the storage shape will track whatever the qualitative-research agent emits (likely event-shaped rows).

#### Earnings transcript NLP pipeline _(Analysis layer)_

- [ ] Specify the transcript ingestion pipeline and the NLP extraction pipeline (tone classification, Q&A clustering, non-answer detection, forward-looking statement extraction). Trigger: earnings commentary data source acquired. _Source: [qualitative-research.md](design/03-analysis-layer/qualitative-research.md)._

**Context.** Pairs with the data-layer Qual4 forward-trigger entry above — both gated on a transcript source being in hand. Tier 2 of the `earnings_commentary` tool currently degrades to `partial_no_transcript` for every call. Schemas (`ManagementToneAnalysis`, `AnalystQADynamics`, `ForwardLookingStatement`, `NonAnswerFlag`, `QAExchange` in [schema/earnings_commentary.py](design/01-data-layer/schema/earnings_commentary.py)) and mappings ([mappings/earnings_commentary.yaml](design/01-data-layer/mappings/earnings_commentary.yaml)) already exist with transcript-derived fields marked `derived from TBD`; storage hybrid pattern pre-cleared in [storage.md §Deferred categories — Qual4](design/01-data-layer/collector/storage.md). Source choice (Quartr / Motley Fool / YouTube + Whisper) and extraction architecture (single-pass LLM vs. two-stage deterministic structuring + LLM annotation) decided at promotion.
