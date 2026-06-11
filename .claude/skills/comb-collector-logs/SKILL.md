---
name: comb-collector-logs
description: Use to comb the AlphaMind collector logs at `C:\Users\jacks\AlphaMind\logs\` for bugs, irregularities, and silent failures — then file Linear issues for confirmed bugs in the To-dos project. Triggers on `/comb-collector-logs` and operator phrases like "comb the collector log for bugs", "look for irregularities in the collector log", "scan the logs for more bugs", "audit the collector logs", "what else could be wrong with the collectors", "find more collector issues", and follow-ups to a fix like "check if there's anything else like that". The skill grep-scans `collector.log` for ERROR/Traceback events, cross-references each against known-transient patterns in operator memory (`collector-service-ops.md`), reads the relevant source files to verify root cause, checks rotated logs for recurrence vs one-off classification, MANDATORILY confirms each candidate bug with the operator before filing (many are already-fixed or expected behavior), then files self-contained Linear issues with symptom/evidence/root-cause/file-refs/scope/acceptance-criteria/verification, priority based on severity, and `blockedBy` wiring where one bug's fix depends on another. Do NOT use for one-off "what does this log line mean" questions or for non-collector logs.
---

# Comb the collector logs for bugs

The operator asks for a sweep of the AlphaMind collector logs to surface bugs that the regular fix-as-you-find loop missed. The output of a successful run is: a short severity-ranked findings report to the operator, operator confirmation on which candidates are real bugs (vs already-fixed or expected), and one filed Linear issue per confirmed bug in the To-dos project under the AlphaMind team.

## Why this skill exists

The collector runs ~12 vendors on cron and logs aggressively. Real bugs hide in the noise:

- **Retry-classifier gaps** look like vendor flakiness — the error reappears every few days, the retry seems "to have fired" (the trace shows the wrapper), but the classifier silently returned False for that exception type and gave up after one attempt.
- **Multi-collector simultaneous errors** at the same second look like a code bug per collector but are actually one infrastructure event (DNS blip, disk I/O stall, NSSM restart) — filing them as separate bugs is wrong.
- **Daily-recurring failures at the exact same minute** look like random vendor 502s but are a stable pattern — vendor-side maintenance window, rate-limit reset, or a tick-aligned cron interaction.
- **Already-fixed bugs** show up in today's log because the fix was just merged and the next cycle hasn't proven it yet — filing them wastes operator time.

The procedure below was distilled from a real run that surfaced three bugs (ALP-418/419/420) plus dismissed two already-known ones (fred.macro, finnhub.estimate_revisions) and one infrastructure event (one-off SQLite disk I/O).

## Inputs the operator provides

Usually nothing — the skill knows where to look. The operator may sometimes scope it ("just check today's log" or "look back a week") in which case adjust the rotated-log sweep accordingly.

Where things are:

- **Today's log:** `C:\Users\jacks\AlphaMind\logs\collector.log` (Python logger output, daily rotation)
- **Stderr:** `C:\Users\jacks\AlphaMind\logs\collector.err.log` (NSSM-captured stderr; usually KeyboardInterrupt noise from clean restarts — skip unless something else surfaces)
- **Rotated:** `C:\Users\jacks\AlphaMind\logs\collector.log.YYYY-MM-DD` (30-day retention)
- **Stdout:** `C:\Users\jacks\AlphaMind\logs\collector.out.log` (huge — INFO chatter; only read if you have a specific reason)
- **Memory:** `C:\Users\jacks\.claude\projects\C--Users-jacks-AlphaMind\memory\collector-service-ops.md` has the known-transient patterns

## Hard rules

### 1. Operator confirmation is mandatory before filing

Many candidate "bugs" are already-fixed (the fix landed today and the next cycle hasn't proven it), expected behavior (e.g. `IBorrowDeskBlockedError` is the vendor's deliberate block — the abort-on-block is intentional), or known-and-deferred. The operator carries this context and you don't. Always present findings first and wait for explicit confirmation per item before filing.

If the operator says "X was already fixed" or "Y is expected", drop those from the file list. Don't file what they already know.

### 2. Distinguish infrastructure events from code bugs

Multi-collector simultaneous errors at the same second (`±100ms`) are almost always one infrastructure event, not N code bugs. Classify the event class:

- `sqlite3.OperationalError: disk I/O error` across multiple collectors → SQLite/disk event (antivirus scan, Windows Defender, file lock, hardware blip)
- `httpx.ConnectError: getaddrinfo failed` across collectors → host DNS failure
- `ConnectionResetError` / connection refused → network egress blip

These may surface a real bug *adjacent* to them (e.g. the retry classifier doesn't cover the exception type — that *is* a code bug), but the infrastructure event itself is rarely the bug. Note it in the findings as "anomaly (not a code bug)" and move on, unless the operator wants persistence-layer retry coverage.

### 3. Recurrence informs priority

After identifying a candidate bug in today's log, grep older rotated logs for the same signature to determine cadence. Use [Grep](#) across `logs/collector.log.*`:

- **One-off** → low-priority unless severity is high
- **Daily-recurring at the same minute** → medium-priority, look for vendor maintenance window or tick-aligned interaction
- **Multi-day pattern** → high-priority if causing data loss; the long timeline shows it's persistent, not a one-day blip

### 4. Read the source code; don't trust the traceback alone

A traceback shows *what raised*, not *what should have caught it*. Always read the relevant file:

- For retry-related errors: read `_is_retryable` in `src/alphamind/data_sources/_common/retry.py` to confirm the exception type is covered. If `_is_retryable` returns False, the retry wrapper silently gave up — that's the bug, not the vendor error.
- For per-ticker collectors: check whether the loop has try/except per ticker. No isolation means one ticker's exhausted retries kills the cycle.
- For wrapped exceptions: check `__context__` and `__cause__` — fredapi wraps `urllib.HTTPError` as `ValueError(None)`, the message is lost but the context isn't.

Cross-check with `git log --oneline -5 -- <path>` to spot fixes that just landed.

## Procedure

### Phase 1 — Inventory

1. List `logs/` with file sizes. Outsized files (recent log dramatically larger than earlier rotations, or one rotated log dramatically larger than its neighbors) are themselves a clue — record them.
2. Read `collector.err.log` end-to-end. If it's all `KeyboardInterrupt` from clean NSSM restarts, skip — that's normal noise. Anything else (uncaught exceptions, native crashes) is a real signal.

### Phase 2 — Enumerate today's errors

Grep `collector.log` for `ERROR|CRITICAL|Traceback|Exception|Failed`. Output mode `content` with line numbers. Build a list of error events:

```
<timestamp>  <collector>  <one-line-error>
```

Look across the list for:

- **Simultaneous bursts** (multiple collectors at the same second) → tag as infrastructure event (Rule 2)
- **Repeating timestamps across days** at the same minute → tag for vendor-pattern check (Rule 3)
- **Singleton errors** → tag for source-code verification

### Phase 3 — Per-event triage

For each event group:

1. **Read the surrounding traceback** in `collector.log`. Note: full exception type, the immediate raising frame, and `__context__` if present.
2. **Cross-check operator memory** at `collector-service-ops.md` for known-transient patterns. If listed there with "should retry":
   - Verify retries actually engaged by checking elapsed time between cycle start (`collector=X start`) and error. Sub-second to a few seconds = retry didn't fire; tens of seconds = retry fired but exhausted.
   - If retry didn't fire, the bug is in `_is_retryable` not classifying that exception as retryable. Read `_is_retryable` in `src/alphamind/data_sources/_common/retry.py` to confirm.
3. **Read the source file from the traceback.** Confirm root cause — don't trust the traceback alone (Rule 4).
4. **Check rotated logs for recurrence** — grep the same error signature across `collector.log.YYYY-MM-DD` files. Record cadence (one-off, daily, multi-day pattern).
5. **Check git log for recent fixes** — `git log --oneline -5 -- <path-from-traceback>`. A commit in the last day that matches the symptom means it was already fixed.

### Phase 4 — Report to operator

Present a concise findings report with severity tags. For each candidate, include:

- **Symptom in one line** (collector name + error type + cadence)
- **Root cause in one sentence** (file:line ref + the gap)
- **Evidence** (specific log line excerpts where load-bearing)
- **Severity hint** (High / Medium / Low based on data loss potential, recurrence, and silence — silent failures are worse than loud ones)

Then ask the operator explicitly which candidates are real bugs vs already-fixed/expected. Do not file before getting confirmation per item.

### Phase 5 — File Linear issues

For each confirmed bug, file via `mcp__plugin_linear_linear__save_issue`:

- **Team:** `AlphaMind`
- **Project:** `To-dos`
- **State:** `Todo`
- **Priority:** 2 (High) for silent error swallowing / data loss / persistent recurrence; 3 (Medium) for known-pattern recurring failures; 4 (Low) for resilience improvements where data isn't lost (e.g. next cycle backfills)
- **Title:** symptom-first, no jargon (e.g. "_is_retryable misses httpx.NetworkError family — DNS/connect failures aren't retried")
- **Description:** self-contained — an implementer should be able to pick it up cold

Body template (sections in this order):

```markdown
## Symptom

