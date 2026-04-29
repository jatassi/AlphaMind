---
status: in_progress
completed_date:
commit_id:
---

# 08 — End-to-end verification

## Goal

Land golden tests reproducing the breach-behavior scenarios from `scenario-tests.md` end-to-end through the package's public surface. Each test instantiates a fixture portfolio state, drives one or more breach events, and asserts the resulting envelope tuples / halt states / emergency contexts / hard rejection payloads match the design walkthrough's documented outcome. The goal is integration coverage: every story 02–07 primitive composes correctly under realistic inputs, and the test corpus serves as living documentation tying code behavior to the design's worked examples.

## Reading

- `docs/design/06-risk-guardrails/scenario-tests.md` — the source of every scenario this story reproduces. Particularly:
  - § A4 — Daily drawdown halt triggers mid-session
  - § A6 — Short squeeze: engine protective CLOSE between invocations (position-level max-loss + single-short-size co-firing on the same position)
  - § A7 — Margin call cascade during elevated regime
  - § A8 — Cumulative drawdown progressive response — system enters tier 2
  - § A10 — Low-vol regime → crisis: emergency invocation + concurrent breaches
  - § A11 — Synchronized HTB buy-in cascade (no engine-originated CLOSE; broker-driven closure; this scenario asserts the *absence* of cascade envelopes plus the daily-drawdown stop check)
- `docs/design/06-risk-guardrails/breach-behavior.md` — the policy contract every test asserts against.
- All prior stories in this work tree (02–07) — the primitives composed here.
- `docs/implementation/06-risk-guardrails/state-delivery/08-end-to-end-verification.md` — sibling story precedent for end-to-end fixture-driven tests.
- `docs/implementation/06-risk-guardrails/guardrail-evaluation/06-end-to-end-golden-tests.md` — sibling precedent.

## Depends on

- 01 (README link)
- 02 (package skeleton + config)
- 03 (canonical types)
- 04a (zone classifier)
- 04b (cumulative drawdown tier)
- 04c (hard rejection payload)
- 04d (position selection)
- 05a (halt state computation)
- 05b (secondary breach check)
- 05c (emergency invocation triggers)
- 06 (engine-originated envelope assembler)
- 07 (margin call cascade orchestration)

(All prior stories must be `done`. The orchestrator gates this story's dispatch on the rest of the work tree.)

## Scope

In scope, all under `tests/risk_guardrails/breach_behavior/test_e2e_scenarios.py`. Fixtures under `tests/risk_guardrails/breach_behavior/fixtures/`.

### 1. Fixture infrastructure

A small set of helpers that construct typed records (PortfolioStateSnapshot, ActiveRiskParameterSet, RiskBudgetConsumption, etc.) from compact dictionary literals. The helpers exist solely for test readability — production code does not import them.

```python
# tests/risk_guardrails/breach_behavior/fixtures/builders.py

def make_position_record(
    *,
    position_id: str,
    ticker: str,
    direction: Literal["long", "short"] = "long",
    size_pct: float,
    size_usd: float,
    unrealized_pnl_usd: float = 0.0,
    sector: str = "tech",
    asset_type: Literal["equity", "option", "strategy"] = "equity",
) -> PositionRecord: ...

def make_risk_budget(*, entries: list[dict]) -> RiskBudgetConsumption: ...

def make_active_risk_parameters(
    *,
    regime: RegimeLabel,
    rule_values: dict[str, float],          # rule_id → effective limit
) -> ActiveRiskParameterSet: ...

def make_drawdown_state(
    *,
    intraday_pct: float,
    cumulative_pct: float,
    cumulative_tier: DrawdownTier | None = None,
) -> DrawdownState: ...
```

The builders use sensible defaults for fields not load-bearing in a given test. They live under `tests/` not `src/`; production code does not import them.

### 2. Scenario A4 — daily drawdown halt

Reproduce the design's A4 walkthrough at the breach-behavior layer:

```python
def test_a4_daily_drawdown_halt_at_2pct8_intraday() -> None:
    # Portfolio state: $100K, daily P/L -1.8% earlier, now -2.8% mid-session.
    # Daily drawdown limit 2.5% (normal regime).
    drawdown = make_drawdown_state(intraday_pct=2.8, cumulative_pct=0.0)
    active_params = make_active_risk_parameters(
        regime=RegimeLabel.NORMAL,
        rule_values={"daily_drawdown_pct": 2.5, ...},
    )

    halt_state = compute_halt_state(
        drawdown_state=drawdown, active_risk_parameters=active_params,
    )

    assert halt_state is not None
    assert halt_state.daily_halt_active is True
    assert halt_state.cumulative_full_halt_active is False
    assert halt_state.daily_drawdown_pct == 2.8
    assert halt_state.daily_drawdown_limit_pct == 2.5
```

Plus the recovery-doesn't-lift-halt-this-session note from the design — verified by a separate test using a portfolio that recovers to 2.2% intraday after halt fired. This test does NOT exercise session-latching (that's continuous-monitor territory); it asserts that the breach-behavior primitive returns the HaltState correctly given current state. Latching is documented as a continuous-monitor responsibility in story 05a's notes.

