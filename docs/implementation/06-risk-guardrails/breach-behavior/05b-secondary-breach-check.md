---
status: in_progress
completed_date:
commit_id:
---

# 05b — Secondary breach check

## Goal

Land the deterministic primitive that, before a protective CLOSE is finalized, verifies the proposed close would not introduce a secondary breach on a *different* rule. The classic case: closing a short position that provides directional balance could push net long over its limit. The primitive consumes the proposed close (instrument + size + direction), the current portfolio state, and produces a `SecondaryBreachCheckResult` naming the outcome (`no_secondary_breach`, `secondary_breach_avoided`, `deferred_to_pm`). It composes the guardrail-evaluation library's `evaluate_proposals` to project post-close rule status and detects newly-introduced FAIL'd rules.

Per `breach-behavior.md § Secondary breach checking`, the primitive's caller (continuous monitor, story 06's envelope assembler, or story 07's cascade orchestrator) uses the result to:
- `no_secondary_breach` → finalize the close
- `secondary_breach_avoided` → an alternate position was found and re-evaluated cleanly; finalize on the alternate
- `deferred_to_pm` → no clean cure exists; finalize the close anyway (primary breach takes priority) but flag the secondary breach for the PM at the next invocation

This story produces the *check*; orchestration of the alternate-position search is in story 07.

## Reading

- `docs/design/06-risk-guardrails/breach-behavior.md` § Secondary breach checking — the authoritative contract:
  > Before any forced reduction, the engine verifies the action wouldn't breach a different rule. Most common case: closing a short providing directional balance could push net long beyond its limit.
  >
  > If a secondary breach would result:
  > 1. Log the conflict with both the primary and secondary breach
  > 2. Select an alternative position that cures the primary without creating a secondary breach
  > 3. If no clean cure exists, execute anyway — primary takes priority, secondary is flagged for the PM at the next invocation.
- `docs/design/05-execution-layer/engine-envelope-schema.md` § `secondary_breach_check_result` — the typed surface the engine envelope carries:
  > `result: enum [no_secondary_breach, secondary_breach_avoided, deferred_to_pm]`
