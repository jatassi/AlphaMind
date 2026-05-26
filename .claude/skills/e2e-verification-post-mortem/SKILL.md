---
name: e2e-verification-post-mortem
description: Use to perform an adversarial post-mortem audit of an AlphaMind end-to-end verification invocation — walk every pipeline layer (distillation → domain researchers → qualitative → adaptive → synthesizer → analyst → strategist → PM), identify bugs vs bootstrap-explained artifacts, file Linear issues with structured symptom/evidence/scope bodies, and wire blockedBy/relatedTo dependencies. Triggers on `/e2e-verification-post-mortem <invocation-path>`, `/e2e-verification-post-mortem` (asks for path), and operator phrases like "perform an adversarial review of the e2e verification at <path>", "post-mortem the latest e2e run", "audit the e2e verification outputs", "review what the agents produced in the e2e run", "review the outputs of the latest e2e", "find bugs in the e2e verification artifacts", "walk through the e2e invocation and identify issues", "do a comprehensive review of the e2e invocation at <path>". The skill walks the layered pipeline asking adversarial questions per layer (data fresh / math mathing / outliers / salient outputs / signal threads / anomaly digging / defensible theses / sensible position changes / sensible commands), MANDATORILY distinguishes real bugs from bootstrap/seeding artifacts (the e2e harness runs against a partially-bootstrapped system where many "X<N observations" reads are expected and must NOT be filed), CONFIRMS triage with the operator per category before filing, files self-contained Linear issues in the To-dos project under AlphaMind team with the standard symptom/evidence (file:line refs)/root-cause/scope/acceptance-criteria/verification/notes body template, wires blockedBy for hard prerequisites and relatedTo for code-area clusters and shared symptoms, and concludes with a parallelization plan grouping issues into waves with a critical-path call-out. The procedure was distilled from inv-20260518T111140Z-7b54d0d6 which surfaced 18 issues (ALP-535 through ALP-552). Do NOT use for one-off "why did the analyst do X" questions or for non-AlphaMind invocations.
---

# E2E Verification Post-Mortem

The operator just completed an AlphaMind end-to-end verification run and wants a comprehensive adversarial review of every layer's output. The output of a successful run is: a triaged candidate-bug list grouped by category with operator-confirmation gating, one filed Linear issue per confirmed bug in the To-dos project (with structured body + appropriate priority + dependency wiring), and a final parallelization plan with a critical-path call-out.

## Why this skill exists

Each e2e invocation produces ~10 layers of agent input/output across ~30 files. Real bugs hide in:

- **Calibration-state conflation**: a green run can still ship modules in `unavailable` (collector broken, vendor missing) right next to `accumulating` (warming up — expected). Both used to share the legacy `bootstrap` label until ALP-540 split them; runs that look healthy at the verdict line still have real bugs hiding in `UNAVAILABLE` entries.
- **Data-quality cascades**: a single broken collector (e.g. FRED unavailable) silently propagates as `correlation: 0` / `beta: 0` defaults into downstream signals. Agents see real-looking zeros and infer no signal.
- **Statistical multiplicity**: σ-tests run across thousands of pairs produce ~100+ false positives at 3σ thresholds without correction. The synthesizer is fire-hosed with noise that looks like signal.
- **Code-path divergence**: analyst, strategist, and PM share a guardrail-state header convention — but the analyst can silently get a different assembler path producing a different view of the same portfolio.
- **Schema discovery overhead**: PM `submit_envelope` conditional required fields and restrictive literals aren't surfaced in prompts. Every invocation eats N retries to rediscover.

These all manifested in inv-20260518T111140Z-7b54d0d6 and produced ALP-535 through ALP-552 (18 issues across data freshness / distillation math correctness / decision layer / operational tooling).

## Inputs the operator provides