### 3. Scenario A6 — short squeeze, position-level max-loss + single-short-size co-firing

Reproduce the engine protective close on a short whose loss hit the position-level max:

```python
def test_a6_short_squeeze_position_max_loss_close() -> None:
    # MARA short, 140 shares at $20 entry, now $28 = +40% adverse → 40% loss > 30% equity max.
    # Position size grew from 2.8% to 3.92% (under single-short cap 3% by 0.92pp).
    # Both rules fire; position-level max-loss has higher priority (full close vs partial trim).

    positions = (
        make_position_record(
            position_id="POS-MARA-001", ticker="MARA", direction="short",
            size_pct=3.92, size_usd=3920.0, unrealized_pnl_usd=-1120.0,
        ),
    )
    liquidity = (PositionLiquidity(position_id="POS-MARA-001", adv_to_position_size_ratio=2.5),)

    selection = select_for_position_max_loss(
        breaching_position_id="POS-MARA-001", open_positions=positions,
    )
    assert selection.action == PositionSelectionAction.FULL_CLOSE
    assert selection.position_id == "POS-MARA-001"
    assert "position-level max loss" in selection.rationale
    assert "MARA" in selection.rationale
```

Plus the engine envelope composition:

```python
def test_a6_engine_envelope_for_max_loss_close() -> None:
    selection = ...  # from prior test
    breach_details = BreachDetails(
        current_value=40.0, limit_value=30.0, overage=10.0,
        unit="% of cost basis", regime_at_breach=RegimeLabel.NORMAL,
    )
    secondary_check = SecondaryBreachCheckResult(
        result=SecondaryBreachOutcome.NO_SECONDARY_BREACH,
        notes="closing reduces all exposures; no new breach introduced",
    )
    positions_by_id = {p.position_id: p for p in positions}

    envelope = compose_engine_envelope(
        monitor_session_id="s1",
        trigger_id=1,
        trigger_timestamp=datetime(2026, 4, 28, 14, 30, tzinfo=UTC),
        rule_breached="position_max_loss_equity_pct",
        breach_details=breach_details,
        position_selection=selection,
        positions_by_id=positions_by_id,
        portfolio_value_usd=100_000.0,
        secondary_breach_check=secondary_check,
    )

    assert envelope.envelope_id == "MON.s1.1"
    assert envelope.command.command_id == "MON.s1.1.1"
    assert envelope.command.position_id == "POS-MARA-001"
    assert envelope.command.quantity_or_all == "all"
    assert envelope.guardrail_trigger_record.rule_breached == "position_max_loss_equity_pct"
    assert envelope.guardrail_trigger_record.cascade_id is None  # not a cascade
    assert envelope.source_provenance == "engine_guardrail"
```

### 4. Scenario A7 — margin call cascade during elevated regime

Reproduce the full cascade orchestration end-to-end:

