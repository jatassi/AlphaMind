---
name: operate-prod
description: Use for operational tasks on the AlphaMind production Windows machine — deploying latest main (the update loop), running a manual invocation, watching or post-morteming a scheduled invocation, restarting NSSM services, and symptom-driven triage. Triggers on operator phrases like "update prod", "deploy to prod", "run a manual invocation", "why did the 10am run fail", "restart the monitor", "the monitor looks wedged". Routes onto the canonical runbooks under docs/runbooks/ and carries the agent-side directives (autonomy gate, never-autonomously list, self-correction duty). NOT for the one-time bootstrap or genesis cutover (Operator-executed), and NOT for feedback-loop cadence work (use feedback-review / feedback-validate / feedback-retrospective).
---

# Operating prod

The Operator names a prod task. Route it onto its runbook and execute from inside that
doc — canonical procedures live in `docs/runbooks/` ([map](../../../docs/runbooks/README.md));
read the routed runbook end-to-end before acting, never from memory of it. This skill
adds only what the runbooks don't carry: routing, the agent-side guardrails, and the
duty to keep the docs true.

## Routes

| Task | Runbook |
|---|---|
| Update / deploy latest `main` | `docs/runbooks/update-loop.md` |
| Run a manual invocation | `docs/runbooks/invocations.md`, watch it via `docs/runbooks/monitoring.md` |
| Watch / post-mortem an invocation | `docs/runbooks/monitoring.md` |
| Restart a service | `docs/runbooks/services.md` |
| Triage a symptom | `docs/runbooks/troubleshooting.md` |

Named, not routed: first-time bootstrap (`docs/runbooks/bootstrap.md`) and genesis
cutover (`docs/runbooks/genesis-cutover.md`) are one-time Operator-supervised
procedures — surface them, don't execute them. Feedback-loop cadence work belongs to
the `feedback-review` / `feedback-validate` / `feedback-retrospective` skills; the prod
operational context is `docs/runbooks/feedback-loop.md`.

## Standing directives

- Prod is Windows: NSSM services, elevated PowerShell, repo checkout at
  `$env:USERPROFILE\AlphaMind` on branch `main`, clean.
- `.env` never auto-sources. Load it explicitly before anything that hits a vendor
  API: `set -a && source <(tr -d '\r' < .env) && set +a` under Git Bash, or
  `load_dotenv()` in Python.
- Single-writer discipline: write the DB only through the sanctioned CLIs and
  services; ad-hoc SQL is read-only.
- Confirm the Alpaca `account_number` before treating broker output as prod evidence.
- Establish local time + timezone first, then translate, before reporting any time.

## Autonomy gate

Before any step that stops services, evaluate the service-stop precondition in
`docs/runbooks/update-loop.md`: market open AND open positions → halt and obtain
explicit Operator confirmation in-session; otherwise proceed.

Never autonomously, regardless of gate state: halt-mode changes, force-close,
cancel-order, DB writes outside the sanctioned CLIs, genesis cutover.

## Self-correction duty

After every executed procedure, compare observed prod behavior against the runbook you
followed and against this skill. On divergence, produce the correction in the same
session: a new experiential gotcha appends to `docs/runbooks/troubleshooting.md`; a
runbook or skill inaccuracy becomes an edit to that file. Deliver corrections as a
docs-only PR from a fresh branch, then restore the checkout to clean `main` — the
update loop's pull step requires it. If pushing or opening a PR is unavailable from
prod, present the exact diff to the Operator in-session instead.