- An invocation directory path, typically under `/Volumes/Users/jacks/AlphaMind/.archive/verify-debug-e2e/<YYYY-MM-DD>/inv-YYYYMMDDTHHMMSS-<hash>/` on the dev machine (or `C:\Users\jacks\AlphaMind\.archive\...` on the production server). Per-invocation artifacts are date-partitioned under the archive root (ALP-689 unified the layout — the older `invocations/<inv-id>/` subdir is legacy and only contains pre-cutover runs).
- Sometimes a hint about where to focus ("the strategist looks weird" — start there but still walk every layer).

Invocation directory layout (every file is load-bearing):

```
<archive_root>/<YYYY-MM-DD>/inv-YYYYMMDDTHHMMSS-<hash>/
├── progress.jsonl              ← phase timing, per-agent tool calls / tokens / latency / stop reason
├── resolved_config.json        ← active universe, risk profile, agent configs, model assignments
├── data_calibration_state.json ← structured {schema_version: "1", summary, unavailable[], accumulating[]} — ALP-540 landed; consumed by verify_summary.txt's DATA HEALTH block
├── verify_summary.txt          ← wrapper-stdout artifact: PASS/FAIL lines + verdict + DATA HEALTH; at-a-glance entry point
├── phase_outputs/              ← per-SDK-phase Pydantic boundary models (ALP-689 resume); 6 analysis + 3 decision files in debug-e2e mode
│   ├── tech_semis.json
│   ├── financials.json
│   ├── energy.json
│   ├── qualitative.json
│   ├── adaptive.json
│   ├── synthesizer.json
│   ├── analyst.json
│   ├── strategist.json
│   └── pm.json
├── distillation/               ← phase outputs from the deterministic distillation prefix (sector briefs, regime, correlation-regime brief)
├── analysis/
│   ├── qualitative_researcher/
│   │   ├── prompt.md           ← system prompt the agent ran with
│   │   ├── user_message.md     ← the assembled input bundle the agent received
│   │   ├── response_initial.md ← the agent's text output
│   │   ├── metadata.json       ← model, input/output tokens, tool_calls_used, wall_clock_seconds
│   │   └── errors.json         ← error events captured during the agent's run
│   ├── tech_semis_researcher/  (same shape)
│   ├── financials_researcher/  (same shape)
│   ├── energy_researcher/      (same shape)
│   ├── adaptive_researcher/    (same shape)
│   └── synthesizer/            (same shape; note response.md is a brief preface, NOT the canonical brief)
└── decision/
    ├── analyst/                (same shape)
    ├── strategist/             (same shape)
    └── portfolio_manager/
        ├── (same shape)
        ├── submission_log.json        ← successful envelope submissions + commands + acknowledgments
        └── failed_submission_log.json ← validation failures (schema-discovery bugs surface here)
```

## Hard rules

### 1. Bootstrap-accumulating series are NOT bugs

The e2e harness runs against a partially-bootstrapped system. The DATA HEALTH block (in `verify_summary.txt` and rendered from `data_calibration_state.json`) partitions every module into three buckets — the operator triage maps to them directly:

- **`ACCUMULATING (N) — collector healthy, wait`** → MUST NOT file. The series is warming up by design (e.g., `baseline_days: 13 < 20 for AXP`, `ema_pairs_min_closes: 194 < 200`, `funding_stress_min_observations: 11 < 60`). The collector is writing rows; the module just hasn't accumulated enough history yet. The operator will redirect any such filing.
- **`UNAVAILABLE (N) — operator action required`** → real-bug candidate set. The vocabulary makes the failure mode explicit: `history unavailable` (vendor unauthed / FRED series missing / collector not scheduled), `0 observations` (collector dead, never wrote a row), `baseline calibration_state=unavailable for <TICKER>` (per-ticker collector gap), `no options snapshots for <list>` (entire sector missing snapshots). File one issue per distinct root cause, not one per `UNAVAILABLE` line.
- **`calibrated=N`** → healthy; no triage needed.

What IS a real bug even when the surface label looks benign:

- Severity firing `investigate_now` from an `ACCUMULATING` module (severity should be gated by calibration state)
- An `ACCUMULATING` module publishing numeric defaults (`correlation: 0`, `beta: 0`) instead of `null` / missing-data sentinel (silent default masquerading as real signal)
- A module appearing in neither `UNAVAILABLE` nor `ACCUMULATING` while its downstream consumer shows the missing-data fallback path (silent calibration miscategorization)