<observable behavior — what fails, when, what error>

<evidence — log excerpts with specific timestamps, log file references>

## Root cause

<single-paragraph or short-list explanation linking the symptom to file:line refs>

## Scope

<numbered list of concrete edits — files to change, classes to extend>

## Acceptance criteria

<bullet list of testable conditions — unit tests, post-deploy observations>

## Verification

<how to confirm the fix works in prod — script invocation, log absence>
```

Include `## Related` at the bottom if the bug references already-fixed work or another filed issue.

**`blockedBy` wiring:** if one bug's fix is a prerequisite for another's effectiveness (e.g. ALP-419's retry-budget bump depends on ALP-418's classifier fix to actually retry the relevant exception types), pass `blockedBy: ["ALP-XYZ"]` on the dependent issue. Use this sparingly — only when the second fix is meaningfully broken without the first.

After filing, report back to the operator with the ALP-IDs and one-line summaries. Offer to draft a patch for the highest-leverage / lowest-risk bug if the operator wants immediate action.

## What not to do

- **Don't file infrastructure events as code bugs** (Rule 2). A one-off `disk I/O error` is not a defect in the collector.
- **Don't file already-fixed bugs.** Always check `git log` and confirm with operator.
- **Don't over-describe** in the findings report — operator wants the signal, not the search transcript. The Linear body is where the detail goes.
- **Don't read `collector.out.log`** unless there's a specific reason — it's 600MB+ and almost entirely INFO chatter that `collector.log` already covers.
- **Don't file issues that are pure design questions** without flagging them as such. Bug 3 (ALP-420) was filed Low *and* called out the design question explicitly so the operator can close it without work if fail-fast was intentional.
