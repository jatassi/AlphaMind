---
status: not_started
completed_date:
commit_id:
---

# 10 — Domain researcher runner (parameterized by sector)

## Goal

Implement `run_domain_researcher` — the single function that turns a sector parameter and an invocation context into a validated `SectorBrief`. Composes the input-bundle assembler (story 08), the harness (story 07), and the validator (story 05) into a clean call surface used by the parallel orchestrator (story 11).

## Reading

- `docs/architecture/llm-integration.md` § Pipeline execution flow — the invocation pattern this runner participates in
- `docs/design/03-analysis-layer/README.md` — sequencing context (domain researchers run in parallel)
- `docs/design/llm-agent-failure-handling.md` — fail-closed semantics (any failure aborts the invocation)
- `docs/design/03-analysis-layer/domain-researchers/tech-semis.md` § Inputs — the three input categories the runner composes

## Depends on

- 03 (`Sector` enum, `SectorBrief`)
- 06 (`load_sector_qualitative_input`, `SectorQualitativeInput`)
- 07 (`invoke_domain_researcher`, `HarnessSuccess`, `HarnessFailure`)
- 08 (`assemble_input_bundle`, `InputBundle`, `RegimeLabel`)

## Scope

In scope: under `src/alphamind/analysis/domain_researchers/` —

- `runner.py` defining:
  - `class DomainResearcherResult(BaseModel, frozen=True)` — the runner's return type:
    - `sector: Sector`
    - `brief: SectorBrief`
    - `input_bundle: InputBundle` (preserved for downstream consumers and diagnostic write-through)
    - `tokens_used: TokensUsed` (passed through from `HarnessSuccess`)
    - `wall_clock_seconds: float`
    - `retry_count: int`
  - `async def run_domain_researcher(sector: Sector, invocation_id: str, as_of: datetime, distillation_output_text: str, regime: RegimeLabel, session: Session, agents_config: AgentRegistry, sectors_config: SectorRegistry) -> DomainResearcherResult` — the main entry point. Composition:
    1. Look up the agent config: `agent_config = agents_config[<sector_to_agent_name>(sector)]`. The mapping is encoded in this module: `TECH_SEMIS → "tech_semis_researcher"`, `FINANCIALS → "financials_researcher"`, `ENERGY → "energy_researcher"`.
    2. Verify `agent_config.sector == sector` (defensive check against config drift).
    3. Load the sector-qualitative input: `qualitative_input = load_sector_qualitative_input(session, sector, as_of, lookback_window_hours=24, max_headlines=30)`. Lookback and max_headlines may become per-agent config later; for now they are the documented defaults.
    4. Build the input bundle: `bundle = assemble_input_bundle(sector, invocation_id, as_of, distillation_output_text, qualitative_input, regime)`.
    5. Build the sector membership map for the validator: `sector_membership = {s: frozenset(sectors_config[s].tickers) for s in Sector}`.
    6. Invoke the harness: `result = await invoke_domain_researcher(agent_config, sector, sector_membership, bundle.bundle_text, invocation_id)`.
    7. Build and return the `DomainResearcherResult`.
- Error propagation: any `HarnessFailure` from the harness propagates up unchanged. The runner does not catch and continue; the caller (orchestrator story 11) handles per fail-closed policy.
- Logging: `logger.info` lines for the four major checkpoints (qualitative input loaded, bundle assembled, harness invoked, brief validated). Each line tags the sector for greppability.
- Unit tests:
  - Happy path: stubs return canned values for each composed step → `run_domain_researcher` returns a `DomainResearcherResult` with the expected sector and a non-empty brief.
  - The input-bundle assembler receives the qualitative-loader output verbatim (composition test, not a content test).
  - The harness receives the assembled bundle text verbatim.
  - The runner verifies `agent_config.sector == sector` and raises `ValueError` on mismatch (defensive — config-drift smoke test).
  - A `HarnessFailure` raised by the harness propagates up unchanged (no swallow).
  - The sector membership map passed to the harness contains exactly the three sector entries from the sectors config.
  - All composed steps are dependency-injected for testing (the runner does not import them by module path; it accepts callables or instances). Acceptable to keep the public API as a thin wrapper that injects the production callables, with an inner private function that takes the dependencies as parameters.

Out of scope:
- The parallel orchestrator that calls this runner three times (story 11).
- The end-to-end verification (story 12).
- Per-invocation-type lookback / max-headlines tuning — wired via the [`run_types/` overlay](../../../design/configuration-management.md#run_typestriggeryaml) when the resolver lands; this story uses the default lookback.

## Notes

The dependency-injection seam keeps unit tests fast and decoupled. The production wrapper passes `load_sector_qualitative_input`, `assemble_input_bundle`, and `invoke_domain_researcher` as defaults; tests pass stubs.

The agent-name mapping (`TECH_SEMIS → "tech_semis_researcher"`) is encoded as a private constant `_AGENT_NAME_BY_SECTOR: Mapping[Sector, str]` in this module. The mapping is the runtime equivalent of the config file's `sector` field on each agent entry — it answers the inverse question (which agent runs for this sector?). Centralizing it here keeps the sector-to-agent-name choice in one place.

`agents_config[<agent_name>]` lookup style: depends on `AgentRegistry`'s shape from story 02. If the registry is a `Mapping[str, AgentConfig]`, square-bracket lookup is fine; if it's a list, the runner does a linear scan. Adapt at integration time.

The runner is `async` because the harness is `async` (the SDK's `query()` is an async generator). The orchestrator (story 11) uses `asyncio.gather` to run three sectors in parallel; the runner's own internals are not parallelized — each step depends on the prior.

The runner emits a structured logger line per checkpoint, not a print or unstructured log. Use the existing `alphamind` module logger (a `getLogger(__name__)` at module top). Per the data-layer's logging convention (`%USERPROFILE%\AlphaMind\logs\collector.log`), the analysis layer can either share that file or write to a new `analysis.log`. Defer the choice to the operator-facing logging-config story (not part of this work tree); for now, use the default logger and let logging-config configure the handler globally.

## Acceptance criteria

- [ ] `run_domain_researcher(sector, invocation_id, as_of, distillation_output_text, regime, session, agents_config, sectors_config)` returns a `DomainResearcherResult`.
- [ ] The composition follows the documented order: qualitative input load → bundle assemble → harness invoke → return.
- [ ] `agent_config.sector != sector` raises `ValueError`.
- [ ] `HarnessFailure` from the harness propagates up unchanged.
- [ ] The sector membership map passed to the harness contains exactly the three sector entries from `sectors_config`.
- [ ] The composed dependencies (qualitative loader, bundle assembler, harness) are dependency-injected; production defaults are wired in via a thin wrapper.
- [ ] Tests cover happy path, config-drift defensive check, harness-failure propagation, and dependency-injection composition.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
