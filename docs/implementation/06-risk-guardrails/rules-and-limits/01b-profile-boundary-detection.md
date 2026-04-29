---
status: done
completed_date: 2026-04-28
commit_id: f74ec1f
---

# 01b — Profile boundary detection

## Goal

Land a pure function that classifies the active portfolio's `total_equity` against the active profile's `capital_range_usd` as `within`, `above` (graduation candidate), or `below` (downgrade candidate). This is the predicate the command-center alerts framework's `Profile boundary crossed` rule fires on, and it backs the Operational advisory described in `rules-and-limits.md § Transitioning between profiles`. Profile transitions are operator-driven; this story ships the *detection* surface, not the operator handler (story 01d) and not the alerting wiring (command-center work).

## Reading

- `docs/design/06-risk-guardrails/rules-and-limits.md` § Transitioning between profiles — boundary-crossing semantics, symmetric upward/downward treatment, no automatic downgrade
- `docs/design/06-risk-guardrails/rules-and-limits.md` § Portfolio size profiles — `capital_range_usd` for each profile (micro `[0, 4999]`, small per shipped YAML, etc.)
- `docs/design/command-center.md` § Default rule set — the `Profile boundary crossed` alert row (Operational severity, `portfolio_summary.total_equity` against `capital_range_usd`)
- `docs/design/configuration-management.md` § Composition model — confirms profile transitions are manual, not derived from the cascade
- `src/alphamind/config/models/profiles.py` — `ProfileConfig.capital_range_usd: tuple[int, int]` (lower, upper)
- `src/alphamind/config/models/main.py` — `Profile` enum (the four members)
- `config/profiles/{micro,small,medium,large}.yaml` — shipped `capital_range_usd` ranges to test against

## Depends on

None. The configuration-management feature has shipped `ProfileConfig` and the four profile files.

## Scope

In scope:

- `src/alphamind/risk_guardrails/rules_and_limits/profile_boundary.py` defining:
  - `ProfileBoundaryStatus` (StrEnum): members `within`, `above`, `below`. `within` means `lower ≤ total_equity ≤ upper`; `above` means `total_equity > upper` (graduation candidate); `below` means `total_equity < lower` (downgrade candidate). The bounds are inclusive on both ends — a portfolio at exactly `capital_range_usd[1]` is `within`, not `above`.
  - `ProfileBoundaryEvaluation` (frozen, slots dataclass): `status: ProfileBoundaryStatus`, `total_equity_usd: float`, `lower_bound_usd: int`, `upper_bound_usd: int`. The dataclass exists so callers (e.g., the alert framework) can include the comparison context in alert payloads without recomputing.
  - `evaluate_profile_boundary(*, total_equity_usd: float, profile: ProfileConfig) -> ProfileBoundaryEvaluation` — the detection function. Pure: no I/O, no logging, no exceptions on the happy path. Raises `ValueError` if `total_equity_usd < 0` (a negative equity reading is a structural failure of the upstream portfolio-state computation; surfacing it here prevents a silent `below` classification on a corrupt input).