When in doubt, ask the operator. Future e2e runs will inherit the same bootstrap context until the underlying collectors mature; if an `ACCUMULATING` series stays at the same observation count across two consecutive runs a week apart, it's actually a collector bug and should be re-triaged into `UNAVAILABLE`.

### 2. Operator confirmation per category before filing

The operator carries context on already-known / already-fixed / expected-synthetic-seeding bugs. Always present the triaged candidate list grouped by category and wait for explicit confirmation per category before filing. The operator may add items you missed or drop items that are out of scope.

### 3. File by category in waves; analyze dependencies at the END

Don't file all 18 issues then wire dependencies after the fact. File category-by-category — data freshness → distillation math → decision layer → operational tooling — each as a coherent batch. After all categories are filed, do a single comprehensive parallelization pass across the full set and wire `blockedBy` / `relatedTo` links.

This ordering matters because each category's issues frequently share design conventions (e.g., the missing-data sentinel pattern across ALP-537/538/539/540/545/546). Identifying the keystone issue that establishes the convention is easier with the full filed set in view.

### 4. Use the per-layer adversarial questions verbatim

The audit walkthrough uses a fixed set of adversarial questions per layer (below). Phrasing matters — the operator uses the same phrasing in handoffs, and switching phrasing each run drops signal.

### 5. Don't audit the synthesizer's response.md as if it were the brief

The synthesizer publishes the canonical brief via tool calls to `publish_brief`. Its `response.md` is a 1-2 sentence preface ("I'll pull portfolio state while processing the full brief") that orients downstream agents to the highest-σ reference IDs. The full brief lives in tool-published artifacts, retrievable per ref ID via `retrieve_brief`. Audit the preface for orienting accuracy, not for brief completeness.

## Procedure

### Phase 1 — Inventory

