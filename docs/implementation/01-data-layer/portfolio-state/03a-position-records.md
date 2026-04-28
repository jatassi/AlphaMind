---
status: not_started
completed_date:
commit_id:
---

# 03a — Position records

## Goal

Define the consumer-facing typed records for position inventory (raw state category 1a) at `src/alphamind/portfolio_state/records/positions.py`, with field constraints validated via Pydantic v2 and a corresponding test suite at `tests/portfolio_state/records/test_positions.py`.

## Reading

- `../../../design/01-data-layer/internal/portfolio-state.md` — full feature spec; sections 1a and 1b define the consumer-facing computations the ingestion layer delivers alongside raw inventory fields
- `../../../design/05-execution-layer/position-model.md` — authoritative position type definition: base interface, equity (long and short), options, strategy, delta-adjusted exposure, margin model, and corporate-action-pending state
- `02-package-skeleton-and-config.md` — the package layout this story populates; `src/alphamind/portfolio_state/records/positions.py` is listed as story 03a's target
- `../../../implementation/03-analysis-layer/synthesizer/05b-portfolio-state-read-protocol.md` — sibling pattern: Pydantic-frozen typed records, StrEnum discriminators, validation tests, in-scope/out-of-scope split, and acceptance-criteria style

## Depends on

- 02

## Scope

In scope:

1. **Enums** — all `StrEnum`, `model_config = {"frozen": True}`, members in ALL CAPS:
   - `Direction`: `LONG`, `SHORT`
   - `InstrumentType`: `EQUITY`, `OPTIONS`, `STRATEGY`
   - `OptionContractType`: `CALL`, `PUT`
   - `PositionStatus`: `PENDING`, `OPEN`, `CLOSED`
   - `LocateStatus`: `LOCATED`, `AT_RISK_OF_RECALL`

2. **Value objects** — Pydantic v2 `BaseModel`, `model_config = {"frozen": True}`:
   - `OptionGreeks`: `delta: float`, `gamma: float`, `theta: float`, `vega: float`
   - `PositionFill`: `fill_timestamp: datetime` (tz-aware UTC), `fill_price: float`, `fill_quantity: float`, `slippage: float`, `fees: float` — the bare-minimum execution audit per position-model.md "Entry execution summary"
   - `EquityPositionDetails`: `ticker: str`, `share_count: float`, `average_cost_basis_per_share: float`; plus short-only fields (`borrow_rate_pct: float | None`, `locate_status: LocateStatus | None`, `margin_held_usd: float | None`) — present when `Direction.SHORT`, `None` when `Direction.LONG` (enforcement in `PositionRecord` validator)
   - `OptionsPositionDetails`: `underlying_ticker: str`, `strike_price: float`, `expiration_date: date`, `contract_type: OptionContractType`, `contract_count: float`, `contract_multiplier: float`, `premium_paid_per_contract: float`, `greeks: OptionGreeks`
   - `StrategyLeg`: embeds `OptionsPositionDetails` plus `leg_id: str` so the strategy bracket can reference individual legs
   - `StrategyPositionDetails`: `strategy_type_label: str`, `legs: tuple[StrategyLeg, ...]`, `net_premium_usd: float`, `max_profit_usd: float`, `max_loss_usd: float`, `breakeven_levels: tuple[float, ...]`, `strategy_greeks: OptionGreeks`

