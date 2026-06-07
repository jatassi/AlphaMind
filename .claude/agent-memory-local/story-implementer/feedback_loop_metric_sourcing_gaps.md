---
name: feedback-loop-metric-sourcing-gaps
description: Decision-layer process metrics whose design-doc source field is NOT persisted into WindowDataset's existing bundles — verify before implementing
metadata:
  type: project
---

The feedback-loop design doc (`docs/design/feedback-loop.md` § Decision layer) lists
metrics whose stated computation references fields that are **not** actually reachable
from the `WindowDataset` bundles a metric core may read (`pm_decision_log` =
`PMDecisionDetail` activity-log entries; `agent_calls` = `AgentCallRecord` telemetry).

Three known gaps (confirmed 2026-06-07 implementing ALP-883):

1. **Anti-pattern frequency** — design says "counts per `anti_patterns_identified`".
   But `PMEnvelope.anti_patterns_identified` is **dropped at emission time**:
   `execution/write_paths/command_execution/__init__.py::_emit_pm_decision` builds
   `PMDecisionDetail` without it. The codec encodes via `dataclasses.fields()`, so the
   field never lands in `activity_log.detail_json`. (Frontend `pm-pane.tsx` reads
   `anti_patterns?` optimistically — it is always undefined.) Raw value lives only
   behind `agent_calls.output_artifact_ref` (story 06d's RefsBundle seam, pre-declared
   empty).

2. **Analyst inaction rate** ("zero-proposal invocations / total") and
3. **Analyst proposals per invocation** — both need analyst Recommendation counts
   *per invocation, including zero-proposal invocations*. `pm_decision_log` is keyed on
   PM decisions, so an invocation where the analyst proposed nothing leaves no row and
   is invisible; `agent_calls` carries no proposal count. Not computable without a new
   per-invocation proposal-count read surface.

**Why:** A new metric story's concurrency note may forbid touching `dataset.py` /
the loader. These gaps then become a STOP-and-report blocker, not a workaround.

**How to apply:** Before promising a decision-layer metric, trace the design-doc source
field to the *persisted* shape (PMDecisionDetail fields:
`envelope_id, source_provenance_json, evaluation_json, modifications_json,
resulting_command_ids, verdict, originating_proposal_json, reprice_markers_json`).
`originating_proposal_json` DOES carry the analyst `Recommendation` / strategist
`PositionAssessment` body (so conviction_level, thesis_status, prior_status,
recommended_action ARE reachable). Implement the computable subset; report the rest.
