---
status: in_progress
completed_date:
commit_id:
---

# 14b — No-magic-numbers code audit test

## Goal

Implement the static audit that enforces the threshold-calibration design doc's "Code constants — none" promise. Every Class A threshold value must reach the code only through the loaded `DistillationConfig` object; none of the documented numeric defaults may appear as literals inside `src/alphamind/distillation/` source files. Land the audit as a pytest test so the discipline is enforced at every CI run, not as one-time grep hygiene.

## Reading

- `docs/design/02-distillation-layer/threshold-calibration.md` § Where each threshold lives — the "Code constants — none" paragraph naming the discipline this test enforces
- `docs/design/02-distillation-layer/threshold-calibration.md` § Static configuration thresholds — the authoritative list of values being audited
- `docs/design/02-distillation-layer/threshold-calibration.md` § Validation invariants — invariants enforced by the config loader, mentioned here because the audit complements them (loader catches malformed configs; audit catches bypassed configs)
- `config/distillation.yaml` — the file the audit reads to compute its scan targets
- `src/alphamind/distillation/` — the source tree the audit scans
- Story `02-config-schema.md` — establishes the YAML and the `DistillationConfig` Pydantic model the audit reads from

## Depends on

- 02 (Distillation config schema) — `config/distillation.yaml` and the `DistillationConfig` model are the audit's source-of-truth inputs.

## Scope

In scope:

- A pytest test under `tests/distillation/test_no_magic_numbers.py`:

  1. **Load the documented thresholds.** Read `config/distillation.yaml` via the existing `load_config()` aggregate (or directly via PyYAML if `load_config()` is not yet wired into the test fixture environment). Flatten into a list of `(key_path, value)` tuples covering every Class A threshold.

  2. **Build a value set.** From the flattened list, produce a `set[Decimal | int | str]` of all distinct values. Keep types intentional: a `2.5` σ threshold and a `2` count threshold are distinct values; a boolean (`regime_skip_emergency_trigger = true`) is excluded from the audit because booleans are not numeric magic numbers and would produce too many false positives. Also exclude values in a small allowlist: `0`, `1`, `0.0`, `1.0`, `100`, `100.0`, `True`, `False` — these are pervasive in Python code and almost never represent a Class A threshold even when the values overlap.

  3. **Walk the source tree.** Recursively enumerate every `*.py` file under `src/alphamind/distillation/`. Exclude empty package stubs (files containing only docstring + `__all__` declarations).

  4. **Parse each file with the `ast` module.** Walk the AST collecting every `ast.Constant` whose `value` is `int`, `float`, or `Decimal`-like. For each constant, capture the file path, line number, and the literal value.

  5. **Match against the value set.** For each captured constant, check whether its value is in the allowlist; if not, check whether it appears in the threshold value set. A match is a candidate violation.

  6. **Apply context exclusions.** A captured constant is exempted from the violation list if any of:
     - The file is a test file (`tests/...`).
     - The line carries a `# noqa: TC001` or equivalent suppression comment AND that suppression is recorded in an explicit allowlist file at `tests/distillation/no_magic_numbers_allowlist.txt` (one line per exemption, with `<file>:<line>:<value>:<reason>` format). The allowlist must be reviewed in code review; growth signals discipline erosion. Per CLAUDE.md, additions to the allowlist require alerting the operator before merge.

     Pydantic field defaults in `src/alphamind/config/models.py` are NOT exempted — every field is `Field(...)` with the value supplied at load time, no fallback. Literal defaults in `models.py` are violations.

  7. **Assert empty violation list.** The test fails with a clear report listing every `(file, line, value, matched_yaml_key)` tuple if the violation list is non-empty.

- A small per-violation report formatter that produces output like:

  ```
  Magic-number violations: 2

    src/alphamind/distillation/regime.py:47
      literal value 14.0 matches config key regime_classification.regime_low_vol_vix_max
      remediation: read the value via config.distillation.regime_classification.regime_low_vol_vix_max

    src/alphamind/distillation/anomalies.py:112
      literal value 2.5 matches config key anomaly_detection.volume_anomaly_sigma
      remediation: read the value via config.distillation.anomaly_detection.volume_anomaly_sigma
  ```

