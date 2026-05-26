# 01e — Generalize premium_at_risk to capital-at-risk semantics

# 01e — Generalize premium_at_risk to capital-at-risk semantics

## Goal

Redefine the `premium_at_risk` field in place — same name, same type, new semantics: "capital at risk" = the USD magnitude of a position's worst-case loss. Today the field is debit-centric ("premium paid", `gt=0`), so a net-credit strategy's risk — (strike width minus credit received) — has no natural field. This is a no-rename, no-new-field change (parent decision): what changes is the documented meaning and the populate-it guidance, across the analyst output schema, the OMS command wire format, the guardrail validation tool, four design docs, and the PM prompt.

## Reading

* `src/alphamind/decision/analyst/models.py` — `PositionSize.premium_at_risk: Money | None = Field(default=None, gt=0)` and its docstring ("required for defined-risk options/strategies"; "premium paid").
* `src/alphamind/commands/command_models.py` — `PositionSize.premium_at_risk: Money | None = Field(default=None, gt=0)` (the OMS wire format).
* `src/alphamind/risk_guardrails/state_delivery/validation_tool.py` — `ValidationSize.premium_at_risk_usd: float | None` and its non-negative validator; `validation_tool_mcp.py` for the tool-facing description. Trace whether any guardrail check consumes the value.
* `src/alphamind/decision/proposal_pre_processor/translator.py` — passes `premium_at_risk` through; confirm no semantic transform is needed.
* `docs/design/04-decision-layer/analyst-output-schema.md`, `docs/design/04-decision-layer/portfolio-manager.md`, `docs/design/06-risk-guardrails/state-delivery.md`, `docs/design/05-execution-layer/oms-command-schema.md` — the four design docs describing `premium_at_risk`.
* `prompts/decision/pm.md` — references `premium_at_risk`.
* `docs/design/05-execution-layer/position-model.md` § Strategy position (per story 01a) — the capital-at-risk definition.
* [ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) (parent) § Resolved design decisions — `premium_at_risk` redefined in place.

## Depends on

Nothing. Wave 1.

## Scope

In scope: the field descriptions / docstrings on the three schema types, the four design docs, and the PM prompt.

### 1\. Field semantics — analyst + command schemas

Redefine `PositionSize.premium_at_risk` in both `decision/analyst/models.py` and `commands/command_models.py` as "capital at risk": the USD magnitude of the position's worst-case loss — premium paid for a net-debit options position or debit strategy; (strike width minus net credit received) for a net-credit strategy; left `None` for equity. The field name, the `Money | None` type, the `None` default, and the `gt=0` constraint are all unchanged (capital at risk is strictly positive when present). Only the docstrings and `Field` descriptions change.

### 2\. Validation tool

In `validation_tool.py`, update `ValidationSize.premium_at_risk_usd`'s field description and any "premium paid" wording to the capital-at-risk definition; the `>= 0` validator is unchanged. Update `validation_tool_mcp.py`'s tool-facing description if it restates the semantics.

### 3\. Design docs + PM prompt

Update the four design docs and `prompts/decision/pm.md` so every description of `premium_at_risk` carries the capital-at-risk semantics, and the "populate it for defined-risk positions" guidance reads coherently for a credit strategy.

### Out of scope

Renaming the field — the parent settled on redefine-in-place. The capital-at-risk *math* — story 03a (P/L %) and 03b (Reg T margin) compute their own values from `max_loss_usd`; this story is the schema-description + doc + prompt redefinition.

## Acceptance criteria

- [ ] `PositionSize.premium_at_risk` in `decision/analyst/models.py` and `commands/command_models.py` is documented as "capital at risk" — the USD magnitude of the position's max loss — naming both the debit (premium paid) and credit (strike width minus credit) cases.
- [ ] `ValidationSize.premium_at_risk_usd` in `validation_tool.py` carries the same capital-at-risk semantics in its description.
- [ ] The field name, type, `None` default, and `gt=0` / `>= 0` constraints are unchanged — this is a description-only redefinition.
- [ ] `analyst-output-schema.md`, `portfolio-manager.md`, `state-delivery.md`, `oms-command-schema.md`, and `prompts/decision/pm.md` describe `premium_at_risk` with the capital-at-risk semantics and read coherently for a credit strategy.
- [ ] Any guardrail check that consumes `premium_at_risk` / `premium_at_risk_usd` still behaves correctly — the value remains a positive USD risk magnitude.
- [ ] The full test suite passes.

## Verification

Iterative runs: `uv run pytest --testmon -n auto`. Final verification: `uv run pytest -n auto` **without** `--testmon` — the PM prompt `.md` changed and prompt-content tests are not tracked via Python imports (per CLAUDE.md). `uv run ruff check .` and `uv run mypy` clean. Inspect the four design docs and `pm.md` for coherent capital-at-risk wording.