```python
def test_a7_margin_call_cascade_no_secondary_breach() -> None:
    # 3 short positions: COIN 8%, SQ 7%, HOOD 7%. Aggregate 22%. Margin call $3K.
    # COIN has worst R/R (per fixture). Full close cures margin; net long 30% → 38%
    # (under 45% elevated limit); gross 74% → 66%. No secondary.

    positions = (
        make_position_record(position_id="POS-COIN-001", ticker="COIN",
                             direction="short", size_pct=8.0, size_usd=8000.0),
        make_position_record(position_id="POS-SQ-001", ticker="SQ",
                             direction="short", size_pct=7.0, size_usd=7000.0),
        make_position_record(position_id="POS-HOOD-001", ticker="HOOD",
                             direction="short", size_pct=7.0, size_usd=7000.0),
    )
    liquidity = (
        PositionLiquidity(position_id="POS-COIN-001", adv_to_position_size_ratio=3.0),
        PositionLiquidity(position_id="POS-SQ-001", adv_to_position_size_ratio=2.5),
        PositionLiquidity(position_id="POS-HOOD-001", adv_to_position_size_ratio=2.0),
    )
    risk_reward = (
        PositionRiskReward(position_id="POS-COIN-001", risk_reward_ratio=0.3),  # worst
        PositionRiskReward(position_id="POS-SQ-001", risk_reward_ratio=0.7),
        PositionRiskReward(position_id="POS-HOOD-001", risk_reward_ratio=0.9),
    )
    margin_call = MarginCallEvent(
        issued_at=datetime(2026, 4, 28, 11, 30, tzinfo=UTC),
        additional_margin_required_usd=3000.0,
    )
    context = CascadeContext(
        monitor_session_id="s1",
        initial_trigger_id=5,
        cascade_id=generate_cascade_id(monitor_session_id="s1", initial_trigger_id=5),
        trigger_timestamp=datetime(2026, 4, 28, 11, 30, tzinfo=UTC),
        portfolio_value_usd=98_000.0,
        config=load_breach_behavior_config(Path("config/breach_behavior.yaml")),
    )

    # Library output stubs: pre-evaluation has total_short_pct in BLOCKED zone (the breach trigger
    # was margin call, but at 22% total short under elevated 25% limit it's still legal — the
    # margin call is broker-imposed regardless); post-evaluation (after closing COIN) has all
    # rules in PASS/WARNING.
    library_config = make_library_config_stub(...)
    market_inputs = make_market_inputs_stub(...)
    current_state = make_state_snapshot(positions=positions, ...)

    envelopes = orchestrate_margin_call_cascade(
        margin_call_event=margin_call,
        open_positions=positions,
        liquidity=liquidity,
        risk_reward_metric=risk_reward,
        current_state=current_state,
        library_config=library_config,
        market_inputs=market_inputs,
        active_regime=RegimeLabel.ELEVATED,
        context=context,
    )

    assert len(envelopes) == 1  # single liquidation, no cascade
    env = envelopes[0]
    assert env.envelope_id == "MON.s1.5"
    assert env.command.position_id == "POS-COIN-001"  # worst R/R
    assert env.command.quantity_or_all == "all"  # full close
    assert env.guardrail_trigger_record.rule_breached == "margin_call"
    assert env.guardrail_trigger_record.cascade_id == "CASCADE.s1.5"
    assert env.guardrail_trigger_record.secondary_breach_check_result.result \
        == SecondaryBreachOutcome.NO_SECONDARY_BREACH
```

A second test for A7 covers a constructed scenario where closing COIN *does* introduce a net-long secondary breach — verifying the design's "primary takes priority; secondary deferred" behavior:

```python
def test_a7_margin_call_cascade_with_secondary_deferred() -> None:
    # Same positions but pre-cascade net long is closer to the limit; closing COIN
    # pushes net long over.

    ...

    envelopes = orchestrate_margin_call_cascade(...)

    assert len(envelopes) == 1  # margin call: no alternate search; single envelope
    assert envelopes[0].guardrail_trigger_record.secondary_breach_check_result.result \
        == SecondaryBreachOutcome.DEFERRED_TO_PM
    assert "net_long_pct" in envelopes[0].guardrail_trigger_record.secondary_breach_check_result.notes
```