- A README block in `tests/distillation/README.md` (extending the directory's existing test README if one exists) documenting:
  - Why the audit exists (cross-reference threshold-calibration.md § Where each threshold lives).
  - How to add an entry to the allowlist (and that doing so requires reviewer scrutiny per CLAUDE.md).
  - The allowlist file format.

Out of scope:
- Auditing thresholds owned outside threshold-calibration.md (e.g., per-rule guardrail limits in `06-risk-guardrails/rules-and-limits.md`). Those have their own canonical home; a parallel audit story can land later if needed.
- Auditing magic numbers that don't match any threshold value (general-purpose magic-number lint). Ruff and existing lints handle that surface; this audit is specifically about Class A bypass.
- Modifying source code that currently violates the audit. The audit's first-run output is the spec; remediation is a follow-up either by this story or by separate per-violation stories.
- Auditing computed compositions (e.g., a `2 * volume_anomaly_sigma` expression). The audit is purely static literal-matching; semantic equivalence is out of scope.

## Notes

The "no Pydantic literal defaults" rule is the load-bearing design choice. Two options exist:
1. **Allow** literal defaults in `models.py`. Pro: code-side defaults provide a fallback if the YAML is missing a key. Con: the YAML stops being the authoritative source — values can drift between YAML and code.
2. **Forbid** literal defaults in `models.py`. Every field is required; YAML is authoritative; loader fails closed if a key is missing.

Option 2 matches the threshold-calibration.md spec ("Code constants — none") more strictly. Adopted. The audit thus treats `models.py` literals the same as any other source file's literals — they are violations.

Caveat: the YAML keys' default *values* still come from the design doc. The Pydantic model declares fields; the YAML carries values. If the YAML is missing a key, the loader raises (per story 02). This story enforces the second leg: even if the YAML carries the right value, the code paths reading the value must go through the loader, not duplicate the literal.

The exemption for tests is necessary — fixture data carries threshold-relevant values, and the tests themselves often reference values from the design doc to assert behavior. The cost is that a stale fixture value silently disagrees with the YAML; we accept this because the production behavior is governed by the loader path which the test exercises end-to-end.

The allowlist mechanism is the escape hatch for legitimate overlaps (e.g., a percentile constant `90` shared between `macro_surprise_percentile` and an unrelated computation that happens to use the digit). Per CLAUDE.md the user wants to be alerted before any suppression is added; the allowlist file is the visible record of those alerts.

The audit runs once per pytest invocation. Performance: 65 universe tickers × 4 baseline kinds × hundreds of source files × thousands of literals — the total surface is small (low thousands of literals), and the AST walk is microsecond-scale. No need for caching.

The audit must NOT depend on per-category indicator stories (08*) being implemented for it to run cleanly; if no per-category source files exist yet, the audit trivially passes against the empty source set. It catches violations as soon as they land.

The deliberate design choice of letting the YAML rather than code carry the values means that if the operator edits the YAML to a non-default value, the audit still passes (the audit doesn't care what the YAML says — it cares about literals in code matching whatever the YAML says at the moment of test). This is correct: the audit enforces "no code bypass," not "the YAML matches the design-doc defaults." A value-vs-doc cross-check is a different audit, out of scope here.

## Acceptance criteria

- [ ] `tests/distillation/test_no_magic_numbers.py` exists and is collected by `pytest`.
- [ ] The test loads `config/distillation.yaml`, flattens it into `(key_path, value)` tuples, and builds a value set excluding the documented allowlist (`0`, `1`, `0.0`, `1.0`, `100`, `100.0`, `True`, `False`).
- [ ] Boolean threshold values (e.g., `regime_skip_emergency_trigger`) are excluded from the audit set.
- [ ] The test recursively walks `src/alphamind/distillation/`, parses every `.py` file via `ast`, and collects integer/float literals with their file paths and line numbers.
- [ ] Pydantic model defaults in `src/alphamind/config/models.py` are NOT exempted — they are subject to the same audit.
- [ ] Test files under `tests/` are exempted.
- [ ] The exemption allowlist at `tests/distillation/no_magic_numbers_allowlist.txt` is the only escape hatch; entries follow the documented `<file>:<line>:<value>:<reason>` format.
- [ ] On a violation, the test fails with a report listing every `(file, line, value, matched_yaml_key)` tuple.
- [ ] On a clean source tree, the test passes.
- [ ] A unit-style fixture test: a synthetic `.py` file containing a known violation produces the expected violation report (validates the audit's matching logic against a controlled input).
- [ ] A unit-style fixture test: a synthetic `.py` file with an allowlisted line (and a corresponding allowlist entry) does not produce a violation.
- [ ] `tests/distillation/README.md` documents the audit's purpose, allowlist format, and the CLAUDE.md alerting requirement.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