- `src/alphamind/risk_guardrails/rules_and_limits/__init__.py` re-exports `ProfileBoundaryStatus`, `ProfileBoundaryEvaluation`, `evaluate_profile_boundary`. Combine with any other re-exports landing in parallel stories via the natural union merge.
- Unit tests covering:
  - **Within bounds.** Equity at the lower bound, the upper bound, and the midpoint — all return `ProfileBoundaryStatus.within`. Test against the shipped micro profile's `[0, 4999]` and the medium profile's range.
  - **Above bounds.** Equity at `upper + 1`, `upper * 2` — both return `ProfileBoundaryStatus.above`.
  - **Below bounds.** Equity at `lower - 1` (where lower > 0; e.g., the small profile's lower bound) — returns `ProfileBoundaryStatus.below`.
  - **Boundary inclusivity.** Equity equal to `lower_bound` is `within`, not `below`. Equity equal to `upper_bound` is `within`, not `above`.
  - **Negative equity raises.** `total_equity_usd = -1.0` raises `ValueError` whose message names the offending value.
  - **Returned dataclass content.** `evaluate_profile_boundary(total_equity_usd=10000.0, profile=medium)` returns an evaluation whose `total_equity_usd == 10000.0`, `lower_bound_usd == medium.capital_range_usd[0]`, `upper_bound_usd == medium.capital_range_usd[1]`.
  - **Across all four shipped profiles.** Parametrize a test that constructs the four shipped profile configs (loaded via `load_profiles`) and asserts the function returns `within` for an equity value at the midpoint of each profile's range.

Out of scope:

- The alerting framework itself — `config/alerts.yaml` lands with the command-center work; this story only ships the predicate.
- Profile-switching execution (operator → handler) — story 01d.
- Continuous monitoring of `total_equity` — that loop runs in the continuous monitor or the command-center polling loop; this story only ships a stateless evaluation function.
- A history of past boundary crossings — the activity-log pattern owns that surface; the predicate is stateless.

## Notes

**Inclusivity choice.** The shipped profiles' `capital_range_usd` ranges are non-overlapping and non-touching by design — the semantic-self-test invariant in story 06b enforces this. Inclusive bounds on both ends are unambiguous because no equity value can fall on two profiles' boundaries simultaneously. The choice matches the operator-natural reading: "$1,500 is in the micro profile (range [0, 4999])."

**No automatic downgrade.** Per the design doc: "the system never automatically downgrades, which would otherwise force-close positions in newly-disabled features." The detection function returns `below` (downgrade candidate); the operator decides whether to act. This story does not block any system behavior on a `below` result; it is informational input to the alert.

**Pure-function discipline.** No reading from `main.yaml`, no querying portfolio state, no logging. Every input is a parameter. Mirrors the discipline of the configuration-management resolver (story 05) and the cross-reference / semantic-self-test validators (stories 06a / 06b).

**`ProfileBoundaryEvaluation` rather than just returning the enum.** The alert framework (when the command-center work lands) needs the bounds and the equity value to render the alert payload. Returning a dataclass with the full context avoids the alert wiring re-fetching the profile and recomputing.

**`ValueError` on negative equity.** A negative `portfolio_summary.total_equity` would mean the portfolio is in liquidation territory and the upstream computation has a sign error or has caught a bracketed margin call mid-cascade. Either case is a structural failure that should surface loudly. The function is meant to be called against a freshly-computed `portfolio_summary`, so the exception is information for the caller, not a runtime hazard.

**Why `tuple[int, int]` for the bounds, not floats.** `ProfileConfig.capital_range_usd` is typed as `tuple[int, int]` per story 04a. The shipped values are whole-dollar integers. Comparing a float `total_equity_usd` against integer bounds works without coercion in Python; the function preserves the integer types in the returned dataclass for downstream rendering.

## Acceptance criteria

- [ ] `src/alphamind/risk_guardrails/rules_and_limits/profile_boundary.py` exists and defines `ProfileBoundaryStatus`, `ProfileBoundaryEvaluation`, `evaluate_profile_boundary`.
- [ ] `ProfileBoundaryEvaluation` is a `dataclass(frozen=True, slots=True)`.
- [ ] `src/alphamind/risk_guardrails/rules_and_limits/__init__.py` re-exports the three names.
- [ ] A unit test asserts `evaluate_profile_boundary(total_equity_usd=10000.0, profile=<medium>)` returns `ProfileBoundaryStatus.within` (10000 sits inside the shipped medium `capital_range_usd`).
- [ ] A unit test asserts equity at the lower bound (e.g., `0.0` for micro) returns `within`.
- [ ] A unit test asserts equity at the upper bound (e.g., `4999.0` for micro) returns `within`.
- [ ] A unit test asserts equity above the upper bound (e.g., `5000.0` for micro) returns `above`.
- [ ] A unit test asserts equity below the lower bound (e.g., `4999.0` for small) returns `below` against the small profile.
- [ ] A unit test asserts `evaluate_profile_boundary(total_equity_usd=-1.0, profile=<any>)` raises `ValueError` whose message names the negative input.
- [ ] A unit test parameterizes over the four shipped profiles and asserts each profile's midpoint equity returns `within`.
- [ ] A unit test asserts the returned `ProfileBoundaryEvaluation` carries `total_equity_usd`, `lower_bound_usd`, `upper_bound_usd` matching the inputs.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
