---
status: not_started
completed_date:
commit_id:
---

# 10 — Synthesizer runner

## Goal

Implement the public entry point that composes the synthesizer's full work: take the six upstream brief inputs (already produced by domain researchers, qualitative researcher, adaptive researcher, and the distillation correlation/regime brief assembler), build the retrieval store, build the synthesizer's input bundle, invoke the harness, and return the synthesis text alongside the retrieval store the decision-layer agents will consume. This is the sole external entry point — the pipeline orchestrator (a future story in a higher-level work tree) calls only this function.

## Reading

- `docs/architecture/llm-integration.md` § Orchestration pattern — how the analysis-layer orchestration calls into the synthesizer
- `docs/design/03-analysis-layer/synthesizer.md` § Inputs — the six upstream sources the runner consumes
- `docs/design/03-analysis-layer/synthesizer.md` § Output — what the runner returns
- `docs/design/03-analysis-layer/synthesizer.md` § Retrieval store side-effect — the side-effect-as-return-value the runner exposes
- `docs/design/llm-agent-failure-handling.md` § Failure response — every agent is Critical; failures abort the invocation
- `docs/design/mid-pipeline-failure-handling.md` — fresh-context invocation; no resume
- `docs/implementation/03-analysis-layer/synthesizer/05a-retrieval-store.md` — `assemble_retrieval_store`, adapters
- `docs/implementation/03-analysis-layer/synthesizer/06a-retrieve-brief-mcp-tool.md` — `make_retrieve_brief_tool` factory the runner exposes for downstream consumers
- `docs/implementation/03-analysis-layer/synthesizer/07-input-bundle-assembler.md` — `assemble_synthesizer_input`
- `docs/implementation/03-analysis-layer/synthesizer/08-agent-sdk-harness.md` — `invoke_synthesizer`, `HarnessSuccess`, failure taxonomy

## Depends on

