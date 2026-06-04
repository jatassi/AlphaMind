# 06c — continuous_monitor composition-root kwargs-capture consolidation

## Goal

The continuous_monitor composition-root tests re-run an expensive boot and assert on captured kwargs. They look like impl-coupled `_CallLog` plumbing, but the verifier found they are the **sole guardians** of documented [ALP-453](https://linear.app/alphamind-jatassi/issue/ALP-453/continuous-monitor-production-wire-deferred-placeholder-providers) / [ALP-123](https://linear.app/alphamind-jatassi/issue/ALP-123/continuous-monitor) composition-root regressions and the surveyor's "covered by test_main" claim is **false**. Consolidate onto a shared boot fixture + a parametrized kwarg table, retaining every named assertion below verbatim.

## Reading

* `tests/execution/continuous_monitor/test_register_breach_loop.py` (L143–465) and `test_main.py` (L73–286).
* `src/alphamind/execution/continuous_monitor/__main__.py` (L852–906 composition-root wiring) and `wiring.py`.
* Parent `ALP-783`.

## Depends on

* none.

## Scope

Introduce one shared boot fixture; collapse the cheap distinct assertions onto it; parametrize the `register_breach_loop` kwarg-capture table — **retaining all assertions below**.

**PRESERVE (verifier — authoritative;** `test_main` **patches out** `_register_breach_loop`**, so it covers none of these):**

* `test_register_breach_loop.py` — the parametrized table MUST retain all **six** captured-kwarg assertions: `trigger_ids` → `CascadeDispatcher`; `trigger_ids` → `make_emergency_callback`; real `activity_log` sink (not `_no_op_sink`); `progressive_tiers` threading; `calendar_cache` market_hours (`_ClosedMarket` type); `AlpacaMarginCallObserver` wiring. Also keep `test_register_breach_loop_registers_breach_loop_task` (the only real un-patched registration check).
* `test_main.py` — keep as focused tests: `test_main_shares_trigger_id_generator_across_breach_loop_and_bracket_stops` (L\~240, the [ALP-123](https://linear.app/alphamind-jatassi/issue/ALP-123/continuous-monitor) Fix-3 client_order_id-collision regression) and `test_main_writes_pip_freeze_snapshot_under_monkeypatched_home` (L\~272–285, the archive-root-resolves-at-call-time regression); keep `test_main_rejects_unknown_subcommand`. The cheap distinct assertions (supervisor constructed, `mode=paper` log line, mode default) may share the boot fixture.

## Acceptance criteria

- [ ] The six `register_breach_loop` kwarg assertions survive (as parametrized rows); the real registration check is kept.
- [ ] `test_main_shares_trigger_id_generator`, the pip_freeze snapshot test, and `test_main_rejects_unknown_subcommand` are retained as focused tests.
- [ ] `coverage report` for `src/alphamind/execution/continuous_monitor/` shows no newly-missing lines vs. before.
- [ ] `uv run pytest tests/execution/continuous_monitor -p no:xdist` green; `uv run ruff check . && uv run mypy` clean.

## Verification

Scoped pytest green; coverage diff no regression; grep confirms all six kwarg assertions + the two named regression guards still present.