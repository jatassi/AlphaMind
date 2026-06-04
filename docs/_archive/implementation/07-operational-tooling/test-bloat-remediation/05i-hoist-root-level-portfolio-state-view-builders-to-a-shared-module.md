# 05i — Hoist root-level portfolio_state view builders to a shared module

## Goal

The view-level builders (`_make_open_position`, `_make_pending_position`, `_make_cash_ledger`, `_make_drawdown_state`, `_make_snapshot`, `_make_active_risk_parameters`, `_make_portfolio_pnl`, `_make_directional_exposure`) are redefined across the root `tests/portfolio_state/` files — `test_freshness.py` (8), `test_snapshot.py` (6), `test_repository.py` (3), `test_assembler.py` (4) — with drifted signatures (`position_id` vs `pos_id`, NVDA vs AAPL defaults). Hoist them into one shared view-level builder module and reconcile the signatures. Pure test-infra dedup.

## Reading

* `tests/portfolio_state/test_freshness.py`, `test_snapshot.py`, `test_repository.py`, `test_assembler.py` — the duplicated view builders.
* `tests/portfolio_state/_fixtures.py` — confirm it is a DIFFERENT surface (see preserve note); do not use it as the target.
* Parent `ALP-783`.

## Depends on

* none.

## Scope — root files only

* Create a NEW shared view-level builder module — e.g. `tests/portfolio_state/_view_builders.py`. Move the view-level builders there; reconcile the drifted signatures into parametrized builders with sensible defaults; update the four root files to import them; delete the local copies.
* Hoist only — no assertion changes.

**PRESERVE (verifier — do NOT route through** `_fixtures.py`**):** `_fixtures.py` builds `RepositoryFixture` 5-tuples that flow through `assemble_snapshot`; the freshness/snapshot tests construct VIEW-level objects directly (a `PositionView`, a fully-built `PortfolioStateSnapshot`) to call `compute_snapshot_freshness` on a hand-assembled snapshot. These are different abstraction surfaces — routing the view-level tests through `_fixtures.py` would force them through the whole assembler. Make a new view-level module; leave `_fixtures.py` untouched.

### Out of scope

* The `tests/portfolio_state/consumers/` copies — owned by 05a. (A future follow-up could unify the consumers' shared module and this one; not this story.)

## Acceptance criteria

- [ ] A new shared view-builder module holds the reconciled builders; the four root files import them; no view-builder is duplicated across those four files.
- [ ] `tests/portfolio_state/_fixtures.py` is byte-unchanged.
- [ ] `coverage report` for `src/alphamind/portfolio_state/` shows no newly-missing lines vs. before.
- [ ] `uv run pytest tests/portfolio_state -p no:xdist` green; `uv run ruff check . && uv run mypy` clean.

## Verification

Scoped pytest green; coverage diff no regression; `git diff --stat` shows `_fixtures.py` untouched.