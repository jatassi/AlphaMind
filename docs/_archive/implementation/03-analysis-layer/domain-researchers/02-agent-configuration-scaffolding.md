## Goal

Verify the as-built `config/agents.yaml` against the design docs and land the small shared-types module the rest of the work tree imports. Two deliverables:

1. **Verification of** `config/agents.yaml`. The yaml already carries entries for `tech_semis_researcher`, `financials_researcher`, `energy_researcher` (commit `ff9f39d`). Confirm field-by-field against the design docs and `src/alphamind/config/models/agents.py`. The known divergence — `output_token_budget: 2000` for all three vs. design-doc ranges (400–800 / 300–600 / 300–600) — has been **operator-resolved: keep 2000 for all three.** No edit to that field; verification only.
2. **Shared-types module** `src/alphamind/analysis/_shared.py`. Defines the typed identifiers stories 03+ import: `Sector` (StrEnum aligned to existing strings), `TokensUsed` (Pydantic frozen model for harness/runner), and a re-export of `AnomalySeverity` from the distillation layer so analysis code does not redefine the literal.

## Reading

* `config/agents.yaml` — the as-built file; verify field shapes.
* `src/alphamind/config/models/agents.py` — the existing Pydantic model the loader uses.
* `docs/design/configuration-management.md` § `config/agents.yaml` — schema discipline + cross-reference / semantic-self-test invariants.
* `docs/design/03-analysis-layer/domain-researchers/tech-semis.md` § Output — design-doc token-budget range 400–800 (informational only; operator chose 2000).
* `docs/design/03-analysis-layer/domain-researchers/financials.md` § Output — design-doc range 300–600 (informational only; operator chose 2000).
* `docs/design/03-analysis-layer/domain-researchers/energy.md` § Output — design-doc range 300–600 (informational only; operator chose 2000).
* `docs/design/cost-and-rate-limit-modeling.md` § Per-invocation agent surface — model assignments and budgets.
* `src/alphamind/distillation/output.py` — existing `AnomalySeverity` literal type to re-export.
* `src/alphamind/distillation/sector_assembly.py` — `DOMAIN_RESEARCHER_BY_AUDIENCE` mapping; the `Sector` enum values must match these strings.
* `docs/architecture/llm-integration.md` § What the Agent SDK doesn't provide — guidance on `TokensUsed` shape.

## Depends on

None.

## Scope — Part 1: Verification of config/agents.yaml

Inspect each of the three domain-researcher entries and confirm field-by-field that `model` is `claude-sonnet-4-6`, the `prompt` path resolves to an existing `prompts/analysis/<name>.md` file, `tools` is an empty list, `latency_budget_seconds` (currently 120) is reasonable against `cost-and-rate-limit-modeling.md`, `context_token_budget` (currently 8000) is reasonable against the same, and `output_token_budget` is `2000` for all three (operator-resolved; do not edit).

