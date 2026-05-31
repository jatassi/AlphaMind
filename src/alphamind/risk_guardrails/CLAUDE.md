# risk_guardrails/ — hard-coded programmatic risk constraints (cross-cutting)

Risk layer. **Not** LLM-mediated — deterministic constraints enforced at multiple layers
(analyst advisory, PM judgment, execution-engine hard stop). Subpackages:
`rules_and_limits/`, `guardrail_evaluation/` (shared math: Black-Scholes delta, per-rule
projection, regime resolution), `regime_adaptation/` (per-regime multipliers),
`breach_behavior/` (forced reduction, drawdown halt, margin cascade), `state_delivery/`
(per-agent guardrail headers), `scenario_tests/`. Design intent (historical):
`docs/design/06-risk-guardrails/`.

## Key invariants

- Constraints are hard-coded and deterministic — never delegate a limit decision to an LLM.
- Multi-layer enforcement: the execution engine is the final hard stop regardless of upstream agent behavior.

## Gotchas

Append gotchas here as you hit them — non-obvious traps not evident from one file. Keep
each to a line or two; delete any that no longer hold.

- (none recorded yet)
