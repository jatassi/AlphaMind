---
status: in_progress
completed_date:
commit_id:
---

# 01c — Minimum position size pre-check

## Goal

Land a pure pre-check function that rejects candidate positions whose dollar size falls below the active profile's `min_position_size_usd` floor. This is the per-profile economic-viability gate from `rules-and-limits.md` — micro's $50 minimum exists because below it bid-ask friction overwhelms the thesis signal; small's $75 minimum exists because thesis-tracking overhead isn't justified for smaller positions; medium's $75 and large's $100 follow the same logic. The check is profile-aware and independent of the 19 numeric guardrail rules — it's a structural floor rather than a percentage limit.

This story ships the *check function*. Wiring the check into the engine guardrail enforcement layer (T3) is owned by the execution-layer guardrail enforcement story; wiring it into the analyst/strategist validation tool is owned by `state-delivery`. Both downstream consumers call the function this story ships.

## Reading

- `docs/design/06-risk-guardrails/rules-and-limits.md` — the per-profile `min_position_size` field per profile section (Micro $50, Small $75, Medium $75, Large $100) with rationale (bid-ask friction, thesis-tracking overhead, P/L-per-move legibility)
- `docs/design/06-risk-guardrails/rules-and-limits.md` § Profile comparison summary — confirms `min_position_size` is a profile-level field, not a regime-modulated value
- `docs/design/06-risk-guardrails/state-delivery.md` § Guardrail validation tool — the validation tool is one of the consumers of this check
- `docs/design/05-execution-layer/architecture.md` § 3 — guardrail enforcement layer, the T3 consumer
- `src/alphamind/config/models/profiles.py` — `ProfileConfig.min_position_size_usd: int`
- `config/profiles/{micro,small,medium,large}.yaml` — shipped `min_position_size_usd` values

## Depends on

None. The configuration-management feature has shipped `ProfileConfig.min_position_size_usd`.

## Scope

In scope:

- `src/alphamind/risk_guardrails/rules_and_limits/min_position_size.py` defining:
  - `MinPositionSizeStatus` (StrEnum): members `pass_` and `fail`. (Underscore suffix on `pass_` because `pass` is a Python keyword.) The enum exists so consumers can branch without checking a bool — same shape as the per-rule `status` field in the guardrail-evaluation library's output.
  - `MinPositionSizeResult` (frozen, slots dataclass): `status: MinPositionSizeStatus`, `command_size_usd: float`, `min_size_usd: int`, `shortfall_usd: float`. `shortfall_usd` is `max(min_size_usd - command_size_usd, 0.0)` — zero on pass, positive on fail. Carrying the shortfall lets the caller render a useful failure message without recomputing.
  - `check_min_position_size(*, command_size_usd: float, profile: ProfileConfig) -> MinPositionSizeResult` — the pre-check. Pure: no I/O. Raises `ValueError` if `command_size_usd <= 0` (a zero-dollar or negative command is a structural error in the upstream PM/strategist envelope construction; surfacing it prevents a silent `pass_` on a corrupt input — every profile's min is positive, so a zero command would `fail`, but the `ValueError` carries clearer diagnostics than a generic shortfall message).
- The boundary semantics are inclusive on the floor: `command_size_usd == min_size_usd` is `pass_`. The shipped profiles' `min_position_size_usd` are exact-dollar floors per the design doc.
- `src/alphamind/risk_guardrails/rules_and_limits/__init__.py` re-exports `MinPositionSizeStatus`, `MinPositionSizeResult`, `check_min_position_size`. Combine with sibling stories' re-exports via the natural union merge.
- Unit tests covering:
  - **At the floor passes.** `command_size_usd == profile.min_position_size_usd` returns `pass_` with `shortfall_usd == 0.0`.
  - **Above the floor passes.** `command_size_usd == profile.min_position_size_usd + 1` returns `pass_`.
  - **Below the floor fails.** `command_size_usd == profile.min_position_size_usd - 1` returns `fail` with `shortfall_usd == 1.0`.
  - **Far below fails.** `command_size_usd == 1.0` against any shipped profile returns `fail` with `shortfall_usd == profile.min_position_size_usd - 1.0`.
  - **Zero size raises.** `command_size_usd = 0.0` raises `ValueError`.
  - **Negative size raises.** `command_size_usd = -100.0` raises `ValueError`.
  - **Returned dataclass.** Asserts every field of `MinPositionSizeResult` matches the inputs and the computed shortfall.
  - **Per-profile coverage.** Parametrized test over the four shipped profiles asserting `check_min_position_size(command_size_usd=p.min_position_size_usd, profile=p)` is `pass_` for each.
  - **Profile floor mismatch.** Asserts the function reads from the *passed* profile, not from a hard-coded default — i.e., a $60 command passes against the micro profile (min $50) but fails against the small profile (min $75).

Out of scope:

- Wiring into the engine T3 layer — owned by `docs/implementation/05-execution-layer/`'s guardrail enforcement story (not yet drafted).
- Wiring into the agent-side validation tool — owned by the `state-delivery` feature's validation-tool story (not yet drafted).
- Position-sizing recommendations or "minimum viable" suggestions — out of scope; the pre-check is a binary pass/fail. The PM's failure-guidance comes from the broader guardrail-evaluation library, not this floor check.
- Maximum position size enforcement — that's the `position_max_size_pct` guardrail rule, owned by the per-rule projection logic in `guardrail-evaluation`. This story is the floor counterpart (which has no rule entry in `guardrails.yaml`).

## Notes

**Why a separate function rather than a rule entry.** The 19 entries in `config/guardrails.yaml` are all *percentage-of-portfolio* rules with regime multipliers. `min_position_size_usd` is a *profile-level dollar floor* — it doesn't scale with regime, it doesn't have an escalation zone, it doesn't have an enforcement-tier hierarchy. Forcing it into the rule registry would either require breaking the rule entry's invariants (no regime multiplier, no zones) or inventing a synthetic regime multiplier of 1.0 with cosmetic zones. Cleaner to ship a separate pre-check.

**Inclusive floor semantics.** A position at exactly `min_position_size_usd` passes. The shipped values are integer dollars; floating-point command sizes (e.g., $50.00 from a fractional-share computation) compare cleanly against an integer floor in Python without coercion concerns. The inclusive boundary matches the operator reading: "the floor is $50."

**`MinPositionSizeStatus` rather than a bool.** Two reasons: (a) the per-rule output objects in `guardrail-evaluation` use a status enum, and aligning shapes lets future consumers compose results uniformly; (b) `pass_` / `fail` reads more cleanly in failure-rendering code than `passed=True/False`.

**`ValueError` on zero or negative command size.** A non-positive command is a structural error in the calling code (PM envelope construction, strategist action proposal, etc.). The function exists to gate compliant commands, not to defend against malformed ones. Surfacing the diagnostic loudly mirrors the configuration-management resolver's "structural error post-validation" exception pattern (story 05's `KeyError` on missing rule).