1. `ls` the invocation directory. Note anomalies in size/count: empty files (`response.md` 0 bytes is suspicious unless it's the synthesizer), missing agent directories, missing `submission_log.json` / `failed_submission_log.json`.
2. Read `progress.jsonl` end-to-end. Build a per-agent table mentally or in scratch:

   | Agent | Model | Tool calls | Output tokens | Latency (s) | Stop reason |

   Flag anomalies: 0 tool calls when tools were available (e.g. analyst with `retrieve_brief` access); extremely short latency (agent bailed); non-`end_turn` stop reasons; outsized tokens.

3. Read `resolved_config.json`:
   - Active sectors and ticker counts (compute pairs: `N * (N-1) / 2` — informs multiple-comparison correction math later)
   - Risk profile + capital range + position caps
   - Agent model assignments and token budgets (`analyst`/`strategist`/`pm` are Opus; researchers are Sonnet)
   - `last_full_validation` date vs invocation date (staleness is informative)

4. Read `verify_summary.txt` first — it carries the wrapper's PASS/FAIL verdict, the 7/7 (or N/7) check summary, and the rendered DATA HEALTH block (`UNAVAILABLE` / `ACCUMULATING` / `calibrated` counts + per-module reasons). This is the at-a-glance entry point for Hard rule 1 triage: anything under `ACCUMULATING (N) — collector healthy, wait` is the "MUST NOT file" set; anything under `UNAVAILABLE (N) — operator action required` is the real-bug candidate set. The structured source is `data_calibration_state.json` (schema_version 1, `summary` + `unavailable[]` + `accumulating[]`) — read that when you need the raw payload (e.g., to filter or compare across runs).

### Phase 2 — Per-layer audit

Walk every layer in dispatch order. Read inputs and outputs end-to-end. Use the adversarial questions verbatim — don't paraphrase. Take notes as you go; synthesize across layers at the end (bugs cluster).

**Data & distillation** (read `analysis/synthesizer/user_message.md` top-to-bottom):

> Data fresh? Math mathing? Outliers that may indicate a computation issue?

The synthesizer's input is the consolidated brief. Sections to walk:
- `=== REGIME ===` — VIX level, term-structure basis, VVIX percentile, realized vol. Default zeros are suspicious.
- `=== INTRA-SECTOR CORRELATION ===` — short vs long window matrices per sector
- `=== CROSS-SECTOR ROTATION ===` — narrative + velocity
- `=== INTERMARKET REGIME SIGNALS ===` — `gld_real_yields`, `oil_xle_beta`, `spy_tlt`, `vix_spy`. Any `correlation: 0` / `beta: 0` is suspicious — distinguish 0-observation (collector failure) from genuine ~0 correlation (latter is rare).
- `=== LEAD-LAG ===` — `commodity_to_energy_equity`, `credit_to_equity`, `financials_to_market`, `semis_to_tech`
- `=== CORRELATION REGIME CHANGE ===` — pair-wise σ deviations. For each pair, ask: is the math sensible? Phantom flags from `long_correlation ≈ 0` (data-alignment artifact) or `short_correlation ≈ 0` (numerical-precision floor)?
- `=== NARRATIVE LAG ===` — count vs qualifying news
- `=== UNIVERSAL CONTEXT ===` — per-module q6.* / q7.* / qual.* blocks. Each line carries the calibration state (`calibrated` / `accumulating` / `unavailable`) and, for non-calibrated states, a `reason:` clause. Apply Hard rule 1 triage: `accumulating` lines are MUST-NOT-file; `unavailable` lines are real-bug candidates.
- `=== ANOMALY FLAGS (N) ===` — N is the load-bearing signal here. If N is in the hundreds with a 3σ threshold, multiple-comparison correction is missing (MATH-2 pattern). Count locus tickers (META in 20 pairs? XYZ in 12? = locus aggregation missing, MATH-3 pattern).

**Qualitative researcher**:

> Surfacing high-signal threads? Getting stuck in the past or paying attention to recent/upcoming events?

Read `qualitative_researcher/user_message.md` (volatility regime + news digest + sentiment aggregates + prediction-market snapshot + event calendar + active theses) and `response_initial.md` (output threads). Compare:

- Does the output focus on next 72h catalysts or rehash old context?
- Do all sentiment tickers show `vol=0, change=0` (frozen pipeline)? Do benchmark/stub tickers share bit-identical values (default-fallback path)?
- Are past-dated prediction-market contracts in the feed (`Iran closes airspace by May 6` on May 18)?
- Is the news digest populated, or 0-collected while sector bundles have headlines (cross-sector roll-up broken)?

**Domain researchers** (tech_semis, financials, energy):

> Producing salient outputs? Anything glaring from the underlying data they failed to pick up on?

For each, read input (sector slice of distillation + sector-specific qualitative bundle with `### HEADLINES (top 30)`) and output (sector brief — leading cluster + sub-sector divergences + per-name anomalies + trade catalysts). Cross-check:

- Did the sector brief cite a headline correctly? Spot-check against the input bundle's HEADLINES section.
- Did it skip an obvious signal? (e.g. a 5d +22% extension with a "Q3 earnings call" headline = post-earnings drift; missing that is a researcher gap.)
- Did it correctly downgrade severity on bootstrap-calibrated alerts? (Tech_semis manually downgrading `funding_stress_alert` to `note_for_context` is good adversarial discipline — but that work belongs at distillation, not analysis. Note for MATH-4 pattern.)

**Adaptive researcher**:

> Output valuable? Able to identify anomalies and get to the underlying details to surface insights?

Read `adaptive_researcher/user_message.md` and `response_initial.md`. Adaptive should have 10-25 tool calls (cumulative limit configurable). 0-3 calls = it didn't do its job. Each finding should structurally carry: trigger (which upstream flag), question, tickers, tools used, findings (with **specific freshness dates** — staleness is a finding itself), assessment, confidence, implication, strengthens/weakens links.

Look for:
- Stale-data discoveries (e.g. CTRA `freshness 2026-05-07` on a May 18 invocation = 11d stale — note for ticker-specific collector bug).
- Internal-consistency observations (`mmf_flow=0 at 100th percentile is internally inconsistent` — adaptive surfaces it; distillation should catch it earlier).
- `Missing:` blocks where tools returned unavailable — these point to macro_data / news_search / prediction_markets tool gaps.

**Synthesizer**:

- `response.md` is the preface — orientation only, NOT the canonical brief (Hard rule 5).
- `metadata.json` `tool_calls_used` should be 1-3 (`publish_brief` plus optional state queries).
- The brief's quality is audited via the consolidated `user_message.md` of downstream agents and the distillation walkthrough above; the synthesizer's response itself is just a preview.

**Analyst**:

> Did it produce any theses? If so, are they defensible/sensible/grounded in upstream data?

- `metadata.json` `tool_calls_used: 0` is a yellow flag. Did the preface mention load-bearing refs (≥4σ flags) that warranted `retrieve_brief` calls? If yes and analyst made 0 calls, that's the ALP-547 pattern.
- Read `user_message.md` guardrail-state header (lines 1-32). Compare to strategist's and PM's headers (their first ~50 lines). If they disagree on `Per-position max size`, `Held positions`, or `Hard blocks`, that's the ALP-549 analyst-view divergence pattern. The strategist/PM share an assembler path; the analyst has its own.
- Read `response_initial.md`. Empty `recommendations: []` is a valid output IF justified by signal absence — not "the brief is incomplete" (treating the preview as canonical is the ALP-547 bug).
- For non-empty proposals: each thesis should cite synthesizer reference IDs (`[SA-TECH-n]`, `[QR-n]`, `[AR-n]`, `[CR-n]`) — never inventing refs.

**Strategist**:

> Did it recommend position changes? Any odd output that might indicate the synthetic seeded portfolio needs to be adjusted?

- Read `user_message.md` — position list, age, P/L, bracket state, activity log. Synthetic-seed tells: all positions at 0.0 hours age, P/L of `+$0`, time deadlines exactly 30d out, entry/target/invalidation rationales as literal `"ENTRY_RATIONALE for debug-thesis-XX"` placeholders.
- Read `response_initial.md` position_assessments. Each should have: status_rationale (signal-absence or signal-against), action_rationale, action_parameters, exposure_impact, cross_position_observations.
- Anti-pattern discipline check: did strategist explicitly name and avoid `conviction_inflation` / `rationalized_continuation` / `thesis_dependency` where applicable? (e.g. a validating signal ≠ add justification; phantom P/L ≠ act.)
- Position-level constraint flag check: `[🔴 CRITICAL]` on a position with large POSITIVE P/L (e.g. +19900%) means the constraint check is doing magnitude rather than signed comparison. That's the ALP-550 pattern.

**PM**:

> What commands were issued? Are these sensible based on upstream inputs?

- Read `submission_log.json`. Per envelope: verdict (approve / approve_with_modification / reject), evaluation (status_classification_warrant / action_status_alignment / action_specific_justification / portfolio_coherence — each pass/fail), rationale_narrative, commands list, submission_results (accepted/rejected with order_id).
- Read `failed_submission_log.json`. Count entries. >3 retries for the first envelope = schema discovery overhead (ALP-548 pattern). Each retry's `validation_error_repr` reveals which schema constraint the model didn't know about.
- Cross-check: does the PM's command count match the strategist's reduce/close recommendations? Holds shouldn't generate commands.
- Activity log handling: did PM correctly handle `RECONCILIATION_ALERT` by holding pending reconciliation rather than acting on the unreconciled state?

### Phase 3 — Triage & categorize

Collect all candidate findings from Phase 2 notes. Categorize into the four standard buckets:

| Category | Examples |
|---|---|
| **Data freshness** | News digest empty for QR; past-dated prediction-market contracts; ticker-specific staleness; macro/FRED collector silent failures; sentiment pipeline frozen / default-fallback artifacts; calibration vocabulary transparency |
| **Distillation math correctness** | Data-alignment guard missing (phantom σ flags on near-zero long correlations); multi-comparison correction missing; locus aggregation missing; severity not gated by calibration state; percentile internal-consistency violations; None-placeholder publishing |
| **Decision layer** | Analyst doesn't fetch full brief; analyst guardrail view divergence; PM schema discovery overhead; position-level constraint sign-check |
| **Operational tooling** | Event calendar synthetic noise; activity log placeholder strings; harness output operator-surface gaps |

For each candidate, apply Hard rule 1: bug or bootstrap-explained? Drop bootstrap-explained items. Annotate each candidate with:
- Severity (High / Medium / Low — see priority guidance below)
- One-line where (file:line or layer)
- One-sentence issue summary

### Phase 4 — Present triage to operator

Show the candidate list grouped by category with severity tags. Format like:

```
DATA FRESHNESS (5 candidates)
  [High]    QR cross-sector news digest empty while sector bundles populated
  [High]    Macro/FRED collectors at 0 observations (DTWEXBGS, DGS, intermarket beta series)
  [Med]     Past-dated polymarket contracts in feed (Iran May 6, Trump China May 3)
  [Med]     Sentiment pipeline frozen — vol=0/change=0 universal + benchmark defaults
  [Med]     CTRA ticker_deep_pull 11d stale vs other tickers fresh

DISTILLATION MATH CORRECTNESS (6 candidates)
  [High]    ...
```

Wait for operator confirmation per category before filing. The operator may add items you missed or drop items that are out-of-scope / already-filed / expected.

If the operator wants to proceed category-by-category (rather than confirm all at once), file each category as it's confirmed.

### Phase 5 — File Linear issues by category

Once a category is confirmed, file all its issues in parallel via `mcp__linear-server__save_issue`. Set:

- `team`: `AlphaMind`
- `project`: `To-dos`
- `state`: `Todo`
- `labels`: `["Bug"]`
- `priority`: per severity (see below)
- `title`: symptom-first, no jargon; specific enough to be useful in a Linear search

Body template (sections in this exact order):

```markdown
## Symptom

<observable behavior — what fails, when, in what layer>

<one-paragraph framing of why this is a bug vs expected behavior>

## Evidence

E2E invocation: `<path-to-invocation-dir>`

`<relative/path/to/artifact>` line X:
```
<copy-pasted excerpt where load-bearing>
```

<comparison to other agents/files if the bug is a divergence>

## Root cause [hypothesis if uncertain]

<single paragraph or numbered list linking the symptom to specific code/data path>

<list multiple hypotheses where ambiguous — let the implementer pick after reading the code>

## Scope

<numbered or labeled list of concrete edits, factored by layer if multi-layer>

<call out anything explicitly NOT in scope so it doesn't accidentally creep>

## Acceptance criteria

- [ ] <testable condition 1>
- [ ] <testable condition 2>
- [ ] <testable condition 3>

## Verification

<how to confirm the fix lands — unit tests, integration tests, direct code paths, sqlite queries, manual inspection of artifacts. NEVER reference running e2e verification, debug-e2e, or re-running the e2e harness as a verification step — the e2e harness is the surfacing tool, not the validation gate, and implementers should not block on its cadence.>

## Notes

<bootstrap-context caveats, related issues, sequencing guidance, what's intentionally NOT in scope>
```

Priority guidance:

- **2 (High)**: silent error swallowing; data integrity / pipeline correctness; decision-layer correctness (wrong commands could be issued); broken pipeline stages; calibration-state vocabulary establishing convention for downstream issues
- **3 (Medium)**: signal degradation that agents work around; efficiency overhead (schema discovery, redundant retrieval); edge-case correctness in computations; missing operator-facing context
- **4 (Low)**: cleanup; brief readability; low-impact resilience improvements; cosmetic placeholder strings

Severity escalation rule: anything that could produce wrong decisions in production is at least Medium. The bar for High is "silent failure that the system reports as healthy" or "downstream cascade where one fix unblocks N others."

Always include the invocation directory path in Evidence so the implementer can audit the original artifacts.

### Phase 6 — Comprehensive parallelization analysis

After all categories are filed, walk through the full list once and identify dependencies:

**Hard `blockedBy` dependencies** — issue A genuinely cannot land without issue B's output. Use sparingly. The most common case: a vocabulary/convention issue (e.g., ALP-540 calibration vocabulary) blocks downstream consumers (severity gating, sentinel publishing, quality flags). If the dependent issue has a viable local-fallback option, prefer `relatedTo` instead.

Example from the seed run: ALP-540 blocks ALP-537/538/539/544 because each of those needs the `accumulating`/`unavailable` vocabulary to express its fix coherently.

**Soft `relatedTo` clusters** — surface for code-area clusters and shared symptoms:
- Same module touched by multiple issues (e.g., correlation breakdown trio ALP-541/542/543 all in `q7.correlation_breakdown` at different pipeline stages)
- Same agent's behavior with two distinct bugs (e.g., analyst pair ALP-547/549 — different files, but same agent's behavior)
- Same symptom unmasked two underlying bugs (e.g., debug-pos-07 +19900% P/L surfaced both ALP-550 sign-check and ALP-552 activity log)

