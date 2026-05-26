# Design — Scheduler `--debug-e2e` resume

**Date:** 2026-05-25
**Shape:** CLI augmentation of `python -m alphamind.scheduler run --debug-e2e`
**Status:** draft for review
**Parent:** [debug-e2e mode](debug-e2e-mode.md) (ALP-493)

---

## 1. What we're building

A `--resume-from <invocation-id>:<phase>` flag on
`python -m alphamind.scheduler run --debug-e2e` that lets an operator
re-run a partially-failed e2e verification without paying for the
already-completed SDK calls. Both the source invocation id and the
target phase are required — the operator names the resume point
explicitly rather than the CLI auto-detecting from the source
progress.jsonl.

The motivating shape: the analyst SDK call hits its latency budget 9
minutes into a run, the operator bumps `decision.analyst.latency_budget_s`
in `config/run_types/<trigger>.yaml`, and re-invokes with
`--resume-from inv-2026-05-25T14:00:00-abc123:analyst`. The new
invocation re-runs the deterministic prefix (phase1, snapshot_assembly,
distillation — all cheap, all deterministic against the synthetic
portfolio fixture), hydrates the six analysis-layer SDK outputs from the
prior archive, then runs analyst + everything downstream with the new
budget. Saves ~10–12 minutes of wall-clock and ~6 Sonnet calls per
iteration.

In scope: the 9 SDK phases (3 domain researchers + qualitative +
adaptive + synthesizer + analyst + strategist + PM). Deterministic
phases (phase1, snapshot_assembly, distillation, pre_processor, phase2)
are always re-run from scratch — they are cheap and produce identical
outputs against the synthetic fixture, so the resume contract does not
need to special-case them. Out of scope: resume against `--debug-e2e`
off (real broker + paper DB) and resume across schema migrations.

Trust boundaries unchanged from the parent feature: the LLM is real,
the production pipeline runs unaltered, the persistence substrate is
unchanged. The only new on-disk state is one JSON file per SDK phase
under each invocation's archive directory.

---

## 2. Principles guiding this design

- **P3 (illegal states unrepresentable):** central. The CLI rejects a
  `--resume-from` target whose source archive is missing any phase
  output required upstream of the resume point — at argparse-time
  validation, before `wipe_and_seed` runs. There is no partial-resume
  fallback or "try anyway" path.
- **P9 (deep modules — small public surface):** central. Resume adds
  exactly one public concept: a `ResumeContext` carried on
  `RunInvocationContext.debug_e2e.resume_context`. Every harness reads
  one boolean (`should_replay`) and one path (`source_archive`); no
  harness learns the resume target's name.
- **P2 (import graph IS the architecture):** moderate. The resume
  loader (`debug_e2e/resume.py`) imports from the harness output
  dataclasses but no harness imports the loader — replay is a property
  of the orchestrator, not the harness.
- **P5 (boundary types Pydantic, internal types frozen dataclasses):**
  central. Phase outputs are serialized via Pydantic when crossing the
  on-disk boundary; internal handoff stays in frozen dataclasses. The
  `phase_outputs/<phase>.json` files round-trip cleanly because the
  serializer + deserializer share the same Pydantic model.

---

## 3. CLI surface

Two new flags on `scheduler run`, both require `--debug-e2e`:

```
--resume-from <invocation-id>:<phase>
    Hydrate SDK-phase outputs from a prior invocation's archive and
    re-run from <phase> onward. Both the source invocation id and the
    target phase are required — colon-separated, with neither empty.
    Mutually exclusive with --fresh-start.

--archive-root <path>
    (Already exists.) When --resume-from is set, this root is also
    where the source invocation directory is looked up. The CLI does
    not accept a separate source-archive root — operators run all
    debug-e2e invocations against the same archive root.
```

`verify_debug_e2e.py` forwards `--resume-from` unchanged to the
subprocess.

**Argparse-level rejections** (exit code 2, no DB writes):

- `--resume-from` without `--debug-e2e`.
- `--resume-from` with `--fresh-start` (resume against a different
  portfolio fixture is a category error — the prior archive's outputs
  were produced against the fixture the original run used).
- `<invocation-id>` directory not found under `<archive-root>/invocations/`.
- `<phase>` not in the 9-SDK-phase whitelist (`tech_semis`,
  `financials`, `energy`, `qualitative`, `adaptive`, `synthesizer`,
  `analyst`, `strategist`, `pm`).
- Any phase **before** `<phase>` in the dependency DAG is missing its
  `phase_outputs/<phase>.json` file in the source archive. The error
  names the missing phase and suggests an earlier resume target.

---

## 4. Phase output contract

Each SDK harness writes one new artifact on successful completion:

```
<archive>/invocations/<invocation_id>/phase_outputs/<phase>.json
```

