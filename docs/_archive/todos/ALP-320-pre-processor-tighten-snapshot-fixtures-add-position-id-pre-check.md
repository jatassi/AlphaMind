## Context

Surfaced as suggested-tier feedback during /review on PR #23 ([ALP-118](<https://linear.app/alphamind-jatassi/issue/ALP-118>), proposal pre-processor) and partially deferred. The reviewer recommended a runner-level pre-check that wraps a `KeyError` (from `build_held_direction_resolver`) or a `TranslatorError` (from the non-hold translator) into a uniform `BundleAssemblyError`, so structural-input bugs surface consistently with the existing halt-state and invocation_id mismatch checks.

Implementation revealed that the verify-script's snapshot constructors and one unit-test fixture already carry pre-existing inconsistencies that the strict pre-check correctly flags but the existing code path did not (the resolver only fires for hold-action assessments where an analyst recommendation exists on the same underlying — none of the existing fixtures hit that combination). Adding the pre-check uncovered \~11 test failures rooted in those incomplete snapshots, so the pre-check was rolled back to keep the review-fix iteration scope-safe.

This issue tracks the proper two-step fix.

## Scope

**(1) Tighten snapshot constructors so every position_assessment's** `position_id` resolves.

* `src/alphamind/scripts/verify_proposal_pre_processor.py` — the four scenarios' snapshot constructors omit `existing_positions` entries for hold-action assessments (e.g., POS-AAPL, POS-JPM, POS-NVDA, POS-XOM in the normal scenario). Update each constructor so `snapshot.existing_positions` covers every `position_id` referenced by `strategist_output.position_assessments[]`. Source the held-position attributes (sector, direction, asset_type, current_greeks, etc.) from the strategist fixture's pre-assessment portfolio state where it can be inferred; otherwise document the constructor inline.
* `tests/decision/proposal_pre_processor/test_runner.py::test_pending_order_assessment_order_preserved` — fixture creates pending orders SA-ORD-1/2/3 with synthesized position_ids POS-1/2/3 but the snapshot only carries POS-1. Either narrow the test's pending orders to all reference POS-1 (the simpler fix), or extend the snapshot to include POS-2 and POS-3.

**(2) Re-introduce the runner pre-check.**

In `src/alphamind/decision/proposal_pre_processor/assembler.py`, add `verify_strategist_position_ids_resolve(strategist_output, snapshot)` that raises `BundleAssemblyError` when any `strategist_output.position_assessments[].position_id` is absent from `snapshot.existing_positions`. Wire it in `runner.py::run_proposal_pre_processor` after `verify_invocation_id_consistency` and before `compute_combined_set_impact`.

The check is intentionally narrowed to `position_assessments` (not `pending_order_assessments`) because pending entry orders may legitimately reference not-yet-existing positions; the strategist's exact convention for `pending.position_id` on `entry_limit` orders for new positions is under-specified, and adding pendings to the check would couple this bug fix to that question.

## Acceptance criteria

- [ ] Each verify scenario's snapshot constructor in `src/alphamind/scripts/verify_proposal_pre_processor.py` includes an `ExistingPosition` entry for every `position_id` referenced by `strategist_output.position_assessments[]`.
- [ ] `test_pending_order_assessment_order_preserved` is updated so all referenced `position_id`s appear in the test snapshot.
- [ ] `verify_strategist_position_ids_resolve` exists in `assembler.py` and raises `BundleAssemblyError` with a message of the form `"strategist output references position_ids absent from snapshot: <sorted list>"`.
- [ ] The runner calls the new verifier after `verify_invocation_id_consistency`.
- [ ] `tests/decision/proposal_pre_processor/test_assembler.py` adds a passing/raising pair for the new verifier.
- [ ] `tests/decision/proposal_pre_processor/test_runner.py` adds a test that an unknown `position_id` in `position_assessments` raises `BundleAssemblyError` from the runner.
- [ ] `uv run python scripts/verify_proposal_pre_processor.py` reports ALL PASS.
- [ ] `uv run pytest -n auto` clean, lint chain clean.

## Reference

* Original /review pass: PR #23 (proposal pre-processor work tree).
* Reviewer's exact framing: a strategist `hold` referencing a `position_id` absent from `snapshot.existing_positions` raises a bare `KeyError` from `build_held_direction_resolver`; a non-hold assessment with the same gap raises `TranslatorError`. A runner pre-check raising `BundleAssemblyError` mirrors the existing `verify_halt_state_consistency` / `verify_invocation_id_consistency` checks.
* Rolled-back commits: see history on PR #23 — the pre-check was added then removed before merge.