# Activity log entries show class-name placeholders instead of human-readable detail content

## Symptom

The activity log fed to the strategist and PM as `=== ACTIVITY LOG (intra-invocation) ===` shows the *class name* of the detail-payload type rather than serializing the actual content. The result: a strategist or PM reading the log sees `DistillationConfigChangeDetail` instead of "config X changed from Y to Z," and `ReconciliationAlertDetail` instead of "position MSFT P/L reconciliation requested due to source mismatch."

The strategist correctly inferred from context that the `ReconciliationAlertDetail` referred to debug-pos-07's anomalous +19900% P/L (using the position-level constraint header) and held the position pending reconciliation. But the inference required cross-referencing across blocks; the activity log itself was uninformative.

## Evidence

E2E invocation: `/Volumes/Users/jacks/AlphaMind/.archive/verify-debug-e2e/invocations/inv-20260518T111140Z-7b54d0d6/`

`decision/strategist/user_message.md` lines 191-193:

```
=== ACTIVITY LOG (intra-invocation) ===
  [2026-05-18T11:11:40Z] DISTILLATION_CONFIG_CHANGE: DistillationConfigChangeDetail
  [2026-05-18T11:11:41Z] RECONCILIATION_ALERT: ReconciliationAlertDetail
```

Same shape in `decision/portfolio_manager/user_message.md` lines 712-714.

Strategist's response (`decision/strategist/response_initial.md` line 9-10) shows the inferential work required:

> *"MSFT (debug-pos-07) reports anomalous +19900% P/L at 0.0-hour age alongside an intra-invocation RECONCILIATION_ALERT — held pending reconciliation rather than acted on against unreconciled data."*

The strategist had to manually correlate `RECONCILIATION_ALERT` with `debug-pos-07` via the position-level constraint header (`[🔴 CRITICAL]` on the +19900% P/L) rather than reading the alert detail directly.

## Root cause hypothesis

The activity-log formatter is serializing the detail payload via its class repr (Python `__repr__` default) rather than calling a structured `to_log_text()` method or unpacking the fields. Likely a `f"{event_type}: {detail}"` template where `detail` is a Pydantic model whose default repr is the class name.

## Scope

Update the activity-log formatter to serialize detail payloads as their structured content rather than class repr:

* `DistillationConfigChangeDetail` → e.g., "regime_label: normal → vol_expansion; threshold_X: 0.05 → 0.10"
* `ReconciliationAlertDetail` → e.g., "position_id=debug-pos-07 (MSFT strategy), P/L source mismatch: portfolio_state=+19900%, oms=+0%"

Pydantic models can implement `__str__` or `model_dump()` and the formatter can use those.

## Acceptance criteria

- [ ] No activity-log entry contains a bare class name as its detail payload.
- [ ] DISTILLATION_CONFIG_CHANGE entries name the field(s) changed and old/new values.
- [ ] RECONCILIATION_ALERT entries name the affected position_id, the mismatched source(s), and the magnitude of the discrepancy.

## Verification

* Re-run debug-e2e; spot-check the `=== ACTIVITY LOG (intra-invocation) ===` block in strategist or PM input. Confirm detail payloads are human-readable.

## Notes

Low priority — the strategist was able to infer the reconciliation alert's target from context. But the inference is fragile (depends on a single +19900% P/L flag in the constraint header being correlated with the bare alert keyword) and won't scale to invocations with multiple simultaneous alerts.
