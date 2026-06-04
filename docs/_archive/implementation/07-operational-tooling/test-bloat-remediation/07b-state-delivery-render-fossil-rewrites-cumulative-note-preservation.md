# 07b — state_delivery render-fossil rewrites + cumulative-note preservation

## Goal

`test_portfolio_manager.py` carries \~25 per-block "renders X" tests that each re-invoke the full renderer to substring-check one block already pinned byte-for-byte by `test_render_pm_header_micro_fixture_full_render`. Delete the per-block fossils in favor of the full-fixture exact renders — while preserving the branch tests and one sole-guardian cumulative-note sequence the verifier flagged.

## Reading

* `tests/risk_guardrails/state_delivery/test_portfolio_manager.py` — the per-block render tests + the micro-fixture full render.
* `tests/risk_guardrails/state_delivery/test_end_to_end.py` — `TestComposition::test_full_invocation_simulation` (L1944).
* `tests/risk_guardrails/state_delivery/test_validation_tool.py` — `test_validation_tool_cumulative_tracking` (L1748), and L515 (pins only `#1-1`).
* Parent `ALP-783`.

## Depends on

* `05d` ([ALP-791](https://linear.app/alphamind-jatassi/issue/ALP-791/05d-hoist-risk-guardrailsstate-delivery-fixture-builders-to-a-shared)) — the state_delivery builder hoist lands first (same directory).

## Scope

* Delete the per-block "renders X" fossils in `test_portfolio_manager.py` subsumed by the full-fixture exact render. **KEEP** the branch tests the verifier named: negative-P&L proximity, BLOCKED-by-size, empty-held.
* `test_end_to_end.py::test_full_invocation_simulation` (L1944): this 95-line test re-runs the AAPL/NVDA/GOOG validation scenario already in `test_validation_tool_cumulative_tracking`. **PRESERVE** the one unique thing it guards — the `cumulative_impact_note` text SEQUENCE across three proposals (`'No prior proposals'` → `'Cumulative impact of proposals #1-1'` → `'#1-2'`, L2020). That `#1-2` three-proposal note appears nowhere else (grep-confirmed). Either strip the redundant render tail and keep the note-sequence assertions, OR migrate the `#1-2` coverage into `test_validation_tool.py` before deleting. Do not just delete.

## Acceptance criteria

- [ ] The per-block render fossils are gone; the full-fixture exact render is the renderer's guard; the named branch tests (negative-P&L proximity, BLOCKED-by-size, empty-held) are retained.
- [ ] The `cumulative_impact_note` `#1-1` → `#1-2` sequence assertion survives (in `test_end_to_end.py` trimmed, or migrated to `test_validation_tool.py`).
- [ ] `coverage report` for `src/alphamind/risk_guardrails/state_delivery/` shows no newly-missing lines vs. before.
- [ ] `uv run pytest tests/risk_guardrails/state_delivery -p no:xdist` green; `uv run ruff check . && uv run mypy` clean.

## Verification

Scoped pytest green; coverage diff no regression; grep confirms the `#1-2` cumulative-note assertion still exists somewhere.