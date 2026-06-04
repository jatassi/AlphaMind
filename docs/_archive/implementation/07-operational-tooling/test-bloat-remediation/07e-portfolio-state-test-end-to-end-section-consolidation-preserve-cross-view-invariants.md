# 07e — portfolio_state test_end_to_end section consolidation (preserve cross-view invariants)

## Goal

`tests/portfolio_state/test_end_to_end.py` has four large sections with partial overlap against `test_assembler.py`. The verifier found each section guards assertions with no equivalent elsewhere, so this is surgical regrouping, not bulk deletion.

## Reading

* `tests/portfolio_state/test_end_to_end.py` — Section B (L149–269), Section C (L277–376), Section F (L569–632), Section G (L640–795).
* `tests/portfolio_state/test_assembler.py` — the partial subsumers (`_FailingRepository`, `_ConsistencyErrorRepository`, `test_empty_portfolio_returns_valid_snapshot`, the enrichment tests).
* Parent `ALP-783`.

## Depends on

* none.

## Scope — per section, verifier-scoped

* **Section G (repository errors):** delete the 2 stub classes (`_RaisingRepositoryReadError`, `_RaisingRepositoryConsistencyError` — near-dupes of `test_assembler`'s) and the 2 redundant propagation tests (`read_error_propagates`, `consistency_error_propagates`, covered in `test_assembler`). **KEEP** `test_repository_read_error_is_not_wrapped` and `…_consistency_error_is_not_wrapped` — they assert the exact exception type is unwrapped and the message string survives; `test_assembler` has no "not wrapped" / message variant.
* **Section F (empty portfolio, 14 tests):** regroup to \~3 tests, **preserving** the distinct view-projection assertions (synthesizer/analyst/strategist/pm projections succeed, empty sector dict, zero gross/pnl) — `test_assembler`'s empty test is snapshot-only and does not exercise these.
* **Section C (assembler correctness, 9 tests) + Section B (cross-view consistency, 16 tests):** regroup into 4–6 tests each but **preserve every distinct assertion** — do NOT bulk-delete. Section B's cross-view invariants (synth sector keys ⊆ strat sectors; synth gross == strat gross within 1e-9; synth/strat/pm position-id-set equality; analyst hides `unrealized_pnl`/`thesis`; information-hiding `hasattr` checks) are the ONLY tests asserting the four consumer projections agree — nothing in `test_assembler` covers them. Section C's NVDA/AMD/JPM MV, per-direction unrealized PnL, weight=abs(mv)/total, gross formula, cash_pct, true_deployable use different fixtures from `test_assembler`.

## Acceptance criteria

- [ ] Section G: the 2 stub classes + 2 propagation dupes are gone; the 2 "not-wrapped" message-asserting tests are retained.
- [ ] Section F regrouped to \~3 tests with all distinct empty-portfolio projection assertions preserved.
- [ ] Sections B and C regrouped (not bulk-deleted); every cross-view invariant + distinct numeric assertion survives.
- [ ] `coverage report` for `src/alphamind/portfolio_state/` shows no newly-missing lines vs. before.
- [ ] `uv run pytest tests/portfolio_state/test_end_to_end.py tests/portfolio_state/test_assembler.py -p no:xdist` green; `uv run ruff check . && uv run mypy` clean.

## Verification

Scoped pytest green; coverage diff no regression; grep confirms the Section B cross-view invariants and the Section G "not-wrapped" assertions survive.