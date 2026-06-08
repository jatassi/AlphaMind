---
name: windowdataset-no-inmemory-week-slicing
description: feedback_loop WindowDataset cannot be sliced per-week purely in-memory — agent_calls and ThesisOutcome carry no per-record timestamp; the trailing-multi-week digest trajectory (ALP-888 / 07a) needs an upstream dataset/loader change
metadata:
  type: project
---

The weekly-digest generator (ALP-888 / story 07a) is specified to load ONE trailing
multi-week `WindowDataset` (feedback_loop/dataset.py) and **slice it per-week** to feed
each metric's pure `compute()`, producing 8-12-week trajectory sparklines + WoW deltas.
The determinism AC ("re-generating from the same WindowDataset yields an identical
WeeklyDigest") forces the generator to be a pure function of one dataset object — so the
per-week slicing must happen in-memory, not by re-querying the DB per week.

**It can't, as the dataset stands.** The records needed for per-week bucketing don't
carry the per-record timestamp:

- `AgentCallRecord` (state/tables/agent_calls.py) has **no timestamp field** — its
  window membership is established only by the DB invocation-join in
  `read_agent_calls_in_window`. dataset.py's own docstring says agent_calls is "strictly
  bounded to [start,end) via the invocation join" — i.e. not reconstructable in-memory.
  Process-pulse + pl_per_token metrics read `dataset.agent_calls`.
- `ThesisOutcome` (the `outcomes.theses` bundle) **drops the resolution timestamp**,
  keeping only `active_duration_hours`. Win-rate / calibration / P-L trajectory metrics
  read `dataset.outcomes` — so the outcome-tier trajectory can't be weekly-bucketed
  either.
- `pm_decision_log` does carry `ActivityLogEntry.timestamp`, but it's a count-based
  sliding window (NOT clipped to the load window).
- `validations` is the point-in-time PENDING set only.

**Why:** the authorized single loader extension for 07a covers only the
`validation_superseded` detector (`read_validations_superseded_in_window` +
`superseded_validations` field). It does NOT cover adding a timestamp to
`AgentCallRecord`/`ThesisOutcome` or an invocation->time map to the dataset — those are
story-05-owned upstream-contract changes, and a concurrent sibling (06e) is editing
dataset.py's replays region.

**How to apply:** if you pick up 07a, this is the blocker to resolve with the operator
FIRST. Options: (a) extend the dataset with per-week-bucketable timestamps (resolution
timestamp on ThesisOutcome + a per-call/invocation timestamp on the agent_calls bundle)
— a story-05 contract change; or (b) redefine the generator's input as a *sequence* of
per-week WindowDatasets (one load_window per week), which keeps the generator pure but
changes the "from one loaded dataset" + determinism framing. Don't silently degrade the
trajectory to a single repeated point — AC1 explicitly requires "a multi-week
trajectory".
