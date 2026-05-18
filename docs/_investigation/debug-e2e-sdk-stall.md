# Investigation brief — debug-e2e SDK stall

**Date:** 2026-05-18
**Status:** Open. Five verify runs failed; root cause not yet identified.
**For:** Follow-up agent picking this up cold.

## The bug

`scripts/verify_debug_e2e.py` — the single e2e gate that drives one
production-faithful pass through the AlphaMind pipeline — fails
deterministically in the analysis-layer fan-out. One of the four parallel
SDK calls (3 sector researchers + qualitative) admits, never streams an
`AssistantMessage`, and eventually surfaces as a `TimeoutFailure` with
zero tokens and `stop_reason: null`.

The failing agent is not fixed — it rotates run-to-run (energy in runs
1/2, tech_semis in runs 3/5, qualitative in run 4). The duration of the
stall matches whichever timer fires first on that harness:

- domain_researchers (init=60s, between=180s watchdogs): ~376s
  (two 60+180 stall-retry attempts) or longer when the harness is the
  2nd-admitted under cap=1 (1084s observed = 333s queue wait + 2 retries).
- qualitative_research (no watchdogs): 360s (full
  `latency_budget_seconds`).

Same byte-identical input succeeds in 222s when invoked in isolation
(`scripts/_debug_energy_isolated.py`).

## How to reproduce

1. Confirm prereqs per `scripts/RUNBOOK_end_to_end_verification.md`
   (mainly `CLAUDE_CODE_OAUTH_TOKEN` exported, `data/alphamind-debug-e2e.db`
   at alembic head).
2. Arm the progress monitor in a second shell (the authoritative bash
   block in the runbook).
3. Launch the verify:

   ```bash
   set -a && source .env && set +a && \
       uv run python scripts/verify_debug_e2e.py \
           --archive-root .archive/verify-debug-e2e
   ```

4. Expect a `TimeoutFailure` in `_run_domain_researcher` (or qualitative)
   roughly 6–18 minutes in.

The isolated repro that **succeeds** every time:

```bash
set -a && source .env && set +a && \
    uv run python scripts/_debug_energy_isolated.py
```

It reads the captured `user_message.md` from the most recent failed
verify, invokes `invoke_domain_researcher(sector=ENERGY, ...)` once, and
exits.

## What we've tried (chronological)

### Run 1 — baseline (no changes)

- 4 SDK calls fired within 278ms (qualitative + 3 sectors).
- qualitative succeeded (351s); energy stalled at 376s.
- Wrapper exited with TimeoutFailure on a domain researcher.

### Run 2 — bump `_LAUNCH_JITTER_SECONDS` from 0.5s to 10s

- Spawn spread widened to ~3.9s.
- qualitative succeeded (306s); energy stalled at 378s (essentially
  identical to run 1, varying only by ~2s).
- **Conclusion:** spawn-time contention is not the cause. The stall
  duration is independent of jitter.

### Run 3 — global `asyncio.Semaphore(3)` in `_harness_core.invoke_sdk`

- Reverted jitter to 0.5s. Added module-level semaphore capping
  concurrent SDK calls at 3.
- qualitative succeeded (252s); tech_semis stalled at 376s (deterministic
  watchdog pattern).
- **Conclusion:** cap=3 is too high. Failing agent rotates but count is
  still exactly one.

### Run 4 — drop cap to 2

- qualitative was queued 3rd, admitted late, stalled for the full 360s
  budget with zero tokens.
- Failure surfaced as `"Invocation exceeded latency budget of 360.0s"`
  rather than `_StuckSDKCall` because qualitative has no watchdog.
- **Conclusion:** even 2 concurrent calls produces one stall.

### Run 5 — drop cap to 1 (full serialization)

- qualitative ran solo first, succeeded (333s).
- tech_semis admitted next (solo), stalled at 1084s total
  (333s queue wait + ~751s of two 375s stall-retry attempts).
- **Conclusion:** serializing SDK calls does not fix the issue. The 2nd
  SDK call in the same process can stall even with no concurrent peer.

### Isolated repro

- `scripts/_debug_energy_isolated.py` invokes ONE SDK call against the
  byte-identical captured `user_message.md`. Succeeds in 222s every
  time. The success of this script is the strongest disconfirming
  evidence against any content-specific hypothesis.

## Confirmed findings

1. **Not content-specific.** Energy's `user_message.md` is byte-identical
   between failed runs and the successful isolated repro.
2. **Not concurrency-only.** Serializing to cap=1 still fails on the
   second SDK call.
3. **Not the between-message watchdog.** qualitative has no watchdog
   and still stalls (it burns the full latency budget with 0 tokens).
   The watchdog catches the stall *faster*; it does not *cause* it.
4. **First-call-succeeds, subsequent-may-stall pattern.** Across all
   six runs (5 pipeline + 1 isolated repro), the **first** SDK call to
   complete in a Python process always succeeded. The failures only
   appeared on subsequent calls in the same process.

| Run | First SDK call to complete | Status |
|---|---|---|
| 1 | qualitative (351s) | ✓ |
| 2 | qualitative (306s) | ✓ |
| 3 | qualitative (252s) | ✓ |
| 4 | qualitative queued 3rd → ran later | failed (553s) |
| 5 | qualitative (333s) | ✓ |
| Isolated | energy (222s) | ✓ |

## Leading hypothesis