### 5. Scenario A8 — cumulative drawdown tier 2

Reproduce tier classification + override application:

```python
def test_a8_cumulative_drawdown_tier_2_overrides() -> None:
    # Cumulative drawdown 10% (just crossed tier 2 trigger).
    # Pre-tier limits: position size 5%, gross 120%.
    # Tier 2 overrides: position size 2%, gross 60%.

    progressive_tiers = ...  # from shipped guardrails.yaml

    tier = classify_cumulative_drawdown_tier(
        current_drawdown_pct=10.0,
        progressive_tiers=progressive_tiers,
    )
    assert tier == DrawdownTier.HEAVILY_CONSTRAINED

    pre_params = make_active_risk_parameters(
        regime=RegimeLabel.NORMAL,
        rule_values={
            "position_max_size_pct": 5.0,
            "gross_exposure_pct": 120.0,
            ...
        },
    )

    post_params = apply_progressive_tier_overrides(
        active_risk_parameters=pre_params,
        tier=tier,
        progressive_tiers=progressive_tiers,
    )

    pos_max = next(e for e in post_params.entries if e.rule_id == "position_max_size_pct")
    gross = next(e for e in post_params.entries if e.rule_id == "gross_exposure_pct")
    assert pos_max.value == 2.0
    assert gross.value == 60.0
    assert "cumulative_drawdown_tier_2" in post_params.active_overlays
```

### 6. Scenario A10 — low-vol → crisis emergency invocation

Reproduce the regime-jump trigger plus concurrent breaches:

```python
def test_a10_regime_jump_low_vol_to_crisis_fires_emergency() -> None:
    now = datetime(2026, 4, 28, 14, 0, tzinfo=UTC)
    last_invocation = now - timedelta(minutes=85)

    context_or_none = evaluate_emergency_invocation(
        now=now,
        last_invocation_started_at=last_invocation,
        last_emergency_triggered_at=None,
        cooldown_minutes=30,
        normal_cadence_minutes=120.0,
        prior_regime_label=RegimeLabel.LOW_VOL,
        current_regime_label=RegimeLabel.CRISIS,
        risk_budget=make_risk_budget(entries=[]),       # empty for this test
        rules_with_deferred_response=("net_long_pct", "gross_exposure_pct", ...),
        drawdown_history=(),
        daily_drawdown_limit_pct=1.5,                   # crisis regime
        margin_call_event=None,
        config=load_breach_behavior_config(Path("config/breach_behavior.yaml")),
    )

    assert context_or_none is not None
    assert context_or_none.trigger == EmergencyTrigger.REGIME_JUMP
    assert "low_vol" in context_or_none.trigger_detail
    assert "crisis" in context_or_none.trigger_detail
    assert context_or_none.minutes_since_last_invocation == pytest.approx(85.0)
    assert context_or_none.normal_cadence_minutes == 120.0
```

A second A10 test reproduces the regime-transition breach detection. The detector and the `RegimeTransitionBreach` typed record are owned by the regime-adaptation work tree (story 07 there); this test imports both from `alphamind.risk_guardrails.regime_adaptation`. The test is included in this work tree's E2E suite because it covers the full A10 walkthrough end-to-end alongside the breach-behavior emergency-invocation trigger:

```python
def test_a10_regime_transition_introduces_breaches() -> None:
    # Pre-transition: low-vol limits (gross 130, net long 70, options delta 45, etc.).
    # Post-transition: crisis limits (gross 60, net long 30, options delta 15, etc.).
    # Portfolio: gross 118, net long 65, theta $170.
    # All three exceed crisis limits but not low-vol limits → regime-transition breaches.

    pre_params = make_active_risk_parameters(
        regime=RegimeLabel.LOW_VOL,
        rule_values={"net_long_pct": 70.0, "gross_exposure_pct": 130.0, "portfolio_theta_pct_per_day": 0.18, ...},
    )
    post_params = make_active_risk_parameters(
        regime=RegimeLabel.CRISIS,
        rule_values={"net_long_pct": 30.0, "gross_exposure_pct": 60.0, "portfolio_theta_pct_per_day": 0.05, ...},
    )

    # detect_regime_transition_breaches and RegimeTransitionBreach come from regime_adaptation.
    # See regime-adaptation story 07 for the detector contract and 02 for the typed record.
    breaches = detect_regime_transition_breaches(
        held_positions=positions,
        risk_budget=risk_budget_snapshot,
        new_effective_limits=post_params_effective_limits,
        transition_state=RegimeTransitionState.TIGHTENING,
        rule_metadata=rule_metadata,
    )

    assert len(breaches) >= 3  # net long, gross, options delta all breach crisis but not low-vol
    rule_ids = {b.rule_id for b in breaches}                    # regime-adaptation field name
    assert "net_long_pct" in rule_ids
    assert "gross_exposure_pct" in rule_ids
    assert "options_delta_pct" in rule_ids
```