Linear MCP gotcha: relation fields on `save_issue` are append-only. Passing `relatedTo: ["ALP-A", "ALP-B"]` does NOT remove existing relations — it adds. Use `removeBlockedBy` / `removeBlocks` / `removeRelatedTo` to clear. Most common silent-corruption hazard when updating an issue's dependency graph; verify with `get_issue(includeRelations=true)` after wiring.

Build a wave plan:
- **Wave 1**: all issues with no blockers (parallelizable; flag the keystone that unblocks Wave 2)
- **Wave 2**: issues `blockedBy` Wave 1's keystone

If multiple issues touch the same module but at different pipeline stages, note the soft sequencing in `relatedTo` but don't hard-block. Engineers can land them in any order with minimal merge risk if each targets a distinct function.

### Phase 7 — Report

Hand the operator:

1. Issue table (ID, title, priority, blockedBy / relatedTo)
2. Wave plan with cluster annotations
3. Critical path: which single issue unblocks the most downstream work (likely the convention/vocabulary keystone)
4. Coverage check: map each original audit-table finding to its filed issue (so the operator can confirm nothing dropped)
5. Cumulative tally (X issues filed across N categories)

If the audit surfaced items NOT in the original adversarial-question scope (often "additional items from broader audit body"), call those out separately so the operator can see what extra value the walkthrough produced beyond the structured questions.

