---
status: done
completed_date: 2026-04-29
commit_id: e45475c
---

# 07 — Margin call cascade orchestration

## Goal

Land the deterministic primitive that orchestrates the multi-step response to a margin call: cascade ID generation, position selection for the initial liquidation, secondary-breach checking, optional alternate-position search, and emission of one or more engine envelopes linked by a shared `cascade_id`. Each cascade step emits its own envelope; envelopes share the cascade ID so the activity log can reconstruct the chain. Per `breach-behavior.md § Margin call cascade handling`, the cascade also re-evaluates all rules post-liquidation and emits additional envelopes for any *new* breaches introduced by the cascade itself (typically a `deferred_to_pm` outcome — the secondary breach is logged but the primary close already executed).

The same orchestration pattern handles primary→secondary breach cascades (a non-margin breach whose protective close introduces a secondary breach in a different rule). This story produces the orchestrator; story 06's envelope assembler emits each cascade step.

## Reading

- `docs/design/06-risk-guardrails/breach-behavior.md` § Margin call cascade handling — the authoritative orchestration contract:
  > Margin call priority: Absolute priority over all guardrail rules. If liquidation is required, the engine liquidates regardless of impact on other constraints — failure to meet the call results in broker-initiated forced liquidation at worse prices.
  >
  > Cascade detection: After liquidation, the engine re-evaluates all rules against post-liquidation state. New breaches (e.g., closing a short causes a net long breach) are handled per their own classification.
  >
  > Cascade logging: Each step is logged as a command envelope with `engine_guardrail` provenance and `margin_cascade` sub-type. A shared `cascade_id` links the chain.
  >
  > Margin call position selection: Worst risk/reward ratio at current price (closest to invalidation, farthest from target). Tiebreaker: most liquid. Full closes — partial may not satisfy the requirement and leaves residual risk.
- `docs/design/06-risk-guardrails/breach-behavior.md` § Secondary breach checking:
  > If a secondary breach would result:
  > 1. Log the conflict with both the primary and secondary breach
  > 2. Select an alternative position that cures the primary without creating a secondary breach
  > 3. If no clean cure exists, execute anyway — primary takes priority, secondary is flagged for the PM at the next invocation.
- `docs/design/05-execution-layer/engine-envelope-schema.md` § Notes on cross-field invariants:
  > Cascades produce multiple envelopes, not multiple commands. Each cascade stage (margin call → forced reduction → secondary breach avoidance → final close) emits its own envelope with its own `trigger_id` and the shared `cascade_id`.
- `docs/design/06-risk-guardrails/scenario-tests.md` § A7 (margin call cascade during elevated regime) — the canonical worked example.
- `src/alphamind/risk_guardrails/breach_behavior/types.py` — `EngineEnvelope`, `SecondaryBreachOutcome`, `BreachDetails`, `PositionSelectionResult`, `RegimeLabel`, etc.
- `src/alphamind/risk_guardrails/breach_behavior/position_selection.py` — `select_for_margin_call` (story 04d).
- `src/alphamind/risk_guardrails/breach_behavior/secondary_breach.py` — `check_secondary_breach`, `ProposedClose` (story 05b).
- `src/alphamind/risk_guardrails/breach_behavior/engine_envelope.py` — `compose_engine_envelope`, `envelope_id_for` (story 06).
- `src/alphamind/risk_guardrails/breach_behavior/config.py` — `BreachBehaviorConfig.cascade_max_steps` (defensive cap on chain length).

## Depends on

- 02 (package skeleton + config — for cascade max-steps)
- 03 (canonical types)
- 04d (position selection — `select_for_margin_call` and other selectors invoked during alternate-position search)
- 05b (secondary breach check)
- 06 (engine envelope assembler)

## Scope

In scope, all under `src/alphamind/risk_guardrails/breach_behavior/cascade.py`. Tests at `tests/risk_guardrails/breach_behavior/test_cascade.py`.

### 1. Cascade ID generation

```python
def generate_cascade_id(*, monitor_session_id: str, initial_trigger_id: int) -> str:
    """Return a deterministic cascade ID derived from the initiating trigger.

    Format: 'CASCADE.{monitor_session_id}.{initial_trigger_id}'. The cascade ID is unique
    within a monitor session because trigger_ids are monotonic; a new session produces a
    new prefix.

    Args:
        monitor_session_id: The continuous monitor's session ID (must not contain '.').
        initial_trigger_id: The trigger_id of the cascade's first envelope.

    Raises:
        ValueError: when monitor_session_id is empty or contains '.', or when initial_trigger_id < 1.
    """
```

