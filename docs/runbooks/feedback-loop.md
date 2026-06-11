# Feedback loop — prod cadences and CLIs

Operating the feedback loop on prod: the analytics/validation/retrospective CLIs and
their cadences. The skills' shared reference is
[docs/agents/feedback-loop-skills.md](../agents/feedback-loop-skills.md).

## 9. Feedback loop — analytics CLIs, cadences, and review skills

The feedback loop (ALP-131) is the month-over-month system-improvement substrate:
it pairs the system's reasoning artifacts with the outcomes they produced and
computes deterministic, conditionable metrics over that pairing. It is **read-only
over trading state** — it measures and proposes, never mutating live trading-state
records — and runs **out-of-pipeline** as a set of operator CLIs plus three review
skills. The deliberative pipeline gained three prod-runtime behaviors that feed it
(documented in their own sections, cross-referenced below).

The end-to-end spine is verified out-of-pipeline by
`scripts/verify/verify_feedback_loop.py` (it runs `alembic upgrade head` on a scratch DB,
seeds a controlled closed-position scenario, and drives resolver → metrics → digest
+ snapshot → validation → supersession → retrospective, asserting each stage). Run
it after a change that touches the feedback-loop tables or the resolver:

```bash
uv run python scripts/verify/verify_feedback_loop.py      # exits 0 on PASS, 1 on any FAIL
```

### 9.1 Prod-runtime behavior changes (the pipeline now feeds the loop)

Three behaviors the deliberative pipeline gained — each documented in full where it
lands, listed here so the feedback-loop picture is complete:

- **`agent_calls` telemetry + provenance every invocation** — every LLM agent call
  persists one `agent_calls` row + a four-file provenance tree. Active in production
  (ALP-907). See invocations.md § 3 ("agent_calls telemetry capture is active in production") and
  the services.md § 10 reference path. The metrics' `WindowDataset` reads `agent_calls` for the
  cost / process / citation-chain tiers.
- **Per-invocation thesis resolution (`ACTIVE → RESOLVED`)** — every deliberative
  invocation runs a `thesis_resolution` step between fill collection and snapshot
  assembly, authoring each closed-position thesis's component outcomes + resolution
  category. See monitoring.md § 5 ("Thesis-resolution skip warnings"). The outcome-tier metrics
  calibrate against these resolved theses; a per-thesis data gap is a skip-WARNING,
  not a fault.
- **Distillation anomaly emission to `activity_log`** — the distillation layer emits
  `DISTILLATION_ANOMALY_FLAG` activity-log entries (ALP-881) when a threshold in its
  anomaly taxonomy trips; the digest's notable-shift surface reads them.

### 9.2 The combined feedback-loop migration (apply before anything reads)

The six feedback-loop tables (`agent_calls`, `validations`, `validation_outcomes`,
`weekly_digest_snapshots`, `retrospective_reports`, `retrospective_decisions`) and
the `activity_log` event-type / source CHECK widenings land in the combined
migration **ALP-879** (`c001fb0000ff_feedback_loop_tables_and_activity_log_check_widen`).
It is applied by the standard `alembic upgrade head` in the update loop (update-loop.md § 1) /
first-run bootstrap (bootstrap.md § 2). Until it is applied on the prod box the `agent_calls`
inserts silently no-op and every analytics CLI errors on the missing table.

### 9.3 Analytics-read CLI — digest, metric, snapshot

`python -m alphamind.feedback_loop.digest.cli` (all subcommands emit JSON on stdout;
`--db-path` defaults to the live DB, `--config-dir` to `config/`):

- `digest [--week YYYY-MM-DD] [--trajectory-weeks N]` — the full six-section
  `WeeklyDigest` for the week (headline outcomes, process pulse, 8–12-week
  trajectory, validation status, notable shifts, replay queue).
- `metric <metric_id> [--window N] [--week …]` — one `MetricResult` aggregated over
  the trailing `--window` weeks.