- `docs/design/06-risk-guardrails/guardrail-evaluation.md` § Per-rule projection and evaluation — the library primitive this story composes against. Specifically the `Status.FAIL` classification and the per-rule output shape.
- `docs/implementation/06-risk-guardrails/guardrail-evaluation/05-evaluate-proposals-entry-point.md` — the `evaluate_proposals(...)` function this story calls.
- `src/alphamind/risk_guardrails/breach_behavior/types.py` — `SecondaryBreachCheckResult`, `SecondaryBreachOutcome` (story 03).
- `src/alphamind/risk_guardrails/breach_behavior/config.py` — `BreachBehaviorConfig.delta_buffer_secondary_check_buffer_factor` (the multiplier on guardrail-evaluation's buffered delta for the cascade re-evaluation; default 1.0 = unchanged).
- `docs/design/06-risk-guardrails/scenario-tests.md` § A6 — confirms a short-close cascade that *doesn't* introduce a secondary breach (the ordinary case); this story's test suite covers both the introduce-secondary case and the no-secondary case.

## Depends on

- 02 (package skeleton + config — for the buffer factor)
- 03 (canonical types — `SecondaryBreachCheckResult`, `SecondaryBreachOutcome`)
- 04a (zone classifier — used to determine which rules are over-limit pre-close vs. post-close)
- 04d (position selection — the proposed close originates from a position-selection result)
- **Cross-feature gate:** the guardrail-evaluation library's `evaluate_proposals` and its `LibraryOutput`/`RuleProjection` types must be available. If the guardrail-evaluation work tree's stories 04 and 05 are not yet `done`, dispatch this story against a Protocol stub matching the documented `evaluate_proposals` signature and library output shape; replace with the production import in a re-dispatch (or follow-up edit) once the gate clears. Per the orchestrator's cross-feature-gate convention, the orchestrator alerts the user before dispatching this story if guardrail-evaluation is incomplete.

## Scope

In scope, all under `src/alphamind/risk_guardrails/breach_behavior/secondary_breach.py`. Tests at `tests/risk_guardrails/breach_behavior/test_secondary_breach.py`.

### 1. Public function

```python
def check_secondary_breach(
    *,
    proposed_close: ProposedClose,
    current_state: PortfolioStateSnapshotProtocol,
    library_config: LibraryConfigProtocol,
    market_inputs: MarketInputsProtocol,
    primary_breach_rule_id: str,
    config: BreachBehaviorConfig,
) -> SecondaryBreachCheckResult:
    """Check whether a proposed protective close would introduce a secondary breach.

    Composes guardrail-evaluation's evaluate_proposals to project post-close rule status.
    Identifies newly-introduced FAIL'd rules — i.e., rules that were PASS or WARNING before
    the close but become FAIL after — excluding the primary_breach_rule_id (which the close
    is intended to cure).

    Returns:
      - SecondaryBreachOutcome.NO_SECONDARY_BREACH when the post-close projection has zero new
        FAIL'd rules.
      - SecondaryBreachOutcome.SECONDARY_BREACH_AVOIDED is NOT returned by this primitive — that
        outcome is reserved for callers (story 07's cascade orchestrator) that perform an
        alternate-position search and re-invoke this primitive on the alternate. The alternate's
        re-check returning NO_SECONDARY_BREACH lets the orchestrator emit the SECONDARY_BREACH_AVOIDED
        outcome at the envelope-record layer.
      - SecondaryBreachOutcome.DEFERRED_TO_PM when at least one new FAIL'd rule appears post-close.
        The notes field names the rule(s) breached secondarily.

    Args:
        proposed_close: The close being evaluated. ProposedClose carries the position_id,
            instrument (ticker, asset_type, direction), close size (full or partial USD/quantity),
            and pre-close size as % of portfolio. The library translates this into a CLOSE-action
            ProposedDelta.
        current_state: The portfolio state snapshot the close evaluates against.
        library_config: The guardrail-evaluation library's LibraryConfig for the active profile/regime.
        market_inputs: Market data for the library (underlying prices, IV surface, risk-free rate).
        primary_breach_rule_id: The rule the protective close is curing. Excluded from secondary-
            breach detection — by definition, the close is supposed to address this rule.
        config: BreachBehaviorConfig. The buffer factor scales the library's delta buffer for
            this re-evaluation; default 1.0 reuses the library's standard buffer.

    Returns:
        A frozen SecondaryBreachCheckResult with `result` (NO_SECONDARY_BREACH or DEFERRED_TO_PM)
        and `notes` (list of breaching rule IDs when DEFERRED_TO_PM; brief descriptive text
        otherwise).

    Raises:
        ValueError: when proposed_close fields conflict (e.g., close size > pre-close size),
            when primary_breach_rule_id is not in the library's active rule set, or when the
            library raises during evaluation.
    """
```

### 2. ProposedClose value object

```python
class ProposedClose(BaseModel):
    """A protective close awaiting secondary-breach validation.

    Translates from a PositionSelectionResult into a library-compatible delta description.
    """

    model_config = ConfigDict(frozen=True)

    position_id: str
    ticker: str
    asset_type: Literal["equity", "option", "strategy"]
    direction: Literal["long", "short"]
    pre_close_size_pct_of_portfolio: float
    close_size_pct_of_portfolio: float              # equals pre_close_size for FULL_CLOSE; smaller for PARTIAL_TRIM
    pre_close_size_usd: float
    close_size_usd: float

    @model_validator(mode="after")
    def _validate_close_within_position(self) -> ProposedClose:
        if self.close_size_pct_of_portfolio > self.pre_close_size_pct_of_portfolio + 1e-9:
            msg = (
                f"close_size_pct_of_portfolio ({self.close_size_pct_of_portfolio}) "
                f"exceeds pre_close_size_pct_of_portfolio ({self.pre_close_size_pct_of_portfolio})"
            )
            raise ValueError(msg)
        if self.close_size_usd > self.pre_close_size_usd + 1e-3:
            msg = "close_size_usd exceeds pre_close_size_usd"
            raise ValueError(msg)
        if self.close_size_pct_of_portfolio <= 0:
            msg = "close_size_pct_of_portfolio must be > 0"
            raise ValueError(msg)
        return self
```

### 3. Library composition

The function composes guardrail-evaluation's `evaluate_proposals` twice — once on `current_state` with no proposals (to obtain the baseline per-rule status), once on `current_state` with the close as a `ProposedDelta` of `action=CLOSE` (to obtain post-close per-rule status). The second invocation is the projection that reveals the new FAIL'd rules.

The pre-evaluation step is needed because some rules may already be in FAIL state pre-close (e.g., the primary breach itself, plus any concurrent breaches). The function classifies a rule as a *secondary breach* only when its status changes from non-FAIL to FAIL across the two evaluations:
- pre-status: PASS or WARNING; post-status: FAIL → secondary breach.
- pre-status: FAIL; post-status: FAIL → pre-existing breach; not secondary.
- pre-status: FAIL; post-status: PASS or WARNING → cured (the primary or a concurrent breach).
- pre-status: PASS/WARNING; post-status: PASS/WARNING → unchanged or improved.

The `primary_breach_rule_id` is always excluded from secondary-breach detection (it would otherwise show as cured rather than introduced; the exclusion is defensive in case the close doesn't fully cure).

### 4. Buffer factor application

`config.delta_buffer_secondary_check_buffer_factor` scales the library's delta buffer for the secondary check. Default `1.0` reuses the library's standard buffer unchanged. Higher values (e.g., `1.2`) tighten the buffer further — useful for cascade re-evaluations where fast-moving markets demand extra conservatism. The factor is passed through to the library's config (the library accepts a `delta_buffer_factor` or equivalent runtime override per its primitive contract; if the library does not accept a runtime buffer override at the time of dispatch, the implementing subagent surfaces the gap and proposes a follow-up).

For the initial implementation, if the library lacks a runtime buffer override, this story's primitive simply does not apply the factor and the test for `buffer_factor != 1.0` is marked skipped with a TODO comment. Acceptance still passes for the default `1.0` case. The orchestrator notes the gap when reviewing the verification report.

### 5. Tests

Tests at `tests/risk_guardrails/breach_behavior/test_secondary_breach.py`. Use stubbed library output (`LibraryOutputProtocol` from the hard-rejection story's Protocol definitions) so tests do not require the full guardrail-evaluation library to be implemented:

#### No-secondary-breach scenarios

- **Closing a long that doesn't push net short over limit:** stubbed library returns pre-status all PASS, post-status all PASS. Result: `NO_SECONDARY_BREACH`, notes are short descriptive text.
- **Closing a short that's well within net long headroom:** pre-status: net_long_pct WARNING (below FAIL threshold), post-status: net_long_pct WARNING (still below FAIL). Result: `NO_SECONDARY_BREACH`.
- **Closing a position that cures the primary without touching other rules:** pre-status: position_max_loss_equity_pct FAIL (the primary), all others PASS; post-status: all PASS. Result: `NO_SECONDARY_BREACH`.

#### Deferred-to-PM scenarios

- **Closing a short pushes net long over limit:** pre-status: total_short_pct FAIL (primary), net_long_pct PASS; post-status: total_short_pct PASS, net_long_pct FAIL. Result: `DEFERRED_TO_PM`, notes name `net_long_pct` as the secondary breach.
- **Multiple secondary breaches:** post-status has FAIL on net_long_pct and gross_exposure_pct that were both PASS pre. Notes list both rules.
- **Pre-existing breach not classified as secondary:** pre-status: net_long_pct FAIL (concurrent; not the primary), post-status: net_long_pct FAIL (still). Result: `NO_SECONDARY_BREACH` (the close did not introduce the breach; the rule was already breaching pre-close). Notes acknowledge the pre-existing concurrent breach.
- **Cured-by-close rule does not count as secondary:** pre-status FAIL → post-status PASS for the primary rule. Not in the secondary breach set.

#### Validation

- **`primary_breach_rule_id` not in active rule set raises:** library config does not carry `nonexistent_rule` → `ValueError`.
- **`ProposedClose.close_size_pct > pre_close_size_pct` raises at construction:** ProposedClose post-validator catches this.
- **Library evaluation raises propagates:** if `evaluate_proposals` raises (e.g., `LibraryInputError`), the primitive re-raises with a descriptive wrapper message naming the secondary-check context.

#### Determinism

- **Pure function:** repeated calls with identical inputs produce equal results.
- **Frozen output:** assigning to a returned `SecondaryBreachCheckResult` field raises `ValidationError`.

#### Worked example — A6 short squeeze

The scenario A6 in `scenario-tests.md` walks through an engine protective CLOSE on a short that hit position-level max loss. The walkthrough confirms:
> Secondary breach check: Closing the short reduces short exposure (good) but doesn't create any new breaches since it's reducing risk.

A test reproduces this: pre-status has `position_max_loss_equity_pct=FAIL` for the breaching short, all other rules PASS. Post-close projection has all rules PASS (the short is gone; net long, gross, total short all reduced). Result: `NO_SECONDARY_BREACH`.

#### Worked example — short close that pushes net long over

A constructed test: portfolio has $30K short and $50K long, total portfolio $100K. Net long = (50 - 30) / 100 = 20%. Net long limit = 25%. Closing the entire $30K short brings net long to 50/100 = 50% — over the 25% limit. Pre-status: total_short_pct=FAIL (some primary like total-short overage); net_long_pct=PASS. Post-close: total_short_pct=PASS, net_long_pct=FAIL. Result: `DEFERRED_TO_PM` with notes naming `net_long_pct`.

Out of scope:
- The alternate-position search that may produce the `SECONDARY_BREACH_AVOIDED` outcome — that's the cascade orchestrator's responsibility (story 07).
- Logging the conflict to the activity log — the engine envelope assembler (story 06) and the activity log writer handle persistence.
- The PM's response to the deferred secondary breach — that's PM-agent territory, surfaced via state-delivery's PM header `Recent engine-originated actions` and the strategist's regime-transition / breach-context handling.
- Computing the post-close projection if the library is not available — the cross-feature gate handles this dispatch-time concern.

## Notes

**Why a single check primitive rather than separate "would-this-cause-net-long-breach" / "would-this-cause-sector-breach" / etc. primitives.** The library's `evaluate_proposals` returns the full per-rule projection in one call; running it twice (pre + post) and diffing the FAIL'd rules is cheaper, simpler, and more general than rule-specific predicates. Adding a new rule requires no change here; the library's per-rule projection picks it up automatically. Per `feedback_simplify_before_building.md`.

**Why `SECONDARY_BREACH_AVOIDED` is not produced here.** The outcome semantically means "we tried an alternate and it cleared." A primitive that takes a single proposed close and returns "we tried an alternate" doesn't compose cleanly — the alternate selection is a separate algorithmic step (which position to try next, when to give up). Splitting that into the cascade orchestrator (story 07) keeps this primitive's contract narrow: one close in, one classification out. The cascade orchestrator emits `SECONDARY_BREACH_AVOIDED` at the envelope-construction layer when its alternate-position-search succeeds.

**Per `feedback_avoid_numeric_anchors.md`,** no thresholds are hardcoded. The library's per-rule limits and zones come from the active config; the buffer factor comes from `BreachBehaviorConfig`.

**Per `feedback_no_inventing_component_names.md`,** function name `check_secondary_breach` matches the design's "Secondary breach checking" subsection. `ProposedClose` is a package-internal value object naming the data it carries (a proposed close action). `SecondaryBreachCheckResult` and `SecondaryBreachOutcome` come from the engine-envelope JSON Schema verbatim.

**Cross-feature dispatch discipline.** When the orchestrator dispatches this story, it surveys the guardrail-evaluation work tree's frontmatter:
```bash
rg "^status:" docs/implementation/06-risk-guardrails/guardrail-evaluation/[0-9]*.md
```
If story 05 (`evaluate-proposals-entry-point`) is not `done`, the dispatch surfaces the gap and the user decides:
1. Proceed against a Protocol stub (the implementing subagent uses a hand-written stub matching the documented LibraryOutput shape; tests stub library calls; production wiring lands in a follow-up).
2. Wait for guardrail-evaluation to land before dispatching this story.
The story file does not pre-decide; the orchestrator handles the decision.

**Buffer-factor TODO.** If the library does not yet support a runtime delta-buffer override, the buffer factor is read but not applied; the story's acceptance criteria ignore the factor's effect (default 1.0 behavior is the baseline). A follow-up story or a coordinated edit in the guardrail-evaluation work tree adds the runtime override.

## Acceptance criteria

- [ ] `src/alphamind/risk_guardrails/breach_behavior/secondary_breach.py` exists and defines `check_secondary_breach` and `ProposedClose` with the documented signatures.
- [ ] `check_secondary_breach`, `ProposedClose` re-exported from `src/alphamind/risk_guardrails/breach_behavior/__init__.py`.
- [ ] `ProposedClose` is a frozen Pydantic v2 model with the documented fields and post-validator.
- [ ] `ProposedClose._validate_close_within_position` rejects `close_size_pct > pre_close_size_pct` and `close_size_usd > pre_close_size_usd`.
- [ ] `ProposedClose._validate_close_within_position` rejects `close_size_pct_of_portfolio <= 0`.
- [ ] No-secondary-breach scenario: pre-status all PASS, post-status all PASS → `result=NO_SECONDARY_BREACH`.
- [ ] Curing-the-primary scenario: pre-status has primary rule FAIL, post-status has primary PASS, no other changes → `result=NO_SECONDARY_BREACH`.
- [ ] Introduced-breach scenario: pre-status PASS for net_long_pct, post-status FAIL for net_long_pct → `result=DEFERRED_TO_PM`, `notes` names `net_long_pct`.
- [ ] Multiple secondary breaches scenario: post-status introduces FAIL on net_long_pct and gross_exposure_pct → `notes` names both rules.
- [ ] Pre-existing concurrent breach (FAIL pre and post) is NOT classified as secondary; result reflects whether other rules introduce breaches.
- [ ] Primary rule (the one being cured) is excluded from secondary detection regardless of its post-status.
- [ ] Library evaluation error propagates with a wrapper message naming the secondary-check context.
- [ ] Determinism: 100 repeated calls with identical inputs produce equal results.
- [ ] Returned `SecondaryBreachCheckResult` is frozen.
- [ ] A6 worked example (engine protective close on max-loss short) produces `NO_SECONDARY_BREACH` per the design's documented walkthrough.
- [ ] Constructed example where closing a $30K short pushes net long from 20% to 50% over a 25% limit produces `DEFERRED_TO_PM` with `notes` naming `net_long_pct`.
- [ ] If guardrail-evaluation library is unavailable at dispatch, tests use a Protocol stub matching the documented `LibraryOutput` shape; the orchestrator notes the cross-feature gap in the verification report and proceeds.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