### 7. Scenario A11 — synchronized HTB buy-in (no engine envelope)

This scenario asserts the *absence* of cascade orchestration. Three forced buy-ins close the positions externally; the breach-behavior primitives are not invoked because the broker handles the close. The test verifies:

```python
def test_a11_htb_buy_in_does_not_fire_emergency_trigger() -> None:
    # Per design A11, synchronized forced buy-ins are not an emergency trigger condition.
    # The trigger list is regime jump, multi-rule breach, drawdown velocity, margin call.
    # Forced buy-ins are not in the list.

    drawdown_history = (
        DrawdownSample(sampled_at=datetime(2026, 4, 28, 14, 0, tzinfo=UTC),
                       intraday_drawdown_pct=2.0),
    )
    context_or_none = evaluate_emergency_invocation(
        now=datetime(2026, 4, 28, 14, 30, tzinfo=UTC),
        last_invocation_started_at=datetime(2026, 4, 28, 13, 0, tzinfo=UTC),
        last_emergency_triggered_at=None,
        cooldown_minutes=30,
        normal_cadence_minutes=120.0,
        prior_regime_label=RegimeLabel.NORMAL,
        current_regime_label=RegimeLabel.NORMAL,                         # no regime change
        risk_budget=make_risk_budget(entries=[]),                        # no rules in BLOCKED
        rules_with_deferred_response=(...),
        drawdown_history=drawdown_history,
        daily_drawdown_limit_pct=2.5,
        margin_call_event=None,                                          # no margin call
        config=load_breach_behavior_config(Path("config/breach_behavior.yaml")),
    )

    assert context_or_none is None  # no trigger fires
```

Plus a halt-state assertion: A11 says "Daily drawdown ~2.0%, under the 2.5% limit. No halt engaged." Verified:

```python
def test_a11_daily_drawdown_below_halt_threshold() -> None:
    drawdown = make_drawdown_state(intraday_pct=2.0, cumulative_pct=0.0)
    active_params = make_active_risk_parameters(
        regime=RegimeLabel.NORMAL,
        rule_values={"daily_drawdown_pct": 2.5, ...},
    )

    halt_state = compute_halt_state(
        drawdown_state=drawdown, active_risk_parameters=active_params,
    )

    assert halt_state is None
```

### 8. Cross-cutting verification

A small set of tests asserting properties across primitives:

- **Determinism across composition:** the same scenario inputs produce identical outputs across 100 repeated runs (composing A6 + A7 + A8 + A10 in turn).
- **No side effects:** running scenarios in any order produces the same per-scenario outputs (no global state).
- **Frozen output:** every returned typed record is frozen.