where `<phase>` is one of the 9 SDK-phase names. The deterministic
phases (distillation, pre_processor) do not write `phase_outputs/` —
they are re-computed on resume.

**Schema.** One Pydantic boundary model per phase, lifted from (or
mirroring) the existing typed result dataclass:

| Phase | Source dataclass | New Pydantic model |
|-------|------------------|--------------------|
| `tech_semis` / `financials` / `energy` | per-sector entry of `DomainResearchersOutput` | `DomainResearcherOutputModel` (one file per sector) |
| `qualitative` | `QualitativeResearcherResult` | `QualitativeResearcherResultModel` |
| `adaptive` | `AdaptiveResearcherResult` | `AdaptiveResearcherResultModel` |
| `synthesizer` | `SynthesizerResult` | `SynthesizerResultModel` |
| `analyst` | `AnalystResult` | `AnalystResultModel` |
| `strategist` | `StrategistResult` | `StrategistResultModel` |
| `pm` | `PortfolioManagerResult` | `PortfolioManagerResultModel` |

The Pydantic models live alongside the dataclass definitions
(`<agent>/models.py` or equivalent) and ship with bidirectional
converters: `to_domain()` → dataclass, `from_domain(dc)` → model.
Re-uses the same boundary-projection pattern as `DistillationConfig` →
`DistillationConfigDomain` introduced in ALP-471.

**Atomic write.** Phase outputs are written via the existing
`alphamind._kernel.atomic_io.atomic_write_text` so a crash mid-write
never produces a partial file the resume loader would misread.

**File-naming gotcha** for domain researchers: three sectors run under
one `domain_researchers` TaskGroup, but each produces an independent
SDK call and writes its own `phase_outputs/<sector>.json`. The resume
loader requires all three files present to honor a resume target
≥ `qualitative` (or anywhere downstream).

---

## 5. Resume execution model

A resume invocation is a **new invocation** — fresh `invocation_id`,
fresh archive directory under `<archive-root>/invocations/<new-id>/`.
The source archive is read-only. The new archive's progress.jsonl
records every phase, but phases that were replayed carry a
`replayed_from: <source-invocation-id>` field on their `phase_done`
event.

The orchestrator threads a `ResumeContext` through
`RunInvocationContext.debug_e2e`:

```python
@dataclass(frozen=True, slots=True)
class ResumeContext:
    source_archive_dir: Path        # <archive-root>/invocations/<source-id>
    resume_phase: str               # one of the 9 SDK-phase names
    phases_to_replay: frozenset[str]  # the set strictly before resume_phase in dep order
```

`phases_to_replay` is computed by the loader at startup from the
phase-dependency DAG hard-coded in `debug_e2e/resume.py`. The loader
also pre-validates that every name in `phases_to_replay` has a
corresponding `phase_outputs/<phase>.json` file.

**Per-phase behavior.** The check lives in the pipeline composition
runners (`run_analysis_pipeline` and `run_decision_pipeline`), not in
each agent's harness — two sites instead of nine, and the runner
already owns the archive-root context the diagnostic-copy step needs.
Before each SDK phase, the composition runner checks
`context.debug_e2e.resume_context`. If present and the upcoming
phase is in `phases_to_replay`:

1. Load `<source-archive>/phase_outputs/<phase>.json`, parse into the
   Pydantic model, convert to the typed dataclass.
2. Copy the per-agent diagnostic directory
   (`<source-archive>/{analysis|decision}/<agent>/`) verbatim into the
   new archive — preserves the audit trail without re-running the SDK.
3. Emit `phase_start` (immediately) and `phase_done` (with
   `replayed_from` set) on the progress emitter.
4. Skip the agent's runner entirely; pass the hydrated dataclass to
   the next phase.

If the upcoming phase is the resume target or downstream, normal
runner invocation. The agent harness does not learn about replay —
it either runs (target / downstream) or is skipped entirely (upstream
replay).

**`wipe_and_seed` runs unconditionally** on every debug-e2e
invocation, including resume. The synthetic portfolio fixture is the
same on every run, so the deterministic prefix re-produces the same
inputs to the replayed phases. Skipping `wipe_and_seed` would force a
side channel for preserving DB state between invocations; the cost of
re-running it (~10s) is not worth that complexity.

**Deterministic-prefix invariant.** Phase1 + snapshot_assembly +
distillation must produce byte-identical outputs on a resume run vs
the source run. If they don't, the replayed downstream phases are
operating against a different upstream than they originally saw — a
silent correctness bug. The verify wrapper gains a single new
post-resume check (`check_deterministic_prefix`) that hashes the
distillation outputs against the source archive and FAILs on
mismatch. This protects against (a) someone editing the synthetic
portfolio fixture between runs, (b) a non-determinism regression
slipping into distillation.