### 2. Cascade orchestrator — margin call

```python
@dataclass(frozen=True, slots=True)
class CascadeContext:
    """Inputs threaded through cascade orchestration."""

    monitor_session_id: str
    initial_trigger_id: int                          # the trigger_id assigned to the first envelope
    cascade_id: str                                  # generated via generate_cascade_id; reused across all envelopes
    trigger_timestamp: datetime                      # tz-aware
    portfolio_value_usd: float
    config: BreachBehaviorConfig


def orchestrate_margin_call_cascade(
    *,
    margin_call_event: MarginCallEvent,
    open_positions: tuple[PositionRecord, ...],
    liquidity: tuple[PositionLiquidity, ...],
    risk_reward_metric: tuple[PositionRiskReward, ...],
    current_state: PortfolioStateSnapshotProtocol,
    library_config: LibraryConfigProtocol,
    market_inputs: MarketInputsProtocol,
    active_regime: RegimeLabel,
    context: CascadeContext,
) -> tuple[EngineEnvelope, ...]:
    """Orchestrate the multi-step margin call cascade.

    Sequence:
      1. Generate cascade_id (already in context).
      2. Initial liquidation:
         a. Select position via select_for_margin_call (story 04d).
         b. Check secondary breach via check_secondary_breach (story 05b) on the proposed close,
            primary_breach_rule_id="margin_call".
         c. Compose envelope #1 via compose_engine_envelope (story 06) with cascade_id and the
            secondary-breach result.
      3. Post-liquidation re-evaluation:
         a. Compute the post-liquidation portfolio state by applying envelope #1's close
            hypothetically. The library re-projection produces the post-state per-rule status.
         b. Identify any newly-FAILed rules (post FAIL but pre PASS/WARNING).
         c. For each new breach in receipt order:
            - Look up the rule's breach_response classification.
            - If immediate_engine: select position for the breach (using the appropriate selector
              from story 04d), check secondary, compose envelope #N+1, append to cascade.
            - If deferred_to_pm: log to cascade output but emit no additional envelope; the
              breach surfaces in the next invocation's strategist/PM context.
      4. Continue until either no new breaches are detected or context.config.cascade_max_steps
         is reached. Hitting the max raises CascadeStepLimitExceeded — the caller surfaces this
         as a structural error.

    Returns:
        A tuple of EngineEnvelope, in cascade-order. The first envelope is the initial
        margin-call liquidation; subsequent envelopes are post-liquidation forced reductions.
        Each envelope's guardrail_trigger_record.cascade_id equals context.cascade_id.

    Raises:
        ValueError: on input-validation failures (empty positions, missing liquidity, etc.).
        CascadeStepLimitExceeded: when the cascade chain reaches context.config.cascade_max_steps.
    """
```

### 3. Cascade orchestrator — primary→secondary breach (non-margin)

```python
def orchestrate_breach_cascade(
    *,
    primary_rule: str,                               # the rule whose breach initiated this cascade (non-margin)
    primary_breach_details: BreachDetails,
    proposed_close: ProposedClose,                   # the close intended to cure the primary breach
    primary_position_selection: PositionSelectionResult,
    open_positions: tuple[PositionRecord, ...],
    liquidity: tuple[PositionLiquidity, ...],
    current_state: PortfolioStateSnapshotProtocol,
    library_config: LibraryConfigProtocol,
    market_inputs: MarketInputsProtocol,
    active_regime: RegimeLabel,
    context: CascadeContext,
) -> tuple[EngineEnvelope, ...]:
    """Orchestrate a non-margin breach cascade with secondary-breach handling.

    Sequence:
      1. Check secondary breach on the proposed_close.
      2. If NO_SECONDARY_BREACH: emit one envelope (the primary close) with the cascade_id
         populated and secondary_breach_check.result=NO_SECONDARY_BREACH. Return single-envelope tuple.
      3. If DEFERRED_TO_PM: attempt alternate-position search (see "Alternate-position search"
         below).
         - If a clean alternative is found: emit one envelope for the *alternate* close, with
           secondary_breach_check.result=SECONDARY_BREACH_AVOIDED and notes describing the
           original-vs-alternate.
         - If no clean alternative: emit the original primary-close envelope with
           secondary_breach_check.result=DEFERRED_TO_PM (the design's "execute anyway — primary
           takes priority" path).
      4. Post-execution re-evaluation: same as orchestrate_margin_call_cascade step 3, applied
         to whichever envelope was emitted.

    Returns:
        A tuple of EngineEnvelope. Same structure and ordering rules as the margin-call cascade.
    """
```