**No regime modulation.** `regime-adaptation.md` defines multipliers for the 19 percentage-based rules. `min_position_size_usd` is intentionally not in the multiplier table — economic viability of a $50 trade doesn't change with VIX. The function reads only from `ProfileConfig`, not from the resolved regime.

## Acceptance criteria

- [ ] `src/alphamind/risk_guardrails/rules_and_limits/min_position_size.py` exists and defines `MinPositionSizeStatus`, `MinPositionSizeResult`, `check_min_position_size`.
- [ ] `MinPositionSizeResult` is a `dataclass(frozen=True, slots=True)`.
- [ ] `MinPositionSizeStatus` is a `StrEnum` with members `pass_` and `fail`.
- [ ] `src/alphamind/risk_guardrails/rules_and_limits/__init__.py` re-exports the three names.
- [ ] A unit test asserts `check_min_position_size(command_size_usd=50.0, profile=<micro>)` returns `pass_` with `shortfall_usd == 0.0`.
- [ ] A unit test asserts `check_min_position_size(command_size_usd=49.99, profile=<micro>)` returns `fail` with `shortfall_usd == 0.01` (within float tolerance).
- [ ] A unit test asserts `check_min_position_size(command_size_usd=75.0, profile=<small>)` returns `pass_`.
- [ ] A unit test asserts `check_min_position_size(command_size_usd=60.0, profile=<small>)` returns `fail` (illustrating the profile-dependent floor).
- [ ] A unit test asserts `check_min_position_size(command_size_usd=0.0, ...)` raises `ValueError`.
- [ ] A unit test asserts `check_min_position_size(command_size_usd=-100.0, ...)` raises `ValueError`.
- [ ] A unit test parameterizes across the four shipped profiles and asserts the floor pass case for each (`command == min_position_size_usd`).
- [ ] A unit test asserts the returned `MinPositionSizeResult` carries `command_size_usd`, `min_size_usd`, `shortfall_usd` matching the inputs.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