---

## 6. Failure modes

| Failure | Surface | Operator next step |
|---------|---------|--------------------|
| Source invocation directory missing | argparse, exit 2 | Confirm `--archive-root` matches the source run; list invocations in the directory |
| Resume target not a valid SDK phase | argparse, exit 2 | Pick one of the 9 SDK-phase names from the error message |
| Upstream phase output missing | argparse, exit 2 | Restart from an earlier phase (the error names the earliest valid resume target) |
| Phase-output JSON fails Pydantic validation | startup, exit 1 | Schema drift between the source-run code and the current code — restart from scratch |
| `check_deterministic_prefix` FAILs | post-run verify | Investigate non-determinism; do not trust the run's downstream output |
| Phase that was supposed to replay raises | harness, exit 1 | Same as a normal pipeline failure — the resume infrastructure does not mask exceptions |
| Resume target itself fails | harness, exit 1 | Iterate normally — the new archive becomes the next resume source |

The verify wrapper's existing 7 PASS lines remain. Resume runs add one
new check (`check_deterministic_prefix`) for a total of 8.

---

## 7. Hardest-to-reverse decisions

1. **New invocation, not in-place resume.** A new `invocation_id` for
   each resume run preserves the prior run's audit trail untouched —
   the operator can compare the two runs side-by-side. The in-place
   alternative (overwrite the failing phase's artifacts under the same
   `invocation_id`) is cheaper to implement but loses the historical
   evidence of why the original run failed. Audit value wins.

2. **Pydantic models for the on-disk format.** The boundary-types-Pydantic
   rule (P5) already applies to every typed value crossing a process
   boundary. The archive directory is a process boundary (one
   invocation writes, another reads). Pickle would dodge the
   serializer plumbing per type but fails on cross-Python-version,
   cross-refactor, and human-readability. The cost of one Pydantic
   model per phase is small — most phases already have one.

3. **No skip of `wipe_and_seed`.** Means a resume still costs ~10s of
   deterministic-phase re-execution. The alternative — preserve DB
   state across invocations — would introduce a second on-disk
   contract (the DB) running in parallel with the
   `phase_outputs/` contract. One contract is better.

4. **Hard-coded phase-dependency DAG in `debug_e2e/resume.py`.** The
   dependency graph between SDK phases is small (9 nodes, ~12 edges)
   and changes only when the pipeline composition changes. Encoding
   it as a constant in the resume module is simpler than reflecting
   it out of `run_analysis_pipeline` / `run_decision_pipeline`. A
   single import-linter rule (or unit test) keeps the constant in
   sync with the actual composition.

---

## 8. Out of scope

- **Resume in non-debug-e2e mode.** Production runs against the real
  broker have side effects (envelope dispatch in phase2) that cannot
  be replayed safely. If an operator needs a paper-mode resume, that
  is a separate design.
- **Resume across schema migrations.** If alembic head differs
  between the source run and the resume run, the resume CLI does not
  detect it. The operator is expected to re-snapshot the debug DB
  between schema changes (already covered by the runbook's
  Prerequisites § 4 pre-flight).
- **Resume across config-schema changes.** If a config field is
  renamed or removed between runs, the resume invocation may behave
  unexpectedly. Out of scope; mitigated by the deterministic-prefix
  check, which will surface most config-induced drift.
- **Partial replay within a phase.** Domain researchers run three
  sectors in parallel; if `tech_semis` succeeded but `financials`
  failed, the operator cannot replay only `financials`. The whole
  `domain_researchers` phase is the resume granularity. Acceptable
  cost: when domain researchers fail, the operator restarts from
  `domain_researchers` and re-pays for all three sectors. This
  matches the existing TaskGroup semantics where one sibling's
  failure cancels the others.

---

## 9. References

- [Debug-e2e mode design](debug-e2e-mode.md) — parent feature
  (ALP-493). The package layout, archive directory contract, and
  progress-emitter Protocol all originate there.
- [End-to-end verification runbook](../../scripts/RUNBOOK_end_to_end_verification.md)
  — operator-facing surface this feature extends.
- `src/alphamind/pipeline/analysis.py` — `run_analysis_pipeline` and
  `AnalysisPipelineResult`; the typed dataclasses that need Pydantic
  mirrors.
- `src/alphamind/pipeline/decision.py` — `run_decision_pipeline` and
  `DecisionPipelineResult`; same shape on the decision side.
- `src/alphamind/analysis/_harness_core.py` — `invoke_sdk` is where
  the replay short-circuit lives.
- `src/alphamind/scheduler/debug_e2e/` — destination package for the
  `resume.py` loader and the `ResumeContext` dataclass.