### 4. Alternate-position search

For a primary breach where the natural position-selection result introduces a secondary breach, the orchestrator searches for an alternate position that:
- Cures the primary breach (rule moves out of FAIL after the close).
- Does NOT introduce a new secondary breach.

Search procedure (deterministic):
1. Build the candidate set: all open positions excluding the originally-selected one. For sector-concentration breaches, restrict to positions in the breaching sector. For directional breaches, restrict to positions on the correct side. For other rules, the full set.
2. Sort candidates by the *next-best* selection criterion for the breach type (e.g., for sector concentration: by sector contribution descending after excluding the original; for drawdown: by next-largest unrealized loss).
3. For each candidate in order:
   a. Construct a `ProposedClose` for the alternate.
   b. Run `check_secondary_breach`.
   c. If `NO_SECONDARY_BREACH`: alternate found; return it along with a re-evaluation that confirms the primary is cured.
   d. If `DEFERRED_TO_PM`: skip; try the next candidate.
4. If no candidate clears: return None (orchestrator emits the original envelope with DEFERRED_TO_PM).

Bound the search at `context.config.cascade_max_steps` candidates (defensive — prevents pathological long searches in synthetic test cases). Early-stopping at first clean alternate is the default.

```python
def search_for_alternate_position(
    *,
    primary_rule: str,
    primary_position_selection: PositionSelectionResult,
    open_positions: tuple[PositionRecord, ...],
    liquidity: tuple[PositionLiquidity, ...],
    current_state: PortfolioStateSnapshotProtocol,
    library_config: LibraryConfigProtocol,
    market_inputs: MarketInputsProtocol,
    config: BreachBehaviorConfig,
) -> tuple[PositionSelectionResult, ProposedClose] | None:
    """Search for an alternate position that cures primary without secondary breach.

    Returns a (selection_result, proposed_close) tuple if found, None otherwise.

    Implementation sketch in section 4 above.
    """
```

### 5. CascadeStepLimitExceeded

```python
class CascadeStepLimitExceeded(Exception):
    """Raised when a cascade chain reaches config.cascade_max_steps."""

    def __init__(self, chain_length: int, max_steps: int, last_breach_rule: str | None) -> None:
        self.chain_length = chain_length
        self.max_steps = max_steps
        self.last_breach_rule = last_breach_rule
        msg = (
            f"Cascade chain length {chain_length} exceeded max_steps {max_steps}; "
            f"last breach was {last_breach_rule!r}"
        )
        super().__init__(msg)
```

The exception carries the chain length, max, and last breach rule; the caller logs and surfaces a structural-error alert. The continuous monitor's policy on hitting this exception is escalation (typically: emit an emergency invocation if not already in flight, and pause further between-invocation actions until the next scheduled invocation).

### 6. Tests

Tests at `tests/risk_guardrails/breach_behavior/test_cascade.py`:

#### Cascade ID generation

- **Happy path:** `monitor_session_id="s1"`, `initial_trigger_id=5` → `"CASCADE.s1.5"`.
- **Rejects empty session ID, dot-containing session ID, `initial_trigger_id < 1`** with `ValueError`.

#### Margin call cascade — happy path (single envelope)

