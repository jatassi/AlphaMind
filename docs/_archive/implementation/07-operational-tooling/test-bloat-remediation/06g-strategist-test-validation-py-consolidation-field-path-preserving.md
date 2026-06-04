# 06g — strategist test_validation.py consolidation (field_path-preserving)

## Goal

Three parametrization/consolidation passes in `tests/decision/strategist/test_validation.py`, each with a verifier-corrected scope (the surveyor miscounted on all three). The unifying risk: several "duplicate" tests assert a distinct `field_path` their named siblings do **not** — dropping them as-written loses `field_path` coverage on real validation rules.

## Reading

* `tests/decision/strategist/test_validation.py` — `TestSyntheticFailScenarios` (L1074–1144), `TestLayer3PositionAssessmentReferences` (L653–851), `TestLayer3PortfolioLevelReferences` (L917–1002), and sibling classes `TestLinkedPositionAssessmentId` (L314), `TestRemedyFlagBreachPairing` (L381), `TestSectorInActiveSectors` (L568).
* `src/alphamind/decision/strategist/validation.py` — `_POSITION_NARRATIVE_FIELDS` (L327–339), `_PORTFOLIO_NARRATIVE_FIELDS` (L319–324, only FOUR names), the defensive-posture branch (L369–376).
* Parent `ALP-783`.

## Depends on

* none.

## Scope — three passes, verifier-corrected

**(1)** `TestSyntheticFailScenarios` **(4 tests).** Only `test_unresolvable_qr_99_reference` is fully subsumed (by sibling L653) — delete it. For the other three, FIRST add the one-line `field_path` assertion to the **three** siblings that lack it — `TestLinkedPositionAssessmentId::…missing_target_is_failure` (L314), `TestRemedyFlagBreachPairing` (L381), `TestSectorInActiveSectors` (L568) — THEN delete the synthetics. (Surveyor wrongly said two siblings; it is three — `sector` does NOT already assert `field_path`.)

**(2)** `TestLayer3PositionAssessmentReferences` **(7 fields).** Parametrize the **3 simple** loop-driven cases (`status_rationale` L653, `action_rationale` L668, `cross_position_observations` L838). **KEEP** the **4 action-coupled** cases (`reduce_rationale` L683, `add_conviction_justification` L704, `adjustment_rationale` L730, `remedy_rationale` L752) as-is — each wires distinct `action_parameters` (ReduceParameters / AddParameters+EntryOrder / AdjustBracketParameters / remedy_flag) and asserts a distinct message + field_path.

**(3)** `TestLayer3PortfolioLevelReferences` **(5 tests).** Parametrize only the **first 4** (`aggregate_thesis_health`, `sector_balance_shifts`, `thesis_dependency_warnings`, `capital_allocation_observations` — the `_PORTFOLIO_NARRATIVE_FIELDS` loop). **KEEP** `capital_preservation_notes` **(L978–1002) separate** — it is a distinct `defensive_posture` branch ([validation.py](<http://validation.py>) L369–376), not in the loop; folding it in drops the defensive-posture mode path.

## Acceptance criteria

- [ ] The three siblings (L314/381/568) now each assert their `field_path`; the three subsumed synthetics + `test_unresolvable_qr_99_reference` are deleted.
- [ ] The 3 simple position-assessment cases are parametrized; the 4 action-coupled cases are kept verbatim.
- [ ] The 4 loop portfolio fields are parametrized; `capital_preservation_notes` stays a separate `defensive_posture` test.
- [ ] `coverage report` for `src/alphamind/decision/strategist/` shows no newly-missing lines vs. before (proves no field_path/branch coverage lost).
- [ ] `uv run pytest tests/decision/strategist/test_validation.py -p no:xdist` green; `uv run ruff check . && uv run mypy` clean.

## Verification

Scoped pytest green; coverage diff no regression; grep confirms `field_path` assertions now present in the three siblings and `capital_preservation_notes` remains a standalone test.