- `metrics-list` — the registered metric ids (a metric gated on an absent
  dependency, e.g. PM-accuracy before ALP-129, is simply absent).
- `snapshot [--week …] [--trajectory-weeks N]` — **producer**: writes the week's
  `WeeklyDigest` into `weekly_digest_snapshots`, idempotently (the `week_start`
  UNIQUE makes a re-run a reported no-op).

All four are DB-only — no vendor API, no `.env` needed.

**Cadence.** The `snapshot` producer is intended to run **once per week, Sunday
8am ET**, snapshotting the week just closed (omitting `--week` targets the prior
completed week). It is **not yet wired into the scheduler** — run it from an
operator cron / CI job, or by hand. `digest` / `metric` are on-demand reads the
`/feedback-review` skill drives.

### 9.4 Validation CLI — register, evaluate, list, detect-supersessions

`python -m alphamind.feedback_loop.validation.cli` (JSON on stdout; `register` /
`evaluate` take a JSON payload on `--input`, a path or `-` for stdin). DB-only:

- `register` — snapshot the registering invocation's provenance (regime +
  model id from `agent_calls`), freeze the pre-registered criteria, persist the
  validation.
- `evaluate --validation-id … --outcome-id … --evaluated-at …` — compute the
  pre/post metric comparison, derive the verdict + rollback status, write the
  `validation_outcomes` row (or short-circuit if the validation was superseded).
- `list` — the pending (unevaluated, un-superseded) validations.
- `detect-supersessions` — **producer**: mark each active validation whose
  post-edit window was crossed by a conditioning shift (regime transition, model
  version change, or a commit to the watched artifact) `superseded`.

**Cadence.** Run `detect-supersessions` **once per pipeline invocation and once per
commit** (the two events that can break a validation's conditioning context). It is
**not yet scheduler-wired** — drive it from the pipeline-adjacent cron / a commit
hook / CI, or via `/feedback-validate`.

### 9.5 Retrospective CLI — ingest, save-report, capture-decision

`python -m alphamind.feedback_loop.retrospective.cli` (DB-only):

- `ingest --start … --end …` — pull the Phase-1 retrospective data set over the
  window (the analytics-spine `WindowDataset` + the unresolved pending-rollback
  follow-ups).
- `save-report --start … --end … --markdown-file …` — persist the rendered report
  metadata row + write the markdown under
  `data/retrospective_reports/{report_id}/report.md`.
- `capture-decision --report-id … --item-identifier … --decision-type … --verdict …
  --rationale …` — record one walkthrough decision (promotion candidate or
  follow-up), optionally linking a spawned validation.

### 9.6 The three review skills (headless)

The operator drives the loop through three Claude skills, all **headless** — they
read the analytics through the CLIs above and emit markdown the operator reads:

- **`/feedback-review`** — weekly process-metric / monthly outcome-metric review and
  ad-hoc agent/metric deep-dives, over the digest CLI.
- **`/feedback-validate`** — register a pre-registered validation for a prompt /
  config edit, or evaluate one whose window has elapsed. Enforces pre-registration
  discipline + confounder controls + posterior-band reporting (confirmation bias is
  the failure mode it exists to defeat).
- **`/feedback-retrospective`** — open-ended quarterly LLM-driven deep retrospective:
  read many invocations end-to-end, surface patterns the deterministic metrics did
  not catch, propose promotion candidates.

The interactive dashboard for this surface (View F) is **deferred to ALP-686** — the
loop is headless-only for now.

### 9.7 PM-accuracy verification follows story 06e / ALP-129

The PM-accuracy + modification-effectiveness metrics (and their
`counterfactual_replays` loader sub-bundle) are gated on the counterfactual replay
engine (ALP-129). They are **verified when story 06e (ALP-887) lands** alongside that
engine — the spine verify above does not assert them, and `metrics-list` simply
omits the gated metric ids until the dependency is present. This is a known,
intentional gap, not a missing check.

---

