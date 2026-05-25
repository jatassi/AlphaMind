## Summary

End-to-end verification on 2026-05-03 surfaced seven distinct issues, all sharing a root cause: the verification scripts and the agents.yaml budgets encode static assumptions that have drifted from the live system's reality (post-bootstrap calibration, current Sonnet 4.6 thinking-block sizing, current parallel-SDK behavior).

The run completed end-to-end (all 5 phases PASS) only after harness code patches and several agents.yaml budget bumps. **Findings 5, 6, 7 already landed in commit** `875c93f` (`fix(analysis): harness stall-retry + agents.yaml budget recalibration (ALP-285)`) during the e2e run itself. **Findings 1, 2, 3, 4 remain pending** — they're verification-script bugs that need separate fixes.

## Status

| \# | Finding | Status |
| -- | -- | -- |
| 1 | `verify_ongoing_collection.py` marketaux cadence comment wrong | Pending |
| 2 | `verify_ongoing_collection.py` finnhub.calendar uses wrong freshness column | Pending |
| 3 | `fred.macro` collector lacks transient-5xx retry | Pending |
| 4 | `verify_bootstrap_calibration_mix.py` lead_lag band has no post-bootstrap exit ramp | Pending |
| 5 | `config/agents.yaml` domain researcher latency budget too tight (post-[ALP-272](https://linear.app/alphamind-jatassi/issue/ALP-272/compress-sector-researcher-input-bundle-to-9k-token-design-budget)) | **Landed in 875c93f** |
| 6 | `harness.py` third parallel SDK call hangs with zero tokens received | **Landed in 875c93f** |
| 7 | `config/agents.yaml` qualitative + adaptive output budgets too tight | **Landed in 875c93f** |

---

## Pending findings

**(1) marketaux.news cadence in** `verify_ongoing_collection.py`. Script line 78 declares marketaux.news as firing every 30 minutes (`CollectorSpec("marketaux.news", "news_articles", 30)`), but the actual cron in `config/collector_schedule.yaml` is `0 */4 * * *` (every 4 hours, sized for the 100-req/day free-tier quota). With the script's 2× tolerance, marketaux is flagged stale after 1 hour when the real fire interval is 4 hours. The 2026-05-03 run reported "marketaux.news … 4h 28m STALE (max 1:00:00)" — within nominal cadence. Fix: change the constant from `30` to `60 * 4` and update the comment. Verification: re-run script against a fresh snapshot; marketaux row should show OK.

**(2) finnhub.calendar freshness signal in** `verify_ongoing_collection.py`. The script measures finnhub.calendar freshness by `MAX(ingested_at)` on `event_calendar`, but the collector's upsert does not bump `ingested_at` for unchanged rows — it only writes when row content changes (new event, status change). Result: when no new earnings events arrive for a day or two, the script flags the collector STALE even though it ran successfully every day on cadence. The 2026-05-03 run flagged "STALE 2d 14h, max 2d" while the collector had successfully run that morning at 04:00:01.680. Fix option A: switch the freshness check to `collection_runs` last-success (`MAX(end_time) WHERE collector_id='finnhub.calendar' AND status='success'`). Fix option B (preferred): apply Option A only to collectors flagged "may be empty" in `CollectorSpec` — always-writing collectors (OHLCV, options, news) keep the row-freshness check; calendar/treasury-style collectors get the run-completion check. Treasury auctions and bls.macro may have the same low-update-rate behavior — audit each `CollectorSpec` for whether row-freshness or run-freshness is the right signal.

**(3)** `fred.macro` collector — no retry on transient 5xx. The FRED API occasionally returns HTTP 502 Bad Gateway. `data_sources/fred/macro.py:collect_series` aborts the entire job on the first failure — no retry inside the collector. Two consecutive failures push data past the 8h verification freshness tolerance, even though FRED outages are typically minutes. The 2026-05-03 run hit `urllib.error.HTTPError: HTTP Error 502: Bad Gateway` at both 14:00 and 18:00 invocations. Fix: wrap the FRED fetch path with backoff retry on `urllib.error.HTTPError` where `code` ∈ {502, 503, 504} and on connection-reset errors. Suggested 3 attempts, exponential backoff (5s, 15s, 45s), idempotent GETs only, attempt count surfaced in the log line. Other collectors (`eia.energy`, `bls.macro`, `finnhub.*`) likely have the same bare-`urlopen`/`requests.get` pattern — audit each.

**(4)** `verify_bootstrap_calibration_mix.py` lead_lag band has no post-bootstrap exit ramp. The script asserts lead_lag calibrated share ≤ 50%, with a band description that says explicitly "≤ 50% calibrated expected immediately post-bootstrap" (`src/alphamind/scripts/verify_bootstrap_calibration_mix.py:132`). There's a `cold_start_deferred` exemption for the high-freq lower-bound bands, but no equivalent exit ramp for the upper-bound bands once the system matures past bootstrap. As pairs accumulate the required event count, they calibrate by design and the band starts producing false-positive FAILs. The 2026-05-03 run had `distillation_pair_lag` with 4 rows, all `calibrated` with 38–39 `n_pair_events` (HYG/SPY, SOXX/QQQ, XLF/SPY, USO/XLE) — 100% calibrated > 50% upper bound → FAIL, even though the calibration is correct behavior. Fix option A — small-N gate: only enforce the upper bound when `total >= MIN_SAMPLE_SIZE` (e.g., 20). With 4 pairs the share is statistically meaningless. Fix option B — post-bootstrap deferral: add a `post_bootstrap_deferred` exemption when every row's `n_pair_events` exceeds the calibration threshold AND the table has been continuously written for ≥30 days. Fix option C — drop the upper bound: simplest. The lower-bound bands are the meaningful regression signal. Loses the "lead-lag clamping calibrated wrongly" check. Worth a short discussion before picking.

---

## Landed findings (commit `875c93f`)

**(5) Domain-researcher** `latency_budget_seconds`. Commit `b304e69` ([ALP-272](https://linear.app/alphamind-jatassi/issue/ALP-272/compress-sector-researcher-input-bundle-to-9k-token-design-budget)) cut the budget from 480s → 120s on the assumption that compacting the sector bundle from \~15K → \~9K tokens would shrink runtime proportionally. Compaction did not change wall clock meaningfully — Sonnet 4.6 extended thinking dominates. The 2026-05-03 run measured 200s, 346s, 345s for the three sectors. Fix landed: bumped all three to 600s. The runbook claim of "8–15s each" (`scripts/RUNBOOK_domain_researchers.md:112`) is also stale and should be updated to match Sonnet 4.6 reality (200–400s with extended thinking).

**(6) Parallel-SDK hang in** `harness.py`. When `run_domain_researchers` fires three SDK queries in parallel against the same OAuth token, one consistently hangs receiving zero messages of any kind for the full latency budget. The hang is not sector-specific — different sectors got starved across the four runs during diagnosis. Smoking-gun signature in metadata.json: `input_tokens=0`, `output_tokens=0`, `wall_clock_seconds=<budget>`. Healthy calls receive `SystemMessage` within 1–3s of spawn; hung calls receive nothing. Likely cause: Anthropic per-OAuth-token concurrency limit — third request gets queued indefinitely under the cap-enforcement classifier when sibling requests are streaming. Fix landed: added a new internal `_StuckSDKCall` exception; `_collect_response` now accepts `init_stall_timeout_seconds` (60s in production) and applies it as a watchdog only on the wait for the first SDK message (watchdog drops once any message arrives, so extended-thinking gaps don't trigger); `_invoke` retries once on `_StuckSDKCall`, with a second consecutive stall raising `TimeoutFailure` carrying a stall-specific message; extracted `_convert_invoke_error` and `_next_message` helpers to centralize exception translation and keep complexity under ruff thresholds.

Follow-up needed for finding 6: add a regression test that injects a stall on the first SDK call and asserts the retry recovers; check whether qualitative_researcher / adaptive_researcher / synthesizer harnesses are exposed to the same issue (they're single-call, not 3-parallel, so likely safe, but worth verifying); consider hoisting the watchdog into `_shared` if multiple harnesses need it; open a separate question for ops on whether per-OAuth-token concurrency is something we want to lean into (with serialized fallback) or work around at the SDK layer.

**(7) Qualitative + adaptive output budgets.** Both consistently produce more output tokens than budgeted: qualitative_researcher produced 13,029 vs budget 6,000; adaptive_researcher produced 9,005 vs budget 8,000. Brief content was structurally correct (qualitative) or a quiet-cycle envelope (adaptive); not a prompt-runaway. Sonnet 4.6 extended-thinking tokens count against `output_token_budget` per the SDK accounting. Fix landed: both bumped to 16,000 (1.7–2× observed actuals).

Follow-up consideration for finding 7: should the cost-modeling doc be updated to reflect actual Sonnet 4.6 + extended-thinking accounting? If the brief content is the desired shape, accept the new floor; if the budgets represent design constraints that shouldn't drift, the prompts could enforce sentence limits per section.

---

## Why these are grouped

All seven findings are instrument drift — the verification scripts, the agents.yaml budgets, and the harness's parallel-SDK assumption all encode static expectations the live system has moved past. Each surfaced as a "FAIL" during the e2e run that masked correct behavior. Until the four pending findings land, every routine e2e run produces FAIL signals the operator has to manually triage as false positives.

## Discovered

End-to-end verification run, 2026-05-03. See conversation logs.