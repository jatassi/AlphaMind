# 01f — MLEG strategy-close leg intent

# 01f — MLEG strategy-close leg intent

## Goal

Make the strategy CLOSE path carry **close-side legs end-to-end**, with the open→close leg inversion happening exactly once at a single, well-named seam.

> **Premise correction (2026-05-20).** The original filing claimed a strategy CLOSE "submits opening orders instead of closing ones" — this is wrong. The pre-existing path is already broker-correct: `_engine_close_dispatch_kwargs` emits open-side `MLEGLegAck` legs, and `_build_close_legs` inverts them to close-side (via `_OPEN_TO_CLOSE_INTENT` / `_OPEN_TO_CLOSE_SIDE`) before broker submission. There is no broker bug. The real issue is structural: the inversion is buried inside `_build_close_legs`, and the close path threads *open-side* legs through a parameter named `open_legs`, so a strategy CLOSE's leg representation is open-side everywhere upstream of the broker adapter. The operator rescoped this story (2026-05-20) as a clean refactor — carry close-side legs end-to-end, invert exactly once. The parent [ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) Current-state table row for `submit_engine_envelope.py:484` is incorrect on this point.

## Reading

* `src/alphamind/execution/oms/submit_engine_envelope.py` — `_engine_close_dispatch_kwargs`; its `StrategyPositionDetails` branch builds the per-leg `MLEGLegAck`s, and the `leg.direction is None` → `ValueError` guard lives there.
* `src/alphamind/execution/broker_adapter/order_mleg.py` — `submit_mleg_close` (`open_legs` parameter), `_build_close_legs` (inverts open→close, rejects non-`*_to_open` intents), `_build_open_legs` / `_build_scaled_open_legs` (OPEN / ADD paths — out of scope), `_SIDE_ENUM_BY_LITERAL` / `_INTENT_ENUM_BY_LITERAL`.
* `src/alphamind/execution/oms/broker_dispatch.py` — `dispatch_command_to_broker` and its CLOSE helper thread the `open_legs` kwarg to `submit_mleg_close`.
* `src/alphamind/execution/continuous_monitor/bracket_stops/wiring.py` — the bracket-stop strategy-close path calls `submit_mleg_close`.
* The PM-originated close path (`_close_command_context`) also feeds `dispatch_command_to_broker`.
* `src/alphamind/portfolio_state/records/positions.py` — `StrategyPositionDetails.legs`, `StrategyLeg.direction`. The `MLEGLegAck` model — `side` (`buy`/`sell`), `ratio_qty`, `position_intent`.
* [ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) (parent) § Pre-resolved decision (D).

## Depends on

Nothing. Wave 1.

## Scope

In scope: the strategy-CLOSE leg path across `submit_engine_envelope.py`, `order_mleg.py`, `broker_dispatch.py`, `bracket_stops/wiring.py`, and the PM close path. Tests under `tests/execution/oms/`, `tests/execution/broker_adapter/`, and `tests/execution/continuous_monitor/`.

**(1) Single inversion seam.** A shared helper converts a position's open-side strategy legs (`StrategyPositionDetails.legs`, each carrying `direction`) into a tuple of close-side `MLEGLegAck` legs — a leg opened LONG closes `sell` / `sell_to_close`, a leg opened SHORT closes `buy` / `buy_to_close`. The `leg.direction is None` → `ValueError` guard lives in this seam.

**(2) Engine close path.** `_engine_close_dispatch_kwargs`'s strategy branch builds close-side legs via the seam, with no re-inversion helper.

**(3) Broker adapter.** `submit_mleg_close` receives close-side legs — its `open_legs` parameter is renamed to close-side semantics. `_build_close_legs` stops inverting: it becomes a straight `MLEGLegAck → OptionLegRequest` translation of close-side legs (mirroring `_build_scaled_open_legs`), and its validation guard rejects non-`*_to_close` intents. `_build_open_legs` and `_build_scaled_open_legs` are unchanged.

**(4) Dispatch threading.** `broker_dispatch.py`'s `open_legs` kwarg through `dispatch_command_to_broker` and its CLOSE helper is renamed consistently to close-side semantics.

**(5) Other callers.** The bracket-stop path (`bracket_stops/wiring.py`) and the PM close path (`_close_command_context`) produce close-side legs via the same shared seam.

### Out of scope

Strategy OPEN / ADD leg construction (`_build_open_legs`, `_build_scaled_open_legs`); equity and single-option close intents; the broker SDK boundary itself.

### Surfacing condition

If the PM-path conversion has a much wider blast radius than the engine + wiring paths — close-side legs would need to thread through many PM-layer functions or change a PM-layer contract — the orchestrator scopes the PM path separately rather than forcing it into this story.

## Acceptance criteria

- [ ] A single shared seam converts open-side strategy legs to close-side `MLEGLegAck` legs (LONG → `sell`/`sell_to_close`, SHORT → `buy`/`buy_to_close`); a leg with `direction is None` raises `ValueError`.
- [ ] `_engine_close_dispatch_kwargs` emits close-side legs via the seam and contains no re-inversion helper.
- [ ] `submit_mleg_close` receives close-side legs (parameter renamed from `open_legs`); `_build_close_legs` performs a straight close-side `MLEGLegAck → OptionLegRequest` translation with no inversion and rejects a non-`*_to_close` intent.
- [ ] `broker_dispatch.py`'s close-leg kwarg is renamed consistently to close-side semantics.
- [ ] The bracket-stop close path and the PM close path produce close-side legs via the shared seam.
- [ ] A strategy CLOSE via the OMS engine path and via the bracket-stop path each produces correct close-side `OptionLegRequest`s at the broker boundary, verified end-to-end (not just intermediate kwargs).
- [ ] OPEN and ADD leg construction are unchanged (regression-covered).
- [ ] Tests pass under `uv run pytest tests/execution/ --testmon -n auto`.

## Verification

`uv run pytest tests/execution/ --testmon -n auto`; `uv run ruff check .` and `uv run mypy` clean. Spot-check: a strategy CLOSE with mixed LONG and SHORT legs yields, at the broker boundary, `sell_to_close` for the LONG leg and `buy_to_close` for the SHORT leg.
