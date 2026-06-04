# 06h — data_sources write-decomposition collapse + protocol-seam pruning

## Goal

Across the vendor adapters, per-field write tests decompose one `collect_*` call into one test per written column (e.g. `tests/data_sources/eia/test_eia_adapter.py` runs `collect_series` once per column), and Protocol-conformance `hasattr` / seam tautologies add no behavioral coverage. Collapse the per-field write-decomposition into one row-assertion test per adapter; prune the protocol-seam tautologies. Vendor-payload parse fixtures stay — those legitimately need real payloads.

## Reading

* `tests/data_sources/**/*.py` — the per-field write tests + protocol-seam `hasattr` tests.
* `tests/data_sources/finnhub/test_estimate_revisions.py` — see the explicit preserve-list (heterogeneous bundle).
* Parent `ALP-783`.

## Depends on

* none.

## Scope

* **Write-decomposition:** per adapter, collapse the per-column write tests into one test that asserts the full written row in a single `collect_*` invocation. Keep the parse/mapping assertions.
* **Protocol seams:** delete/parametrize the Protocol-conformance `hasattr` and seam tautology tests (they assert structural typing the type-checker already enforces).

**PRESERVE (verifier —** `finnhub/test_estimate_revisions.py`**, heterogeneous):**

* DROP only `test_no_separate_client_file_created` (L188) — a genuine permanently-green TDD-refactor fossil.
* KEEP the column-set guard (`test_table_creation_succeeds` L159 + `test_model_has_required_columns` L164) — the collect tests assert most columns but **not** `revised_at` / `source` / `ingested_at`, so this is a weak-but-live guardrail.
* KEEP `test_fetch_eps_estimates_calls_company_eps_estimates` (L896) and its revenue twin (L915) — they assert the [ALP-405](https://linear.app/alphamind-jatassi/issue/ALP-405/finnhubestimate-revisions-attributeerror-client-has-no-attribute) method-name routing (`eps_calls == ['AAPL']`) **and** the `response['data']` unwrap; `TestFirstObservation` drives the path but asserts neither.
* KEEP the real-SDK `hasattr` drift guards (L880 / L888).

**Safety guard:** any deletion must not drop coverage of the corresponding `src/alphamind/data_sources/` lines.

## Acceptance criteria

- [ ] Per-adapter write tests collapsed to one full-row assertion each; parse/mapping assertions retained.
- [ ] Protocol-seam `hasattr` tautologies pruned.
- [ ] The finnhub preserve-list is honored exactly (only the one fossil dropped).
- [ ] `coverage report` for `src/alphamind/data_sources/` shows no newly-missing lines vs. before.
- [ ] `uv run pytest tests/data_sources -p no:xdist` green; `uv run ruff check . && uv run mypy` clean.

## Verification

Scoped pytest green; coverage diff no regression; grep confirms the four finnhub preserve-tests still present.