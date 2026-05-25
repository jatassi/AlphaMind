## Problem

When a PM agent calls `submit_envelope` with a payload that fails Pydantic (Layer-1) validation, the rejection is returned to the agent but **no record of the attempt is persisted** to `submission_log`. Forensics of failed PM invocations is impossible — the archive looks identical to a run where the agent never called the tool.

## Code path

`src/alphamind/execution/oms/submit_envelope_mcp.py:323-345` — `_handle_submit_envelope` calls `_build_envelope_level_rejection` with `log_envelope_for_record=None` on Pydantic `ValidationError`:

```python
try:
    envelope = _validate_envelope_payload(args)
except ValidationError as exc:
    return _build_envelope_level_rejection(
        envelope_id=str(args.get("envelope_id", "ENV-REC-INVALID")),
        invocation_id=state.invocation_id,
        suggested_modification=_format_first_error(exc),
        log_state=state,
        log_envelope_for_record=None,  # ← discarded
    )
```

`_build_envelope_level_rejection` (lines 449-455) appends to `state.submission_log` only when `log_envelope_for_record is not None`:

```python
if log_envelope_for_record is not None:
    log_state.submission_log = (
        *log_state.submission_log,
        SubmissionLogEntry(
            envelope=log_envelope_for_record, submission_results=submission_results
        ),
    )
```

So Layer-2/3 failures are logged (envelope was parsed; we have a `PMEnvelope` to record), but Layer-1 failures vanish.

## Evidence

`.archive/verify-pipeline-20260504/invocations/20260505T172913Z-verify-pm-normal/decision/portfolio_manager/`:

* `metadata.json`: `tool_calls_used: 10`, `success: true`, `stop_reason: end_turn`
* `submission_log.json`: `[]`
* `response_initial.md`: agent narrates submitting 4 envelopes, all rejected at the schema-discriminator layer

The 4 attempts and their rejection reasons are entirely absent from the post-hoc archive. Reconstructing them requires reading the response narrative — fragile and not machine-parseable.

## Fix options

1. **(Preferred) Parallel raw-args log.** Add a `failed_submission_log: tuple[FailedSubmissionEntry, ...]` field to `SubmitEnvelopeState` carrying `(raw_args: dict, validation_error_repr: str, command_id: str)` per Layer-1 failure. Serialize alongside `submission_log.json` from `harness.py:479-488` as `failed_submission_log.json`.
2. **Unified log with optional envelope.** Change `SubmissionLogEntry.envelope` to `PMEnvelope | None` and persist Layer-1 failures with `envelope=None`, raw args attached. Simpler structure; downstream readers must handle the None case.

Either way, ensure `harness.py` serialization picks up the new track so the archive is complete.

## Why now

Without this, the PM prompt-vs-schema mismatch (sibling To-do issue) is invisible in the archive — you have to read `response_initial.md` to know envelopes were even attempted. Any future Layer-1 regression is similarly invisible.