**Process-level state leakage in the Claude Agent SDK or its sub-stack
(MCP servers, local CLI subprocess pool, OAuth refresh, asyncio loop
state).** The first `claude_agent_sdk.query(...)` call in a process
works; subsequent calls in the same process can hang after admission,
emitting `SystemMessage` (or not even that) but never
`AssistantMessage`.

Plausible culprits:

- **MCP server zombies.** Each agent spawns its own MCP server
  subprocesses. The SDK may not clean them up between calls; a hung
  zombie from call N could starve call N+1.
- **Local CLI subprocess pool.** The `claude` CLI binary may maintain a
  pool / cache (sockets, lock files, file descriptors) that doesn't
  reset cleanly between Python-side invocations.
- **OAuth token refresh state.** A first-call refresh that consumes the
  refresh window could leave subsequent calls waiting on a renewal that
  hangs.
- **asyncio loop state.** Generator-cleanup tracebacks were a known
  Windows quirk during harness construction; subtle leftover state
  (cancelled tasks, leaked sockets) could starve a second call.
- **Windows-specific.** AlphaMind production runs on Windows (operator
  is on `win32`); the Claude Agent SDK's Windows test coverage is
  unknown. macOS / Linux might not exhibit the issue.

## Recommended next moves

In rough priority order. Each is 10–30 min of work.

1. **Two-back-to-back-energy test in the same Python process.** Extend
   `scripts/_debug_energy_isolated.py` to invoke `invoke_domain_researcher`
   TWICE in sequence in the same process. If the second call stalls,
   process-state-leakage is confirmed and we have a minimal repro to
   file upstream + use as a workaround test rig. If both succeed,
   process-state-leakage is wrong and the pipeline composition itself
   triggers something we haven't accounted for.
2. **Subprocess-isolate each SDK call as a workaround.** Spawn a fresh
   Python process per agent invocation (`subprocess.run(..., python -m
   alphamind.analysis.<agent>.standalone)`). Heavy-handed and breaks the
   architecture, but if it unblocks the verify gate it confirms the
   process-state hypothesis and gives operators a path forward while
   the root cause is investigated.
3. **Instrument `_collect_response` to log every SDK message arrival.**
   Currently the stall leaves `response_initial.md` empty (0 bytes)
   because diag-flush happens via `_record_failure` without persisting
   what the SDK actually streamed. Adding a per-message debug log
   (timestamp + type + tool name) would let us see whether the stalled
   call emits SystemMessage and then nothing, or doesn't emit anything,
   or emits partial AssistantMessage and then stops.
4. **Run the verify on macOS dev box.** Operator's `CLAUDE.md` lists
   both Windows (production server) and Mac dev box paths. If the
   verify succeeds on Mac and fails on Windows, the issue is
   Windows-specific in the Claude Agent SDK or its dependencies.
5. **Try `output_format` removal.** All three failing harnesses use
   `output_format={"type": "json_schema", ...}`. The SDK's JSON-mode
   handler is a known source of edge-case bugs. Temporarily run with
   structured-output disabled to see if the stall persists.
6. **File an SDK-upstream issue.** If we get a minimal two-call repro
   (move 1), we have ammunition. The hypothesis "second `query()` call
   in same process hangs after admission" is concise and testable
   upstream-side.

## State of the codebase

- **Cap=1 semaphore is active.** `_MAX_CONCURRENT_SDK_CALLS = 1` in
  `src/alphamind/analysis/_harness_core.py:101`. Multi-paragraph comment
  block above documents the empirical journey.
- **Jitter is back to 0.5s** in both
  `src/alphamind/analysis/domain_researchers/harness.py:275` and
  `src/alphamind/analysis/qualitative_research/harness.py:91`.
- **`scripts/_debug_energy_isolated.py`** is a temporary diagnostic
  script (underscore-prefixed, not part of the canonical surface).
  Reads the latest captured `user_message.md` and invokes the
  energy_researcher harness once. Delete or promote depending on next
  agent's call.
- **Runbook is up-to-date.** `scripts/RUNBOOK_end_to_end_verification.md`
  has the authoritative monitor + arm-after-launching guidance.
- **No tests are broken.** `uv run pytest tests/analysis/ -n auto`
  passes (855 + 2 skipped).

## Key files

- `src/alphamind/analysis/_harness_core.py` — shared driver
  (`invoke_sdk`, `_collect_response`, semaphore). The fix landing point.
- `src/alphamind/analysis/domain_researchers/harness.py` — domain
  researcher harness; watchdog config at lines 259-275.
- `src/alphamind/analysis/qualitative_research/harness.py` — qualitative
  harness; no watchdog (line 360).
- `scripts/verify_debug_e2e.py` — e2e gate.
- `scripts/_debug_energy_isolated.py` — minimal isolated repro.
- `scripts/RUNBOOK_end_to_end_verification.md` — operator entry point.
- `docs/design/debug-e2e-mode.md` — design doc (parent issue ALP-493).
- `.archive/verify-debug-e2e/invocations/` — per-invocation archives
  (progress.jsonl + analysis/<agent>/metadata.json for each agent).
- `.archive/repro-energy-isolated/` — successful isolated-repro archives.

## Open questions

- Is this a Windows-only problem? (Mac dev-box test would settle it.)
- Does the Claude Agent SDK have a known issue around multi-call
  process lifetime?
- Would `claude_agent_sdk.query()` with `resume_session_id` between
  calls behave differently?
- Does the failure persist if we disable MCP servers entirely
  (tools=[])?
