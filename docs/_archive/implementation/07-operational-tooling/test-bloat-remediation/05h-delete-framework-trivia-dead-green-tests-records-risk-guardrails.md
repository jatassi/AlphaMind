# 05h — Delete framework-trivia / dead-green tests (records + risk_guardrails)

## Goal

Delete the genuinely tautological framework tests the audit flagged (parent decision D): frozen-ness checks (assign a field → expect `FrozenInstanceError`/`ValidationError`, which tests `@dataclass(frozen=True)`), `MEMBER == 'MEMBER'` StrEnum echoes, `f(x) == f(x)` determinism on side-effect-free pure functions, and bare single-field construction echoes. These test the language/framework, not AlphaMind. Scoped to two areas with explicit keep-lists and a mechanical safety guard. This story owns **all** records cleanup except `test_activity_log.py` (owned by 06e), including the static-only migration markers in §4.

## Reading

* `tests/portfolio_state/records/*.py` — frozen/construct/enum trivia (EXCLUDING `test_activity_log.py`, owned by 06e).
* `tests/portfolio_state/records/test_orders.py`, `test_positions.py`, `test_capital.py` — the static-only markers + the live bogus-discriminator codec test (§4).
* `tests/risk_guardrails/regime_adaptation/test_types.py` — frozen/slots/hashable/construct trivia.
* `tests/risk_guardrails/breach_behavior/test_types.py`, `test_zones.py` — frozen trivia + `f(x)==f(x)` determinism parametrizations on pure zone functions.
* Parent `ALP-783`.

## Depends on

* none.

## Scope — delete/dedup per area

* `portfolio_state/records/`: per-record frozen-ness tests; `MEMBER == 'MEMBER'` / `len == N` enum echoes; bare single-field construction-echo tests with no validator. **Exclude** `test_activity_log.py` **entirely** (owned by 06e).
* `regime_adaptation/test_types.py`: frozen / slots / hashable / construct trivia. **KEEP the four** `__post_init__` **validator suites** (real logic).
* `breach_behavior/test_types.py` **+** `test_zones.py`: \~25 frozen tests + the `f(x)==f(x)` determinism parametrizations on pure zone functions. **KEEP the real zone-classification behavior tests** (those asserting a specific zone for a specific input).

### 4\. Records static-only markers + the mislabeled live codec test (verifier — route per-test)

* **KEEP** `test_bogus_discriminator_no_longer_validated_at_construction` **(**`test_orders.py` **L385).** It is MISLABELED as dead-green — its body imports `_instrument_spec_from_dict` and asserts it **RAISES** on `{'instrument_type': 'BOGUS'}` (a live codec-rejection assertion, the only unit test of that raise; the DB-CHECK test at `test_positions_table.py:603` is a different layer). Do not delete; optionally rename to reflect it tests the codec raise.
* **Collapse the five dead-green static-only markers into ONE parametrized "static-only validation" test** (rows: orders `spec_type` L1162, orders `direction` L879, orders `strategy_legs` L395, positions `construction_from_dict` L484, capital `zone` L317). Each currently asserts the post-Pydantic dataclass STORES the bad value unchanged (mypy-only enforcement, no runtime guard). One parametrized test preserves the "validation is static-only" design note in a single place instead of five scattered dead-green copies.

**Safety guard (mechanical):** a trivia/dead-green test may be deleted/collapsed only if doing so does **not** drop coverage of any `src/` line. Frozen-ness, determinism, and static-only storage are framework-guaranteed, so removing their tests must not reduce source-line coverage — if it does, the test was exercising real logic; keep it.

**Do NOT delete:** wire-contract enum tests (those pinning the exact JSON vocabulary the LLM agents emit) — apply the rule if encountered.

## Acceptance criteria

- [ ] The named frozen / enum-echo / determinism / construct-echo trivia tests are deleted across the listed files; `test_activity_log.py` is untouched.
- [ ] The kept `__post_init__` validators and zone-behavior tests pass.
- [ ] `test_bogus_discriminator_no_longer_validated_at_construction` (the live codec raise) is retained; the five static-only markers are collapsed into one parametrized static-only-validation test.
- [ ] `coverage report` for `src/alphamind/portfolio_state/` and `src/alphamind/risk_guardrails/` shows no newly-missing lines vs. before (proves the deletions/collapse were non-behavioral).
- [ ] `uv run pytest tests/portfolio_state/records tests/risk_guardrails/regime_adaptation tests/risk_guardrails/breach_behavior -p no:xdist` green; `uv run ruff check . && uv run mypy` clean.

## Verification

Scoped pytest green; before/after coverage diff shows zero source-line regression; `test_activity_log.py` byte-unchanged; grep confirms the bogus-discriminator codec-raise assertion survives.