- 02 (`AgentConfig`)
- 05a (`assemble_retrieval_store`, `sector_brief_to_bundle`, `correlation_regime_brief_to_bundle`, `raw_brief_to_bundle`)
- 06a (`make_retrieve_brief_tool` — exposed in the runner's return value for downstream wiring)
- 06b (`make_portfolio_state_tools` — wired into the harness via the runner)
- 07 (`assemble_synthesizer_input`)
- 08 (`invoke_synthesizer`, `HarnessSuccess`)
- 09 (the system prompt file — exists at the configured path; runner does not load it directly, but its absence would surface as an `SDKFailure` from the harness)

## Scope

In scope: under `src/alphamind/analysis/synthesizer/` —

- `runner.py` defining:

  - `class SynthesisOutput(BaseModel, frozen=True)` — the runner's return value:
    - `invocation_id: str`
    - `synthesis_text: str` — the synthesizer's prose output (from `HarnessSuccess.response_text`).
    - `retrieval_store: RetrievalStore` — populated from the upstream briefs; passed downstream so the analyst, strategist, and PM can resolve their reference citations.
    - `tokens_used: TokensUsed` — surfaces from `HarnessSuccess.tokens_used` for the cost-tracking telemetry.
    - `wall_clock_seconds: float`
    - `tool_call_count: int`

  - `class UpstreamBriefs(BaseModel, frozen=True)` — typed bundle of all upstream inputs the runner takes (one named field per brief source, so the call site is self-documenting and fail-closed if any brief is missing):
    - `tech_semis: SectorBriefInput` — `(brief: SectorBriefProtocol, raw_text: str, freshness: datetime)`
    - `financials: SectorBriefInput`
    - `energy: SectorBriefInput`
    - `qualitative: RawBriefInput` — `(text: str, freshness: datetime)` (qualitative researcher work tree not yet implemented; runner takes raw text + freshness via `raw_brief_to_bundle`)
    - `adaptive: RawBriefInput`
    - `correlation_regime: CorrelationRegimeBriefProtocol` — already carries `text` and `freshness_min`
    - `regime: RegimeLabel` — universal context broadcast (also embedded in the CR brief but consumed structurally here for the input-bundle assembler)

    `SectorBriefInput` and `RawBriefInput` are small Pydantic value types defined in this story (each three / two fields) so the typing is explicit at the call site.

  - `async def run_synthesizer(invocation_id: str, as_of: datetime, agent_config: AgentConfig, upstream: UpstreamBriefs, portfolio_reader: PortfolioStateReader) -> SynthesisOutput` — the entry point. Composes:

    1. Build the six `BriefBundle`s from `upstream` via the adapters in story 05a:
       - `sector_brief_to_bundle(upstream.tech_semis.brief, upstream.tech_semis.raw_text, upstream.tech_semis.freshness)`
       - same for `financials` and `energy`
       - `correlation_regime_brief_to_bundle(upstream.correlation_regime)`
       - `raw_brief_to_bundle(BriefSource.QR, upstream.qualitative.text, upstream.qualitative.freshness)`
       - `raw_brief_to_bundle(BriefSource.AR, upstream.adaptive.text, upstream.adaptive.freshness)`

    2. Order the bundles per the convention documented in story 07's Notes: `CR → SA-ENERGY → SA-FIN → SA-TECH → QR → AR`.

    3. `retrieval_store = assemble_retrieval_store(bundles)`. May raise `RetrievalAssemblyError` on cross-source key collision — the runner does NOT catch this; it propagates as a runner-level exception that aborts the invocation per fail-closed semantics.

    4. `input_bundle = assemble_synthesizer_input(invocation_id, as_of, upstream.regime, bundles)`.

    5. `harness_result = await invoke_synthesizer(agent_config, input_bundle, portfolio_reader)`. May raise any `HarnessFailure` subclass — runner does NOT catch; propagates.

    6. Append `unknown_markers_by_source` (from `retrieval_store.unknown_markers_by_source`) to the harness's diagnostic record's `metadata.json`. The harness owns most of the metadata; the runner owns this one field because only the runner has access to the assembled retrieval store. Implementation: open `metadata.json`, parse, mutate, re-write; the harness's diagnostic-write happens before the runner's append.

    7. Return `SynthesisOutput(invocation_id=invocation_id, synthesis_text=harness_result.response_text, retrieval_store=retrieval_store, tokens_used=harness_result.tokens_used, wall_clock_seconds=harness_result.wall_clock_seconds, tool_call_count=harness_result.tool_call_count)`.

  - `def make_retrieve_brief_tool_for_invocation(synthesis_output: SynthesisOutput)` — a thin convenience wrapper around `make_retrieve_brief_tool(synthesis_output.retrieval_store)` from story 06a. The pipeline orchestrator calls this when wiring the analyst / strategist / PM agents' `ClaudeAgentOptions`. Documented in the runner module so the integration point is discoverable.

- Unit tests under `tests/analysis/synthesizer/`:
  - **End-to-end happy path**: stub upstream briefs (six fixtures spanning all six sources), stub `PortfolioStateReader`, stub harness response → `run_synthesizer` returns `SynthesisOutput` with the expected `synthesis_text`, retrieval_store keys, and metadata fields. The harness is stubbed via dependency injection (mirror story 08's pattern); no real SDK call.
  - **Bundle ordering**: assert that the runner orders bundles per the documented convention (`CR → SA-ENERGY → SA-FIN → SA-TECH → QR → AR`) — verifiable by inspecting `input_bundle.bundles` order.
  - **Harness failure propagates**: harness raises `EmptyResponseFailure` → runner re-raises (does not catch).
  - **`RetrievalAssemblyError` propagates**: assemblage fails on synthetic colliding fixture → runner re-raises.
  - **Diagnostic metadata append**: `metadata.json` written by the harness gets `unknown_markers_by_source` appended after the runner returns; the merged file contains both the harness fields and the runner-added field.
  - **`make_retrieve_brief_tool_for_invocation`**: returns a callable equivalent to `make_retrieve_brief_tool(synthesis_output.retrieval_store)` — verified by calling both with the same `ref_id` and asserting equal payloads.
  - **Missing brief**: constructing `UpstreamBriefs` with any of the seven required fields absent fails Pydantic validation (regression check that the runner fails-closed before the SDK call when the orchestrator forgot a brief).

Out of scope:
- The pipeline orchestrator that calls `run_synthesizer` — a future story in a higher-level work tree (the analysis-layer pipeline orchestrator does not yet exist beyond the architecture sketch in `llm-integration.md § Orchestration pattern`).
- Wiring the `retrieve_brief` tool into the analyst / strategist / PM agents' SDK options — those are the decision-layer work trees' responsibility; this story exposes `make_retrieve_brief_tool_for_invocation` so they can wire it.
- Live SDK verification (story 11).
- Resume / checkpoint logic — per `mid-pipeline-failure-handling.md`, none.
- Adapter for the *qualitative researcher* and *adaptive researcher* value-object outputs — those work trees do not exist on `main` yet; the `raw_brief_to_bundle` adapter (story 05a) is the bridge until typed adapters land.

## Notes

The `UpstreamBriefs` typed bundle is the runner's principal API. By making each brief a named field, the call site documents the contract — the orchestrator cannot accidentally pass briefs in the wrong order or omit one. Pydantic's required-field enforcement is the runtime guard; mypy is the static guard.

`raw_brief_to_bundle` for the qualitative and adaptive briefs is a deliberate bridge. When those work trees land:
- They will likely produce typed value objects (analogous to `SectorBrief`).
- A typed adapter (analogous to `sector_brief_to_bundle`) will live in story 05a's `adapters.py`.
- The `RawBriefInput` type and the `raw_brief_to_bundle` call sites in the runner can be replaced by typed equivalents — a small, mechanical refactor.
- Until then, the runner accepts text + freshness for those two sources.

The runner is *thin*. Its job is composition, not policy. Per [`feedback_simplify_before_building.md`](../../../../.claude/projects/-Users-jatassi-Git-AlphaMind/memory/feedback_simplify_before_building.md), no orchestrator class hierarchy, no plugin registry, no event-bus pattern. A function that calls six other functions in order is the right shape.

The `unknown_markers_by_source` append-to-`metadata.json` is the one slightly awkward bit — the harness writes most of the metadata before the runner has access to the retrieval store's diagnostic field. The alternative (passing `unknown_markers_by_source` into the harness) would couple the harness to the retrieval store, which the harness's design explicitly avoids (the harness's job is the SDK call, not retrieval). The append-after pattern is the cleanest seam: the harness writes what it owns; the runner amends with what only it knows. Document the contract in both stories' Notes; the integration test (story 11) verifies the merged file shape.

The runner orders bundles per story 07's convention. This is a runner-level decision — the assembler preserves whatever order it receives. Centralizing the order here lets the orchestrator stay agnostic and lets future re-ordering experiments (e.g., "what if we put QR first on regime-transition days?") happen in one place.

Per [`feedback_llm_agents_uniformly_critical.md`](../../../../.claude/projects/-Users-jatassi-Git-AlphaMind/memory/feedback_llm_agents_uniformly_critical.md), every failure propagates. The runner does NOT catch any harness or assembly failure — the pipeline aborts the invocation. This is the designed behavior per `mid-pipeline-failure-handling.md`.

The `as_of` parameter is the runner's view of "now" at synthesizer-stage start. It is NOT derived from any individual brief's `freshness` (those are upstream-stage-start timestamps). The orchestrator passes `as_of = datetime.now(UTC)` at the moment it kicks off the synthesizer.

Per CLAUDE.md, alert before disabling lint rules. The runner is plain composition; there should be no lint suppression candidates here.

## Acceptance criteria

- [ ] `SynthesisOutput` defined as a frozen Pydantic model with `invocation_id`, `synthesis_text`, `retrieval_store`, `tokens_used`, `wall_clock_seconds`, `tool_call_count`.
- [ ] `UpstreamBriefs` defined as a frozen Pydantic model with the seven required fields (three sector inputs, two raw-brief inputs, one CR brief, one regime label); missing field rejects at construction.
- [ ] `run_synthesizer(...)` happy path returns `SynthesisOutput` with the expected `synthesis_text`, retrieval-store keys, and metadata.
- [ ] Bundles passed to `assemble_synthesizer_input` are ordered `CR → SA-ENERGY → SA-FIN → SA-TECH → QR → AR`.
- [ ] `RetrievalAssemblyError` from cross-source key collision propagates from the runner.
- [ ] `HarnessFailure` subclasses propagate from the runner without being caught.
- [ ] `metadata.json` produced by the harness is amended with `unknown_markers_by_source` after the runner returns.
- [ ] `make_retrieve_brief_tool_for_invocation(synthesis_output)` returns a callable equivalent to `make_retrieve_brief_tool(synthesis_output.retrieval_store)`.
- [ ] The harness is stubbed via dependency injection in tests; no real SDK call.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
