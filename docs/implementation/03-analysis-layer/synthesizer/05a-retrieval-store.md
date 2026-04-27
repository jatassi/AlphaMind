---
status: not_started
completed_date:
commit_id:
---

# 05a — Retrieval store assembly + adapters

## Goal

Implement the per-invocation retrieval store the synthesizer assembles from upstream `BriefBundle`s and the small adapter layer that converts each upstream's native value object (or raw text payload) into a `BriefBundle`. The store exposes a Python-level lookup `lookup(ref_id) → str | None` used by:
- the `retrieve_brief` MCP tool (story 06a) the decision-layer LLMs call at runtime, and
- the consumer-side Layer 3 referential-integrity check the analyst, strategist, and PM validators run.

This is the synthesizer's primary downstream contract — every decision-layer agent depends on this store resolving the references their narratives cite.

## Reading

- `docs/design/03-analysis-layer/synthesizer.md` § Retrieval store side-effect — the contract
- `docs/design/testing/llm-output-validation.md` § Layer 3 — Referential integrity — consumer-side resolution rule that uses this store
- `docs/design/testing/llm-output-validation.md` § Resolution contexts — "Analyst, strategist: the synthesizer's retrieval store for the current invocation — the authoritative source for all `SA-*`, `QR-*`, `AR-*`, `CR-*` IDs available to the decision layer"
- `docs/design/03-analysis-layer/synthesizer.md` § Inputs — the six brief sources the store covers
- `docs/implementation/02-distillation-layer/11b-correlation-regime-brief-assembly.md` — `CorrelationRegimeBrief` upstream value-object (one of the inputs adapted)
- `docs/implementation/03-analysis-layer/domain-researchers/03-sector-brief-data-model.md` — `SectorBrief` upstream value-object (another input adapted)
- `docs/implementation/03-analysis-layer/domain-researchers/07-agent-sdk-harness.md` § Diagnostic preservation — the upstream's `HarnessSuccess.raw_response` field that carries the brief text the synthesizer needs

## Depends on

- 03 (`BriefBundle`, `BriefSource`, `ReferencePrefix`, `parse_reference_id`)
- 04 (`extract_sections`, `ExtractionResult`)

Cross-tree references (no dependency; the adapters operate on documented shapes that may not yet have implementations on `main`):
- `SectorBrief` from `alphamind.analysis.domain_researchers.models` (domain-researchers story 03)
- `CorrelationRegimeBrief` from `alphamind.distillation.correlation_brief` (distillation story 11b)

When either of these types is not yet on `main`, the corresponding adapter is implemented against a structural protocol declared in this story, and the wiring completes when the upstream type lands. Surface this cross-tree dependency at dispatch time per the orchestrator's posture.

## Scope

In scope: under `src/alphamind/analysis/synthesizer/` —

- `retrieval.py` defining:

  - `class RetrievalStore(BaseModel, frozen=True)` — the per-invocation store as a value type:
    - `entries: dict[str, str]` — flat reference-ID → section-text map. Keys are full reference IDs (e.g., `"SA-TECH-3"`, `"CR-7"`, `"QR-CW-1"`); values are the corresponding section text from the originating brief.
    - `freshness_by_source: dict[BriefSource, datetime]` — per-source freshness timestamp; the LLM's bundle assembler (story 07) uses this to render "as-of" lines per upstream.
    - `unknown_markers_by_source: dict[BriefSource, tuple[str, ...]]` — diagnostic-only field carrying every marker the per-bundle extractor flagged as unknown / foreign-prefix / duplicate. Surfaces in the diagnostic record (story 08); does NOT surface to the synthesizer's LLM and does NOT cause assembly to fail.

  - `def lookup(self, ref_id: str) -> str | None` — returns `entries[ref_id]` if present, else `None`. The caller treats `None` as a referential failure (Layer 3 violation at the consumer) — it is not the store's job to escalate.

  - `def assemble_retrieval_store(bundles: Iterable[BriefBundle]) -> RetrievalStore` — pure function:
    1. For each `bundle` in `bundles`, call `extract_sections(bundle)` (story 04) and merge `result.sections` into the running `entries` map.
    2. Cross-source key collision is a structural error (e.g., two bundles both producing a `"CR-1"` section). Per the design's per-source prefix segregation, collisions are impossible unless an upstream produced an out-of-spec brief. Raise `RetrievalAssemblyError` with the offending key, both source bundles, and the colliding section bodies — this surfaces an upstream contract break early. Do NOT silently overwrite.
    3. Populate `freshness_by_source` with each bundle's `freshness` (one entry per `BriefSource` in the input set).
    4. Populate `unknown_markers_by_source` from each bundle's `ExtractionResult.unknown_markers`.
    5. Return the populated `RetrievalStore`.

  - `class RetrievalAssemblyError(Exception)` — raised on cross-source key collision; carries the colliding key, the two source bundles, and both section bodies for diagnostics.

