# Production runbooks

Operator workflows for AlphaMind in production — on the Windows trading machine,
against the live `alphamind.db` and paper Alpaca account. This directory holds
**procedures executed against the live prod system**; verification-artifact companion
docs live beside their scripts under `scripts/verify/`. Agents reach these through the
`operate-prod` skill, which routes situations onto the right runbook and carries the
agent-side directives (autonomy gate, never-autonomously list, self-correction duty).

| Runbook | Answers |
|---|---|
| [update-loop.md](update-loop.md) | Deploying latest `main`: pull, sync, migrate, restart, verify |
| [services.md](services.md) | The seven NSSM services, restarting one, ports + paths reference |
| [invocations.md](invocations.md) | Running a manual invocation; the scheduler's run-type schedule |
| [monitoring.md](monitoring.md) | Watching an invocation live; after-the-fact investigation |
| [troubleshooting.md](troubleshooting.md) | Symptom-indexed gotchas — the living doc agents append to |
| [command-center.md](command-center.md) | Command-center bring-up, access, passkeys, recovery |
| [feedback-loop.md](feedback-loop.md) | Feedback-loop CLIs and cadences on prod |
| [bootstrap.md](bootstrap.md) | One-time first-run bring-up (Operator-supervised) |
| [genesis-cutover.md](genesis-cutover.md) | One-time genesis cutover (Operator-executed) |

The split docs keep the original monolith's section numbering (update-loop = § 1,
bootstrap = § 2, invocations = §§ 3–4, monitoring = § 5, services = §§ 7 + 10,
troubleshooting = § 8, feedback-loop = § 9), so cross-doc references like
`monitoring.md § 5.4` stay stable; a doc opening at "§ 7" is complete, not truncated.

> **Living documents — agents, keep them current.** If you follow a procedure here
> and it's wrong, outdated, or doesn't match actual prod behavior, update the runbook
> in the same change that produces the fix. Don't paper over a broken step with an
> ad-hoc workaround — make the runbook reflect reality. New monitoring patterns, new
> gotchas, new failure modes, schedule changes, port changes — they all belong here so
> the next operator (human or agent) inherits the lesson.