- **A7 reproduction (no cascade beyond initial liquidation):** elevated regime, 3 short positions; margin call $3K. The COIN short has the worst R/R; full close cures the primary AND does not introduce secondary breaches (per A7's walkthrough). Output is a single-envelope tuple with:
  - `envelope_id = "MON.s1.5"` (initial trigger_id from context)
  - `guardrail_trigger_record.rule_breached = "margin_call"`
  - `guardrail_trigger_record.cascade_id = "CASCADE.s1.5"`
  - `guardrail_trigger_record.secondary_breach_check_result.result = "no_secondary_breach"`
  - `command.position_id = "POS-COIN-001"` (or whatever the test fixture names)
  - `command.quantity_or_all = "all"` (margin-call selection is full close)

#### Margin call cascade — multi-envelope chain

- **Initial liquidation introduces a secondary breach:** the close pushes net long over the limit. Cascade emits:
  - Envelope #1: initial liquidation, `secondary_breach_check_result=DEFERRED_TO_PM` (the design's "execute anyway — primary takes priority" — for margin calls, the primary IS the broker's call; secondary breaches are always logged-not-cured for margin calls).
  - Envelope #2 onwards (if any): post-liquidation forced reductions for any *new* deferred-classification breaches that themselves are immediate-engine on follow-up. (In practice for margin calls, the initial liquidation's secondary breach is always logged, not cured by additional liquidation — extra cascade steps fire only if some other rule's breach response is immediate_engine and a new breach of that rule surfaced post-liquidation. Tests construct this scenario with a fixture.)
- **Cascade max-steps respected:** synthetic scenario where each step introduces a new breach indefinitely. With `cascade_max_steps=4`, the cascade emits 4 envelopes and raises `CascadeStepLimitExceeded` on the 5th.

#### Non-margin breach cascade — single envelope, no secondary

- **Primary close has no secondary breach:** orchestrate_breach_cascade with a sector-concentration breach + clean primary close. Output: single envelope, `secondary_breach_check_result=NO_SECONDARY_BREACH`.

#### Non-margin breach cascade — alternate found

- **Primary close has secondary breach; alternate clears:** orchestrate_breach_cascade with a sector-concentration breach where closing position A causes net-long breach but closing position B (same sector, slightly different size) does not. Output: single envelope on position B, `secondary_breach_check_result=SECONDARY_BREACH_AVOIDED`, notes describing the swap from A to B.

#### Non-margin breach cascade — no alternate, deferred

- **No alternate clears:** every position in the breaching sector causes a secondary net-long breach when closed. Output: single envelope on the original primary position (position A), `secondary_breach_check_result=DEFERRED_TO_PM`, notes naming the secondary rule.

#### Alternate-position search

- **Returns first-found alternate:** input has 3 candidate positions; the second-tried clears. Output: `(selection_result_for_position_2, proposed_close_for_position_2)`.
- **Returns None when no candidate clears:** all candidates introduce secondary breaches.
- **Search bound respected:** with `cascade_max_steps=2`, search visits at most 2 candidates before giving up (returns None even if a 3rd candidate would clear).
- **Restricted candidate set per breach type:** for a sector breach in tech, the candidate set is positions in tech only (excluding the original); for net-long breach, the candidate set is long positions (excluding the original); etc.

#### Validation

- **Empty open_positions raises:** both orchestrators reject empty position lists with `ValueError`.
- **Missing liquidity coverage raises:** position list has 3 entries, liquidity has 2 → `ValueError`.

#### Determinism

- **Pure functions (modulo library calls):** repeated calls with identical inputs produce equal envelope tuples (same envelope IDs, same cascade_id, same content).
- **Frozen output:** envelopes in the returned tuple are frozen.

#### Worked example — A7 cascade

The A7 design walkthrough:
> Margin call $3K. Three short positions: COIN 8%, SQ 7%, HOOD 7%. Worst R/R is COIN (closest to invalidation).
> Engine issues CLOSE for COIN short ($8K notional > $3K margin call). Post-liquidation:
> - Short exposure 22% → 14% (no breach)
> - Net long 30% → 38% (limit 45% — OK)
> - Gross 74% → 66% (limit 90% — OK)
> No secondary breaches. Cascade is single-envelope.

Test asserts the orchestrator output matches: one envelope, COIN closed, `secondary_breach_check_result=NO_SECONDARY_BREACH`.

Out of scope:
- The continuous monitor's session-state management (trigger_id assignment, cascade_id persistence) — execution-layer.
- Activity-log persistence of cascade chain events — execution-layer.
- The agent-side rendering of cascade events in the PM's `Recent engine-originated actions` block — state-delivery.
- Cancelling pending limit orders during cascade — orthogonal concern; cascade orchestration only emits CLOSE commands.
- Margin requirement re-computation after each step (does the next step's close still leave us short of margin?) — the broker's response handling is execution-layer; this orchestrator emits envelopes based on the current breach classification at each step, not based on margin-requirement satisfaction.

## Notes

**Why two distinct orchestrator functions (margin-call vs. non-margin breach).** Margin calls have unique semantics: priority over all other rules, no alternate-position search (the broker's call must be met regardless of secondary breach), and a different position-selection rule (worst R/R rather than rule-specific selection). Splitting into two orchestrators keeps each function's flow clear and avoids a bloated unified function with branching for "is this a margin call?" Per `feedback_simplify_before_building.md`.

**Why the alternate-position search bound is `cascade_max_steps`.** The design does not specify a search bound. Using `cascade_max_steps` (default 8) reuses an existing config knob and gives operators a single tuning point for both cascade-chain depth and alternate-search depth. The bound is defensive — typical alternate searches clear after 1–2 candidates.

**Why margin-call cascades log secondary breaches as `DEFERRED_TO_PM` rather than searching for alternates.** The margin call's primary is the broker's external deadline; the engine must liquidate. Even if an alternate position would cure the secondary breach, the margin call's worst-R/R selection rule is the design's choice — alternate selection would amount to overriding the broker's preferred-liquidation criterion. The PM handles the secondary at the next invocation. Per the design's "if no clean cure exists, execute anyway — primary takes priority" wording.

**Per `feedback_avoid_numeric_anchors.md`,** the cascade max-steps comes from `BreachBehaviorConfig`. No hardcoded chain-length cap.

**Per `feedback_no_inventing_component_names.md`,** function names match the design's terminology directly: `orchestrate_margin_call_cascade` and `orchestrate_breach_cascade` align with "margin call cascade handling" and the broader "cascade" wording. `CascadeStepLimitExceeded` names the structural-error condition.

**Per `feedback_per_producer_schema.md`,** `EngineEnvelope` has a single producer at the assembler layer (story 06). Both cascade orchestrators call the assembler; neither builds envelopes by hand.

**Library-evaluation cost.** The cascade orchestrator may invoke `evaluate_proposals` 1 + N + (alternate_search_count) times per cascade. For typical cascades (1 step, 0-1 alternate), that's 2-3 calls. The library is pure and deterministic; caching is a future optimization if profiling reveals it's a bottleneck. Per `feedback_simplify_before_building.md`, no caching in the initial implementation.

## Acceptance criteria

- [ ] `src/alphamind/risk_guardrails/breach_behavior/cascade.py` exists and defines:
  - `generate_cascade_id`
  - `CascadeContext` (frozen dataclass)
  - `orchestrate_margin_call_cascade`
  - `orchestrate_breach_cascade`
  - `search_for_alternate_position`
  - `CascadeStepLimitExceeded`
- [ ] All public symbols re-exported from `src/alphamind/risk_guardrails/breach_behavior/__init__.py`.
- [ ] `generate_cascade_id` produces `"CASCADE.{session}.{trigger_id}"`; rejects empty/dot-containing session and `trigger_id < 1`.
- [ ] Margin-call cascade A7 reproduction produces a single-envelope tuple with `secondary_breach_check_result.result="no_secondary_breach"`, COIN closed, cascade_id populated.
- [ ] Margin-call cascade with secondary breach produces one envelope with `secondary_breach_check_result.result="deferred_to_pm"` (margin call always primary; never alternate-search).
- [ ] Non-margin cascade with no secondary breach produces single envelope with `result="no_secondary_breach"`.
- [ ] Non-margin cascade with secondary breach AND clean alternate produces single envelope on the alternate with `result="secondary_breach_avoided"` and notes describing the swap.
- [ ] Non-margin cascade with secondary breach AND no clean alternate produces single envelope on the original with `result="deferred_to_pm"` and notes naming the secondary rule.
- [ ] Cascade chain of N envelopes shares the same `cascade_id` across all envelopes.
- [ ] Cascade chain envelope_ids use distinct, monotonic trigger_ids starting at `context.initial_trigger_id`.
- [ ] `cascade_max_steps` enforced: synthetic infinite-cascade scenario raises `CascadeStepLimitExceeded` after `max_steps` envelopes; the exception carries `chain_length`, `max_steps`, `last_breach_rule`.
- [ ] Alternate-position search returns the first-found clean alternate.
- [ ] Alternate-position search returns `None` when no candidate clears.
- [ ] Alternate-position search bound by `cascade_max_steps` candidates.
- [ ] Alternate-position search restricts the candidate set by breach type (sector breach: same-sector positions only; directional: same-side; etc.).
- [ ] Empty `open_positions`, missing liquidity, missing R/R, etc. raise `ValueError`.
- [ ] Determinism: 100 repeated calls with identical inputs produce equal cascade outputs.
- [ ] Returned envelopes are frozen.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