- `adapters.py` defining the upstream-shape → `BriefBundle` adapters:

  - `def sector_brief_to_bundle(brief: SectorBrief, raw_text: str, freshness: datetime) -> BriefBundle` — wraps a domain-researcher's output. Accepts the raw response text from the harness's `HarnessSuccess.raw_response` (story 07 of the domain-researchers tree) plus the parsed `SectorBrief` (used only to determine `BriefSource` from `brief.sector`):
    - `Sector.TECH_SEMIS → BriefSource.SA_TECH`
    - `Sector.FINANCIALS → BriefSource.SA_FIN`
    - `Sector.ENERGY → BriefSource.SA_ENERGY`
    - `text = raw_text`, `freshness = freshness`. Default `prefixes` (set by `BriefBundle`'s validator) covers the section variants for that sector.

  - `def correlation_regime_brief_to_bundle(brief: CorrelationRegimeBrief) -> BriefBundle` — wraps the distillation work tree's CR output. `text = brief.text`, `freshness = brief.freshness_min`, `source = BriefSource.CR`. The CR brief's own `reference_index` (story 11b) is not used here — the extractor (story 04) re-derives the per-`CR-N` segmentation from the brief text by scanning markers, producing the same map the `reference_index` would have indirectly. The two should agree on every key per the upstream's contract; story 11b's invariants ensure this.

  - `def raw_brief_to_bundle(source: BriefSource, text: str, freshness: datetime) -> BriefBundle` — generic constructor for upstreams that do not yet have an implementation work tree (qualitative, adaptive). Expects `source` to be one of `BriefSource.QR | BriefSource.AR`; rejects `source in {SA_TECH, SA_FIN, SA_ENERGY, CR}` (those have dedicated adapters above) with a clear error pointing the caller at the right adapter.

  - Structural protocols `SectorBriefProtocol` and `CorrelationRegimeBriefProtocol` declared in `adapters.py` (Python `typing.Protocol`) so this story's tests can exercise the adapters without depending on the cross-tree value-object implementations being on `main`. Tests construct stub objects matching the protocols; the production wiring uses the real types.

- Unit tests:
  - `assemble_retrieval_store([])` returns an empty store with empty `entries`, `freshness_by_source`, `unknown_markers_by_source`.
  - Single-bundle assembly: a fixture sector bundle decomposes into N sections; the store's `entries` has those N keys and `lookup` returns each section's text.
  - Multi-bundle assembly: three sector bundles + one CR bundle + one QR bundle (constructed as `raw_brief_to_bundle`) + one AR bundle merge into a single store with the union of keys; `freshness_by_source` carries six entries; `lookup` returns the right text for each key across all sources.
  - Cross-source key collision raises `RetrievalAssemblyError` with both source bundles and the colliding key in the message. (Construct the test by manually building two bundles whose extractors produce overlapping keys — synthetic, since per-source prefix segregation makes this physically impossible in production.)
  - `lookup("XX-1")` returns `None` (unknown reference) rather than raising.
  - `lookup("SA-TECH-99")` returns `None` when the brief contains no such section; does not silently match a different section.
  - `unknown_markers_by_source` carries through from the per-bundle extraction; a bundle whose extraction surfaced two unknown markers shows both in the store.
  - `sector_brief_to_bundle` correctly maps `Sector.TECH_SEMIS → BriefSource.SA_TECH` (and the other two sectors).
  - `correlation_regime_brief_to_bundle` produces a `BriefSource.CR` bundle whose `text` equals the input brief's `text` and whose `freshness` equals `freshness_min`.
  - `raw_brief_to_bundle(BriefSource.SA_TECH, ...)` rejects with a clear error pointing at `sector_brief_to_bundle`.
  - `raw_brief_to_bundle(BriefSource.QR, ...)` and `raw_brief_to_bundle(BriefSource.AR, ...)` succeed.
  - Assembly is deterministic — identical input bundles in the same order produce a `RetrievalStore` whose `entries` iteration order is identical across repeated calls.

Out of scope:
- The MCP-tool wrapper that exposes `retrieve_brief` to the SDK runtime (story 06a — wraps `RetrievalStore.lookup`).
- The synthesizer's bundle assembler (story 07 — composes the full LLM user message; also reads `freshness_by_source`).
- The harness that runs the synthesizer LLM call (story 08).
- Persistence of the retrieval store beyond the invocation (per `mid-pipeline-failure-handling.md`'s no-resume policy, the store lives in memory for the invocation and is discarded on completion or abort).

## Notes

The `RetrievalStore` is a Pydantic model rather than a `dict` subclass because (a) we want the `lookup` semantics to be a documented method, not a `__getitem__` that raises, and (b) the `freshness_by_source` and `unknown_markers_by_source` companion fields belong on the same value. A plain dict would force callers to carry the metadata separately or pack tuples — both rougher seams.

Cross-source key collision raising rather than overwriting: per [`feedback_no_decision_trails.md`](../../../../.claude/projects/-Users-jatassi-Git-AlphaMind/memory/feedback_no_decision_trails.md), state contracts positively. The design's per-source prefix segregation (`SOURCE_PREFIXES` in story 03) means cross-source collisions are physically impossible for well-formed upstreams — `SA-TECH-N` only appears in the tech-semis bundle, `CR-N` only in the CR bundle, etc. A collision is therefore a contract break worth surfacing loudly. Silent overwrite would mask the break and produce subtle downstream failures (decision agents would resolve a reference to the wrong source's content).

The CR adapter ignores `CorrelationRegimeBrief.reference_index` and re-derives via `extract_sections`. Two reasons: (1) the `reference_index` from story 11b maps `CR-N → block_id` (the source distillation block's ID), not `CR-N → section text`; the synthesizer needs section text. (2) Re-deriving via the same extractor used for sector / qualitative / adaptive bundles keeps the path uniform — one extractor, one assembly step, one set of tests, no special-case for CR.

The `raw_brief_to_bundle` adapter's restriction (rejects sector / CR sources) keeps the API hard-to-misuse: callers cannot accidentally bypass the typed adapters by routing a raw text through the generic path. When the qualitative-research and adaptive-research implementation work trees land, they may want to introduce typed `qualitative_brief_to_bundle` and `adaptive_brief_to_bundle` adapters analogous to the sector / CR ones, at which point `raw_brief_to_bundle` either becomes test-only or is retired entirely. The current shape leaves room for either evolution without forcing a breaking change here.

Per `feedback_simplify_before_building.md`: this story does NOT introduce a `BriefIngestionPipeline` orchestrator class to wrap `assemble_retrieval_store`. The function is the orchestration. The runner (story 10) calls it directly. Adding an indirection layer would just hide the call.

The `unknown_markers_by_source` field surfaces in the harness diagnostic record (story 08) under `metadata.json`. Per the design's "stop-reason check only" stance for the synthesizer, unknown markers from upstream are NOT a synthesizer-level failure — but they are useful operator-visible signal that an upstream produced malformed output. The Phase 4 feedback loop (`docs/design/feedback-loop.md`) tracks the rate as a cross-cutting metric.

## Acceptance criteria

- [ ] `RetrievalStore` defined as a frozen Pydantic model with `entries`, `freshness_by_source`, `unknown_markers_by_source` fields.
- [ ] `RetrievalStore.lookup(ref_id)` returns the section text for known IDs, `None` for unknown.
- [ ] `assemble_retrieval_store([])` returns an empty store with empty companion fields.
- [ ] `assemble_retrieval_store` over a multi-bundle fixture (3 sector + 1 CR + 1 QR + 1 AR) produces the union of per-bundle sections.
- [ ] Cross-source key collision raises `RetrievalAssemblyError` carrying the colliding key and both source bundles.
- [ ] `unknown_markers_by_source` carries through from per-bundle extraction without alteration.
- [ ] `sector_brief_to_bundle` maps each `Sector` enum member to the correct `BriefSource`.
- [ ] `correlation_regime_brief_to_bundle` produces a `BriefSource.CR` bundle with `text` and `freshness` from the input.
- [ ] `raw_brief_to_bundle` rejects sector / CR sources with a clear error; accepts QR / AR sources.
- [ ] Assembly is deterministic across repeated calls on identical input.
- [ ] `SectorBriefProtocol` and `CorrelationRegimeBriefProtocol` declared as `typing.Protocol`s so adapter tests do not require the cross-tree value-object implementations.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
