# execution/ — order management, broker I/O, real-time risk

Execution layer (the pipeline's terminal stage) plus the standalone **continuous
monitor** process. Start here: `oms/` is the command surface; `broker_adapter/` is the
Alpaca boundary; `continuous_monitor/` is a separate long-running process owning
real-time risk on open positions (breach checks, bracket stops, greeks, entry windows,
borrow accrual). Also: `position_model/`, `thesis_model/`, `corporate_actions/`,
`guardrail_enforcement/`, `paper_evaluation_harness/`, `counterfactual_replay_engine/`,
`state_persistence/`, `write_paths/`. Design intent (historical):
`docs/design/05-execution-layer/`.

## Key invariants

- **Two-phase invocation**: Phase 1 collects, Phase 2 commits atomically.
- **Single-writer**: only the execution write paths mutate portfolio state.
- OMS vocabulary is five commands — OPEN / CLOSE / ADJUST / CANCEL / ADD — no compound commands.
- Every position carries mandatory thesis-linked protective brackets.

## Gotchas

Append gotchas here as you hit them — non-obvious traps not evident from one file. Keep
each to a line or two; delete any that no longer hold.

- (none recorded yet)