Surface a brief verification report (in the commit message or as a comment on the Linear issue) noting the verified values. If any field other than `output_token_budget` diverges from `src/alphamind/config/models/agents.py`'s schema, stop and surface — the loader's startup validation already catches structural drift, but a value-level divergence (e.g., a `model:` string the API doesn't recognize) is what a subagent might miss.

## Scope — Part 2: Shared-types module src/alphamind/analysis/\_shared.py

Create the file at `src/alphamind/analysis/_shared.py` (the `_shared` underscore-prefix marks it as analysis-internal; sibling work trees that need these types import from it explicitly).

Module contents:

```python
"""Shared types across the analysis layer.

Re-exports the canonical identifiers the analysis sub-packages
(domain_researchers, qualitative_research, adaptive_research, synthesizer)
import. Centralizing here avoids per-package redefinition of names that
already exist in distillation or elsewhere in the codebase.
"""

from enum import StrEnum

from pydantic import BaseModel, Field

from alphamind.distillation.output import AnomalySeverity, OutputAudience
from alphamind.distillation.sector_assembly import DOMAIN_RESEARCHER_BY_AUDIENCE

__all__ = [
    "AnomalySeverity",
    "Sector",
    "TokensUsed",
    "_SECTOR_AUDIENCE_MAP",
]


class Sector(StrEnum):
    """Analysis-layer sector identifier.

    Values match `DOMAIN_RESEARCHER_BY_AUDIENCE` so a `Sector` round-trips
    through the distillation audience mapping without translation.
    """

    TECH_SEMIS = "tech_semis"
    FINANCIALS = "financials"
    ENERGY = "energy"


class TokensUsed(BaseModel, frozen=True):
    """Token-accounting record returned alongside any LLM-agent invocation.

    Mirrors what the Claude Agent SDK exposes in its response metadata.
    Fields are non-negative integers; the SDK reports zero for absent
    cache reads/writes rather than `None`.
    """

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_read_tokens: int = Field(ge=0)
    cache_write_tokens: int = Field(ge=0)


_SECTOR_AUDIENCE_MAP: dict[Sector, OutputAudience] = {
    Sector.TECH_SEMIS: OutputAudience.SECTOR_TECH_SEMIS,
    Sector.FINANCIALS: OutputAudience.SECTOR_FINANCIALS,
    Sector.ENERGY: OutputAudience.SECTOR_ENERGY,
}

# Compile-time guard: a future drift between Sector and DOMAIN_RESEARCHER_BY_AUDIENCE
# fails import rather than silently producing a wrong sector lookup downstream.
assert {DOMAIN_RESEARCHER_BY_AUDIENCE[aud] for aud in _SECTOR_AUDIENCE_MAP.values()} == {
    s.value for s in Sector
}
```

Stories 06, 08, 10 import `_SECTOR_AUDIENCE_MAP` to resolve `Sector → OutputAudience` without re-encoding the relationship.

## Unit tests

Under `tests/analysis/test_shared.py`:

* `Sector` has exactly three members with values `tech_semis`, `financials`, `energy`.
* `_SECTOR_AUDIENCE_MAP` covers every `Sector` member and resolves each to a distinct `OutputAudience`.
* `TokensUsed` rejects negative values (Pydantic field constraint at construction).
* `AnomalySeverity` re-export round-trips: `from alphamind.analysis._shared import AnomalySeverity` returns the same object as `from alphamind.distillation.output import AnomalySeverity`.

## Out of scope

* The `SectorBrief` / `Finding` / `Anomaly` / `ThesisCandidate` Pydantic models — those are story 03.
* The runner's `_AGENT_NAME_BY_SECTOR` mapping — that lives in story 10's runner module since it's runner-internal.
* Any new `config/sectors.yaml` file — sector membership is read from the database via `alphamind.distillation.sector_assembly.load_sector_roster()`. Do not create a yaml duplicate.
* Edits to `src/alphamind/config/models/agents.py` — the existing schema is the contract.
* Edits to the `output_token_budget` field — operator-resolved at 2000.

## Notes

The module is named `_shared.py` (underscore-prefix) deliberately. Domain researchers, qualitative research, adaptive research, and synthesizer are sibling sub-packages of `alphamind.analysis`; they share these types but the types are not part of any sibling package's external contract. The leading underscore signals "import within `alphamind.analysis`, do not import from outside the analysis layer." If a downstream layer (decision, execution) needs `Sector`, that layer either re-exports it from its own package or imports `Sector` directly from this shared module — but the runtime sector-string contract lives in `alphamind.distillation.sector_assembly` and is the canonical shape regardless.

`TokensUsed` is a Pydantic frozen model rather than a dataclass because (a) the harness story 07 carries it inside `HarnessSuccess` which is also Pydantic, (b) the field-validator surface (non-negative int constraints) costs nothing extra. If a future profiling pass shows the model's per-instance overhead matters, swap to `@dataclass(frozen=True, slots=True)` — the contract is the field set, not the implementation.

The `_SECTOR_AUDIENCE_MAP` lives here rather than in a sibling story because *every* sector-aware story (06, 08, 10) needs it, and the canonical shape is "map `Sector` to its distillation audience." Stories that need the inverse direction (an `OutputAudience` → `Sector` lookup) build it locally; the forward direction is the load-bearing one.

## Acceptance criteria

- [ ] `config/agents.yaml` field-by-field verification report posted as a commit message or comment on this issue, confirming the three domain-researcher entries match the documented field-shape contract (model, prompt path, tools, latency, context, output budgets) with `output_token_budget=2000` retained per operator decision.
- [ ] `src/alphamind/analysis/_shared.py` defines `Sector`, `TokensUsed`, `_SECTOR_AUDIENCE_MAP` and re-exports `AnomalySeverity`.
- [ ] `Sector` values exactly match `DOMAIN_RESEARCHER_BY_AUDIENCE` strings; the assertion at module top fails import on drift.
- [ ] `TokensUsed` rejects negative inputs; tests cover.
- [ ] `_SECTOR_AUDIENCE_MAP` covers every `Sector` member and resolves to distinct `OutputAudience` values.
- [ ] `from alphamind.analysis._shared import AnomalySeverity` returns the same object as the distillation module's literal.
- [ ] No new `config/sectors.yaml` file is created.
- [ ] No edits to `output_token_budget` in `config/agents.yaml`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