3. **`PositionRecord`** — the consumer-facing base record (all instruments):
   - Identity / binding: `position_id: str`, `thesis_id: str | None`, `bracket_id: str | None`
   - Status / direction: `status: PositionStatus`, `direction: Direction`
   - Entry: `entry_timestamp: datetime | None` (tz-aware UTC; `None` allowed when `status == PENDING`)
   - Instrument discriminator: `instrument_type: InstrumentType`
   - Details (exactly one non-`None`, matching `instrument_type`): `equity_details: EquityPositionDetails | None`, `options_details: OptionsPositionDetails | None`, `strategy_details: StrategyPositionDetails | None`
   - Execution history: `execution_history: tuple[PositionFill, ...]`
   - Realized P/L: `realized_pnl_to_date_usd: float | None`
   - Consumer-facing computed fields (shape declared here; population is the assembler's job — story 06): `current_market_value_usd: float`, `unrealized_pnl_usd: float`, `unrealized_pnl_pct: float`, `position_weight_pct: float`, `position_age_hours: float`, `notional_exposure_usd: float`, `delta_adjusted_exposure_usd: float`, `distance_to_target_usd: float | None`, `distance_to_stop_usd: float | None`, `risk_reward_at_current: float | None`
   - Corporate action: `corporate_action_adjustment_needed: bool`
   - Spin-off linkage: `parent_position_id: str | None`, `origin: str | None`

4. **Validation rules** (enforced via Pydantic `model_validator`):
   - Discriminator: exactly one of `equity_details`, `options_details`, `strategy_details` is non-`None`, and it matches `instrument_type`. Mismatched or multiple populated raises `ValidationError`.
   - `PositionStatus.PENDING` ⇒ `entry_timestamp` may be `None`; `execution_history` must be empty.
   - `PositionStatus.OPEN` ⇒ `execution_history` non-empty.
   - `PositionStatus.CLOSED` ⇒ `realized_pnl_to_date_usd` non-`None`; `current_market_value_usd` and `unrealized_pnl_usd` must be zero.
   - `Direction.SHORT` on equity ⇒ `borrow_rate_pct`, `locate_status`, `margin_held_usd` all non-`None`. `Direction.LONG` on equity ⇒ all three are `None`.
   - `position_weight_pct` in `[0, 100]`; `position_age_hours` ≥ 0.
   - `notional_exposure_usd` ≥ 0; `delta_adjusted_exposure_usd` unconstrained (negative for net-short positions).
   - Spin-off invariant: when `origin` is non-`None`, `parent_position_id` must be non-`None` and `corporate_action_adjustment_needed` must be `True`.

5. **Tests** at `tests/portfolio_state/records/test_positions.py`:
   - Each enum: correct member set, correct string values, immutability (frozen).
   - Each value object: valid construction; required fields enforced.
   - Each validation rule: at least one passing fixture and one failing fixture that produces `ValidationError` with a meaningful error path.
   - Discriminator coverage: EQUITY with `equity_details` populated passes; EQUITY with `options_details` populated fails; two details fields populated simultaneously fails.
   - Spin-off invariant: `origin` set without `parent_position_id` fails; `origin` set without `corporate_action_adjustment_needed=True` fails; all three set passes.
   - Range checks: `position_weight_pct` outside `[0, 100]` rejected; `position_age_hours < 0` rejected; `notional_exposure_usd < 0` rejected.

Out of scope:

- Computed-field population (story 05a defines pure computation functions; story 06 wires them via the assembler).
- Repository / OMS reads (story 04b).
- Sector exposure rollups (story 05c).
- Renderer for guardrail state header position-block lines (owned by the risk-guardrails state-delivery feature, not portfolio state).

## Notes

The position model is authoritative in `docs/design/05-execution-layer/position-model.md`. The consumer-facing record shape declared here is the contract; cross-tree convergence (when state-persistence stories implement DB schemas) is a future concern.

Per `feedback_no_inventing_component_names.md`, every type name, enum member, and field name is sourced directly from the design docs (`position-model.md`, `state-persistence.md`, `portfolio-state.md`). No new field names are introduced here.

Per `feedback_avoid_numeric_anchors.md`, field docstrings do not impose threshold-style interpretations (e.g., no "weight above X% is concentrated"). The records carry the data; downstream interpretation is not the records' job.

Per `feedback_per_producer_schema.md`, the equity/options/strategy details split via discriminator is the per-producer pattern. The consumer-facing `PositionRecord` is one type with three details flavors validated structurally — not a shared variant union.

Computed fields (`current_market_value_usd`, `position_weight_pct`, `unrealized_pnl_usd`, `unrealized_pnl_pct`, `position_age_hours`, `notional_exposure_usd`, `delta_adjusted_exposure_usd`, `distance_to_target_usd`, `distance_to_stop_usd`, `risk_reward_at_current`) are part of the consumer-facing shape because `portfolio-state.md` § 1a and `position-model.md` § Base position name them as fields the ingestion layer delivers. The shape is consumer-facing; population is the assembler's job (story 06).

The `thesis_id` and `bracket_id` fields are `str | None` to accommodate the spin-off orphan state described in `position-model.md` § Corporate-action-pending positions, where a `SPIN` activity creates a new local position with `thesis_id: null` and `bracket_id: null` until the strategist resolves it.

## Acceptance criteria

- [ ] `Direction`, `InstrumentType`, `OptionContractType`, `PositionStatus`, and `LocateStatus` each define exactly the documented members.
- [ ] `OptionGreeks`, `PositionFill`, `EquityPositionDetails`, `OptionsPositionDetails`, `StrategyLeg`, `StrategyPositionDetails`, and `PositionRecord` are frozen Pydantic v2 models with the documented field sets.
- [ ] Discriminator validation enforces exactly one of `equity_details`, `options_details`, `strategy_details` is non-`None` and matches `instrument_type`; mismatch or multiple raises `ValidationError`.
- [ ] `PositionStatus.PENDING` ⇒ `entry_timestamp` may be `None` and `execution_history` must be empty — both rules have passing and failing tests.
- [ ] `PositionStatus.OPEN` ⇒ `execution_history` non-empty — rule has passing and failing tests.
- [ ] `PositionStatus.CLOSED` ⇒ `realized_pnl_to_date_usd` non-`None`; `current_market_value_usd` and `unrealized_pnl_usd` zero — rule has passing and failing tests.
- [ ] `Direction.SHORT` on equity ⇒ short-only fields non-`None`; `Direction.LONG` on equity ⇒ short-only fields `None` — both have passing and failing tests.
- [ ] `position_weight_pct` outside `[0, 100]` rejected; `position_age_hours < 0` rejected; `notional_exposure_usd < 0` rejected — each has a passing and failing test.
- [ ] Spin-off invariant: `origin` non-`None` ⇒ `parent_position_id` non-`None` and `corporate_action_adjustment_needed` `True` — invariant has passing and failing tests.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
