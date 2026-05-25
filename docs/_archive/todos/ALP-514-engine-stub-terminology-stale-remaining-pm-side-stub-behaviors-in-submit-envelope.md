## Symptom

The `decision/portfolio_manager/submit_envelope/` package is documented as a "transitional engine-stub", but [ALP-390](https://linear.app/alphamind-jatassi/issue/ALP-390/03e-engine-stub-coordinated-swap) (engine-stub coordinated swap routing real Alpaca paper acks) + [ALP-121](https://linear.app/alphamind-jatassi/issue/ALP-121/broker-adapter) (broker adapter) + [ALP-119](https://linear.app/alphamind-jatassi/issue/ALP-119/state-persistence) (state persistence) all landed. The package now routes through real broker dispatch and real persistence. The "engine-stub" terminology is stale across \~50 doc references, and several stub behaviors that the terminology described as "until the real engine lands" are still in the code.

## Part 1 — Stale "engine-stub" terminology cleanup

### Locations referencing "engine-stub"

* `src/alphamind/decision/portfolio_manager/submit_envelope/__init__.py:1, 3, 26` ("Engine-stub `submit_envelope` MCP wrapper package")
* `src/alphamind/decision/portfolio_manager/submit_envelope/{types,process,dispatch,persist,server}.py` — \~25 references
* `src/alphamind/commands/{protocols,submission_log,submission_results,command_models}.py` — \~10 references
* `src/alphamind/execution/oms/{submit_engine_envelope,broker_dispatch,command_ids,__init__}.py` — \~10 references
* `src/alphamind/execution/write_paths/phase2/{open,_shared,__init__,adjust}.py` — \~10 references
* `src/alphamind/execution/broker_adapter/{order_modify,order_options}.py` — \~3 references
* `src/alphamind/decision/portfolio_manager/harness.py` — \~5 references

Most can be rephrased as "submit_envelope MCP wrapper" or "PM-originated envelope path". Some genuine historical references (e.g., describing past behavior contrast like *"replaces the synthetic-acknowledgment behavior of the engine-stub"*) can stay as past-tense.

## Part 2 — Remaining stub behaviors

These are real stub paths the "until the real engine lands" terminology referenced. They survived the [ALP-390](https://linear.app/alphamind-jatassi/issue/ALP-390/03e-engine-stub-coordinated-swap) coordinated swap because their dependencies (position-id resolver, strategy direction projection) didn't land with it.

**(A)** `POS-{ticker}-stub` **/** `ORD-{ticker}-stub` **synthetic IDs for OpenCommand/AddCommand acknowledgments.** `src/alphamind/decision/portfolio_manager/submit_envelope/process.py:443-458`. The comment at line 343-344 says: *"Real exposure projection for ADD requires the position-id resolver wired through the OMS submission engine."*

**(B) StrategyInstrument direction fallback to** `"long"`**.** `src/alphamind/decision/portfolio_manager/submit_envelope/process.py:73-84` `_instrument_direction`. StrategyInstrument carries direction per-leg, not at the instrument level; the fallback "until story 03 reshapes the projection" is unresolved (story 03 = [ALP-323](https://linear.app/alphamind-jatassi/issue/ALP-323/03-pmenvelope-sentinel-minimal-oms-command-models) = Done but did not include the reshape).

**(C) AdjustCommand/AddCommand placeholder validation request.** `src/alphamind/decision/portfolio_manager/submit_envelope/process.py:347-368`. AddCommand has no embedded instrument; routes to placeholder validation with `ticker="__PLACEHOLDER__"`. Real exposure projection deferred.

## Scope

**(A) Terminology cleanup.** Mechanical pass replacing "engine-stub" with "submit_envelope MCP wrapper" / "PM-originated envelope path" in doc comments. Preserve past-tense historical references where they document a past-state contrast.

**(B) Wire position-id resolver.** Plumb a position-id lookup (via `PortfolioStateRepository`) through the PM harness into `submit_envelope/server.py` and surface it to OpenCommand/AddCommand acknowledgment construction. AdjustCommand already has `position_id` so its `ORD-ADJUST-{position_id}` synthesis is acceptable.

**(C) StrategyInstrument per-leg direction projection.** Reshape the projection path to consume real per-leg direction from `StrategyInstrument`. Falls under [ALP-323](https://linear.app/alphamind-jatassi/issue/ALP-323/03-pmenvelope-sentinel-minimal-oms-command-models) follow-up.

**(D) AddCommand validation request.** Construct a real `ValidationRequest` for AddCommand from the resolved underlying position's instrument; eliminate the `"__PLACEHOLDER__"` ticker path.

## Acceptance criteria

- [ ] `grep -n "engine-stub" src/alphamind/` returns only past-tense historical references.
- [ ] `grep -n "POS-.*-stub\|ORD-.*-stub" src/alphamind/` returns zero hits in non-test code.
- [ ] OpenCommand/AddCommand acknowledgments carry real position_id from a lookup, not a synthetic ID.
- [ ] StrategyInstrument projections consume per-leg direction; the `"long"` fallback in `_instrument_direction` is removed.
- [ ] AddCommand routes through a real `ValidationRequest` with the resolved underlying's ticker/instrument.
- [ ] `uv run pytest -n auto` passes; lint chain clean per CLAUDE.md.

## Verification

* `uv run python scripts/verify_debug_e2e.py` returns exit 0.
* Manual smoke: run a strategy ADD command end-to-end; observe a real position_id in the acknowledgment and a non-placeholder ticker in the validation request.