Out of scope:
- The continuous monitor's session-state management (this story's tests construct CascadeContext and emergency-trigger inputs directly).
- The OMS / broker submission pipeline (envelopes are constructed; submission is execution-layer).
- State-delivery rendering (envelopes flow into headers via state-delivery's renderers; that integration is state-delivery story 08).
- LLM-side reasoning on emergency / halt / breach contexts (agent-prompt territory).

## Notes

**Why this story is fixture-heavy.** Reproducing the design's worked examples requires constructing realistic PortfolioStateSnapshot, ActiveRiskParameterSet, and library-output objects. Builders centralize the construction so tests focus on the breach-behavior assertion rather than 50-line setup boilerplate.

**Why include A11 (no-engine-envelope scenario).** A negative-result test confirms the primitives correctly *do not* fire under conditions the design says should be benign. The trigger list is closed; A11 verifies the closure.

**Coverage scope: design walkthroughs vs. exhaustive coverage.** This story does not aim to be exhaustive — story 04 series tests cover unit-level edge cases. This story exercises the integration paths the design explicitly walks through. Adding scenarios beyond the design (e.g., "regime jump + margin call simultaneously fire margin call, not regime jump" — already a 05c unit test) is out of scope; integration coverage focuses on the documented worked examples.

**Library stubs vs. real library.** If the guardrail-evaluation library is not yet implemented at the time of this story's dispatch, the tests use library-output Protocol stubs for any scenarios involving `evaluate_proposals`. The orchestrator surfaces this gap in the verification report; once the library is implemented, a follow-up edit replaces stubs with production calls (mechanical change).

**Per `feedback_simplify_before_building.md`,** this story's tests cover the design's six named scenarios. Resist the urge to add hypothetical scenarios — the unit-level tests already cover them. Per `feedback_no_inventing_component_names.md`, scenario test names use the design's `A1`/`A2`/`A6`/`A7`/`A8`/`A10`/`A11` labels verbatim (the `Part A` prefix from `scenario-tests.md`).

## Acceptance criteria

- [ ] `tests/risk_guardrails/breach_behavior/test_e2e_scenarios.py` exists with at least one test per scenario A4, A6 (×2: selection + envelope), A7 (×2: no-secondary + secondary-deferred), A8, A10 (×2: emergency trigger + regime-transition breaches), A11 (×2: no emergency + no halt).
- [ ] `tests/risk_guardrails/breach_behavior/fixtures/builders.py` exists with the builder helpers documented in section 1.
- [ ] A4 test: `compute_halt_state` returns a HaltState with `daily_halt_active=True` for the documented input.
- [ ] A6 selection test: `select_for_position_max_loss` returns FULL_CLOSE on POS-MARA-001 with rationale containing "position-level max loss" and "MARA".
- [ ] A6 envelope test: `compose_engine_envelope` produces an envelope with the documented fields (envelope_id, command_id, command.position_id, command.quantity_or_all, rule_breached, source_provenance, secondary_breach_check_result.result).
- [ ] A7 no-secondary test: `orchestrate_margin_call_cascade` returns a single-envelope tuple with `secondary_breach_check_result.result == "no_secondary_breach"`, COIN closed (worst R/R), cascade_id populated.
- [ ] A7 secondary-deferred test: same orchestrator returns a single-envelope tuple with `secondary_breach_check_result.result == "deferred_to_pm"` and notes naming the secondary rule.
- [ ] A8 test: `classify_cumulative_drawdown_tier` returns HEAVILY_CONSTRAINED at 10% drawdown; `apply_progressive_tier_overrides` produces a parameter set with position_max_size 2% and gross 60% with the `cumulative_drawdown_tier_2` overlay.
- [ ] A10 emergency test: `evaluate_emergency_invocation` returns a context with `trigger=REGIME_JUMP` and the documented descriptive text for low-vol → crisis.
- [ ] A10 regime-transition-breach test: `detect_regime_transition_breaches` (imported from `regime_adaptation`) returns at least 3 breaches with `rule_id` ∈ {net_long_pct, gross_exposure_pct, options_delta_pct} given the documented A10 inputs. (Cross-feature: requires regime-adaptation's stories 02 and 07 to be `done` before this assertion can run; otherwise the test is skipped with a TODO comment until the gate clears.)
- [ ] A11 emergency test: `evaluate_emergency_invocation` returns `None` for the A11 conditions (no trigger).
- [ ] A11 halt test: `compute_halt_state` returns `None` for the A11 conditions (drawdown below limit).
- [ ] Determinism: each scenario test runs 5 times in a row producing identical outputs; verified by a parametrized `repetition_count=5` decorator or equivalent.
- [ ] All returned typed records are frozen (assigning to fields raises ValidationError) — verified at least once per scenario.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
