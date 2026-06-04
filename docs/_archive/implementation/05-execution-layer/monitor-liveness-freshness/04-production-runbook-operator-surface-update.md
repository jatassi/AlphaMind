# 04 — Production runbook + operator-surface update

## Goal

Update the operator-facing surfaces to match the post-hardening reality so the operator's procedure is correct, not stale. `scripts/RUNBOOK_production.md` § 8.9 currently documents a manual-restart-only world where "the in-process watchdog didn't trip" and the monitor sat wedged for \~11h; after this epic a silent wedge of either stream forces a budget-neutral reconnect within the frame-staleness bound, and a genuinely-blocked watched task trips the per-cadence watchdog → `os._exit(1)` → NSSM auto-restart within a bounded window. The operator's job shifts from "detect + manually restart" to "confirm auto-recovery happened, and read the new health signals correctly." This is the out-of-pipeline operator-behavior surface (the monitor is not exercised by `verify_debug_e2e.py`), so it gets a dedicated documentation story.

## Reading

* `scripts/RUNBOOK_production.md` § 7 (Restarting a single service — the "`Get-Service … Running` does NOT mean healthy" gotcha + the "see § 8.9" cross-ref); § 8.9 (the WinError 121 monitor-wedge detection + recovery + reconciliation-lag note); § 5.2 (monitor `/events` SSE on 8766); § 5.7 (what to watch for).
* The behavior delivered by the sibling stories: 01a (per-cadence watchdog auto-recovery + control-surface opt-out), 03 (underlying-stream silent-wedge → budget-neutral reconnect + now-watched), 02b/02c/02d (stale gates), 02d/01b (the distinct per-position-stale vs global-stale signal labels).
* `src/alphamind/execution/continuous_monitor/breach_loop/result.py` + 02d — the exact health-signal labels the runbook should name.

## Depends on

* 02b ([ALP-829](https://linear.app/alphamind-jatassi/issue/ALP-829/02b-bracket-stops-freshness-gate-watchdog-safety-critical)), 02c ([ALP-830](https://linear.app/alphamind-jatassi/issue/ALP-830/02c-greeks-refresh-freshness-gate-watchdog)), 02d ([ALP-831](https://linear.app/alphamind-jatassi/issue/ALP-831/02d-breach-loop-migrate-alp-770-gate-onto-cache-api-watchdog)), 03 ([ALP-832](https://linear.app/alphamind-jatassi/issue/ALP-832/03-underlying-price-stream-adopts-the-supervised-stream-primitive)) — all the operator-visible behavior changes the runbook documents. (01a/01b/02a are pulled transitively.)

## Scope

In scope: `scripts/RUNBOOK_production.md` (the production runbook). Docs-only — no code, no tests; CI is skipped for `**.md` per the `paths-ignore` policy, so this lands on local review of rendered correctness.

### 1\. § 8.9 — from manual-restart to confirm-auto-recovery

Rewrite the monitor-wedge section so the current-state framing is auto-recovery, not an 11h hang: a silently-wedged **fill or underlying** stream during RTH forces a budget-neutral reconnect within its frame-staleness bound, and a genuinely-blocked watched task trips the per-cadence watchdog → `os._exit(1)` → NSSM restart. Keep the 2026-06-02 incident as labelled history (what it looked like *before* the fix), but the operator's primary action becomes confirming a fresh PID / StartTime (the watchdog fired) and a behavioral signal, with the manual § 7 restart as the fallback when auto-recovery is itself suspect. Extend the section beyond the trade-updates websocket to name the underlying-price stream as an equally-covered case.

### 2\. § 7 — the watchdog now backstops "Running ≠ healthy"

Update the "`Get-Service … Running` does NOT mean healthy" gotcha: a watched-task wedge now self-recycles within a per-cadence bound (the multi-hour silent wedge should not recur), with the manual StartTime + behavioral-signal verification still the way to confirm. Note the one documented exception — the control-surface HTTP server is registered unwatched (01a opt-out), so it is the residual task a watchdog will not auto-recycle.

### 3\. Document the two stale signals

Name the distinct health signals so the operator interprets them correctly: per-position stale / "no live price received … excluded from stop enforcement" = **subscription lag** (one ticker not yet subscribed — clears at next Phase-1), vs the global-stale / writer-wedged signal = **price stream dead** (the whole feed cold — the auto-recovery path should be firing). Align this with § 8.9's existing reconciliation-lag note so "subscription lag, not a dead feed" reads consistently with the new labels.

### 4\. Cross-reference integrity

Keep the § 7 ↔ § 8.9 cross-references intact and the § 9 ports/paths table accurate. Re-read the rendered sections after editing.

### Out of scope

* The central `scripts/RUNBOOK_end_to_end_verification.md` — unchanged (this is monitor operations, not the debug-e2e gate).
* Any code or test change — purely the operator runbook.

## Acceptance criteria

- [ ] § 8.9 reflects auto-recovery (per-cadence watchdog + budget-neutral reconnect) for **both** the fill and the underlying streams; the \~11h hang appears only as labelled pre-fix history.
- [ ] § 7's "Running ≠ healthy" gotcha names the per-cadence watchdog backstop and the control-surface opt-out as the residual unwatched task.
- [ ] The per-position-stale (subscription lag) vs global-stale (feed dead) signals are documented with the labels 02d emits, consistent with § 8.9's reconciliation-lag note.
- [ ] § 7 ↔ § 8.9 cross-references and the § 9 table remain correct; no orphaned "see § 8.x" pointers.
- [ ] No remaining text presents the silent-wedge-with-no-watchdog behavior as current state.

## Verification

By inspection: re-read the rendered § 7, § 8.9, § 5.2, § 5.7 sections after editing; confirm every cross-reference resolves and the recovery procedure matches the behavior 01a/02a/03/02b/02c/02d actually ship. No automated test (docs-only; CI skips `**.md`).