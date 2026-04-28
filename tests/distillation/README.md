# Distillation tests

Tests covering the distillation layer's runtime behavior, persistence schema,
and configuration discipline.

## No-magic-numbers audit

`test_no_magic_numbers.py` enforces the rule stated in
[`docs/design/02-distillation-layer/threshold-calibration.md`](../../docs/design/02-distillation-layer/threshold-calibration.md)
section "Where each threshold lives": every Class A threshold reaches the code
through the loaded `DistillationConfig`, never as a numeric literal in source.

### What the audit checks

The audit reads `config/distillation.yaml`, flattens it into
`(key_path, value)` tuples, and builds a value index keyed by the literal
threshold values (excluding pervasive values `0`, `1`, `0.0`, `1.0`, `100`,
`100.0`, and all booleans). It AST-walks every `*.py` under
`src/alphamind/distillation/` plus `src/alphamind/config/models/distillation.py`
and reports any integer or float literal whose value matches a threshold.

A match means the code carries the threshold value as a literal — a Class A
bypass that lets the YAML and the code drift apart. The fix is to read the
value through the loaded config object.

`models/distillation.py` is in scope because every Pydantic field in the
distillation schema is required (`Field(...)` with no default). A literal
default added in the model file would be a Class A bypass exactly as if it
appeared in `distillation/calibration.py`.

Test files under `tests/` are exempted; fixture data legitimately references
threshold values to assert end-to-end behavior.

### Allowlist

The allowlist at `no_magic_numbers_allowlist.txt` is the only escape hatch.

**Format**: one entry per line as

```
<repo-relative-file>:<line>:<value>:<reason>
```

- `<file>` is the repo-relative POSIX path (e.g.
  `src/alphamind/distillation/calibration.py`).
- `<line>` is the literal's source line number, copied from the audit's
  failure report.
- `<value>` matches the literal's repr exactly (`2.5`, `90`, `5.0`).
- `<reason>` is free-text justification.

Blank lines and lines starting with `#` are ignored.

### Adding an allowlist entry requires operator alert

Per `CLAUDE.md` ("Alert the user before disabling the linter or any rule in
any form"), every allowlist addition is a rule suppression and the operator
must be alerted before the change merges. Growth of this file signals
discipline erosion: the audit is the visible enforcement of
`threshold-calibration.md`'s "Code constants — none" promise, and entries
weaken that promise.

When you find yourself reaching for the allowlist, prefer threading the value
through the loader instead. Exemptions are reserved for cases where a literal
genuinely does not represent the threshold it numerically resembles (e.g., an
unrelated computation that happens to use the digit `90`).