## What not to do

- **Don't file bootstrap-accumulating series as bugs** (Hard rule 1). `X < N observations` where X > 0 is expected during the bootstrap window. Future e2e runs will inherit the same context until collectors mature.
- **Don't audit the synthesizer's `response.md` as the canonical brief** (Hard rule 5). It's a preface; the brief is in tool-published artifacts.
- **Don't file without operator confirmation per category** (Hard rule 2). Many candidates are already-known, already-fixed, or expected synthetic-seeding artifacts.
- **Don't synthesize before walking every layer**. Bugs cluster across layers; surfacing the locus requires the full walk.
- **Don't add `blockedBy` for stylistic preferences**. If the dependent issue has a local-fallback option, `relatedTo` is the right level. Hard blockers slow work; reserve them for genuine prerequisites.
- **Don't file all issues then wire dependencies as an afterthought**. File category-by-category, then do a single comprehensive parallelization pass at the end. The keystone is easier to spot with the full filed set in view.
- **Don't paraphrase the per-layer adversarial questions**. Operators use the same phrasing in handoffs; switching loses signal.
- **Don't reference running e2e verification in any filed issue's Verification section**. The e2e harness is the surfacing tool, not the validation gate — implementers should not block on e2e cadence to confirm fixes. Use unit tests, integration tests, direct code-path inspection, sqlite queries against the production DB, or manual artifact inspection instead. This applies to phrases like "Re-run debug-e2e", "Re-run the e2e harness", "Verify in the next e2e invocation", or anything that would have the implementer wait for the next e2e run to confirm their work.
