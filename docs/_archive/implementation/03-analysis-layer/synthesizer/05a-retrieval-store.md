# 05a — Retrieval store assembly + adapters

## Goal

Implement the per-invocation retrieval store the synthesizer assembles from upstream `BriefBundle`s and the small adapter layer that converts each upstream's native value object into a `BriefBundle`. The store exposes a Python-level lookup `lookup(ref_id) -> str | None` used by the `retrieve_brief` MCP tool (story 06a) the decision-layer LLMs call at runtime, and by the consumer-side Layer 3 referential-integrity check the analyst, strategist, and PM validators run.

## Reading

* `docs/design/03-analysis-layer/synthesizer.md` § Retrieval store side-effect — the contract.
* `docs/design/testing/llm-output-validation.md` § Layer 3 — Referential integrity — consumer-side resolution rule.
* `docs/design/03-analysis-layer/synthesizer.md` § Inputs — the six brief sources.
* [ALP-201](https://linear.app/alphamind-jatassi/issue/ALP-201) (story 03) — `BriefBundle`, `BriefSource`, `ReferencePrefix`, `parse_reference_id`.
* [ALP-202](https://linear.app/alphamind-jatassi/issue/ALP-202) (story 04) — `extract_references`, `ExtractionError`.
* `src/alphamind/analysis/domain_researchers/models.py` — `SectorBrief`, `SECTOR_PREFIX`. The adapter renders this to text per the synthesizer's expected layout.
* `src/alphamind/distillation/correlation_brief.py` — `CorrelationRegimeBrief` (already has pre-rendered `text`, `reference_index`, `freshness_min`; the adapter is near-trivial).
* `src/alphamind/analysis/qualitative_research/models.py` — `QualitativeBrief` (the adapter renders to text).
* `src/alphamind/analysis/adaptive_research/models.py` — `AdaptiveBrief` (the adapter renders to text).

## Depends on

* [ALP-201](https://linear.app/alphamind-jatassi/issue/ALP-201) (story 03) — `BriefBundle` and prefix taxonomy.
* [ALP-202](https://linear.app/alphamind-jatassi/issue/ALP-202) (story 04) — `extract_references`.

## Scope

Two new modules under `src/alphamind/analysis/synthesizer/`. The first defines the store; the second defines the four adapters that convert each upstream native value object into a `BriefBundle`. Rendering is owned by the adapters (not the upstream models) so the upstream models remain pure data and the synthesizer's expected text layout is co-located with the synthesizer's other code.

#### retrieval module

Module path is `src/alphamind/analysis/synthesizer/retrieval.py`. The module defines four symbols.

* `class RetrievalStore(BaseModel, frozen=True)` with two fields. `entries: dict[str, str]` is keyed by reference ID and holds the section text. `freshness_by_source: dict[BriefSource, datetime]` carries per-source freshness for diagnostics.
* `def lookup(self, ref_id: str) -> str | None` returns the section text for a known ref ID and `None` for unknown.
* `def assemble_retrieval_store(bundles: Iterable[BriefBundle]) -> RetrievalStore` folds all `extract_references` outputs into a single entries map. Raises `RetrievalAssemblyError` on cross-source key collision (carries the colliding key plus both source bundles).
* `class RetrievalAssemblyError(Exception)` is the assembly-time failure type.

#### adapters module

Module path is `src/alphamind/analysis/synthesizer/adapters.py`. The module defines four adapter functions.

* `sector_brief_to_bundle(brief: SectorBrief, freshness: datetime) -> BriefBundle` — renders `SectorBrief.findings` plus `anomalies` plus `thesis_candidates` to the canonical text layout (one `[<prefix>-<index>]` header per item, prefix derived from `SECTOR_PREFIX[brief.sector]`). Returns a `BriefBundle` whose `source` matches `brief.sector` (e.g. `Sector.TECH_SEMIS` maps to `BriefSource.SA_TECH`).
* `correlation_regime_brief_to_bundle(brief: CorrelationRegimeBrief) -> BriefBundle` — wraps the brief's existing `text` field; uses `freshness_min` for the bundle's freshness; sets `source=BriefSource.CR`.
* `qualitative_brief_to_bundle(brief: QualitativeBrief, freshness: datetime) -> BriefBundle` — renders the qualitative brief's narrative threads (QR-N) and catalyst watch (QR-CW-N) to the canonical text layout.
* `adaptive_brief_to_bundle(brief: AdaptiveBrief, freshness: datetime) -> BriefBundle` — renders adaptive findings (AR-N) to the canonical text layout.

#### Tests for retrieval

Test module path is `tests/analysis/synthesizer/test_retrieval.py`. Test cases listed below.

* `test_assemble_empty_returns_empty_store`.
* `test_assemble_single_bundle_indexes_all_refs`.
* `test_assemble_multi_bundle_unions_entries`.
* `test_assemble_collision_raises` — two bundles emit the same ref ID, raising `RetrievalAssemblyError` carrying both sources.
* `test_lookup_returns_none_on_unknown`.
* `test_assembly_is_deterministic` — repeated calls on identical input produce identical entries dict.
* `test_freshness_by_source_populated`.

#### Tests for adapters

Test module path is `tests/analysis/synthesizer/test_adapters.py`. Test cases listed below.

* `test_sector_brief_to_bundle_uses_sector_prefix` — fixture `SectorBrief(sector=TECH_SEMIS, ...)` produces a bundle whose text contains `[SA-TECH-N]` headers.
* `test_sector_brief_to_bundle_round_trips` — rendered text passes back through `extract_references` and produces the expected ref-ID set.
* `test_correlation_brief_to_bundle_passthrough` — bundle's `text` equals `brief.text`; freshness equals `brief.freshness_min`.
* `test_qualitative_brief_to_bundle_includes_qr_cw` — fixture with both QR and QR-CW items renders both prefix families.
* `test_adaptive_brief_to_bundle_renders_findings`.

## Out of scope

* The MCP-tool wrapper that exposes `retrieve_brief` to the SDK runtime (story 06a).
* The synthesizer's input-bundle assembler (story 07).
* The harness that runs the synthesizer LLM call (story 08).
* Persistence beyond the invocation.
* Introducing `*Protocol` abstractions around the upstream concrete types — import the concrete `SectorBrief`, `CorrelationRegimeBrief`, `QualitativeBrief`, `AdaptiveBrief` directly.

## Acceptance criteria

- [ ] `RetrievalStore` is a frozen Pydantic model with `entries` plus `freshness_by_source`.
- [ ] `RetrievalStore.lookup(ref_id)` returns the section text for known IDs, `None` for unknown.
- [ ] `assemble_retrieval_store([])` returns an empty store.
- [ ] Cross-source key collision raises `RetrievalAssemblyError` carrying both source bundles.
- [ ] Each adapter rounds-trips through `extract_references` (rendered text re-extracts to the expected ref-ID set).
- [ ] Each adapter produces a bundle whose `source` corresponds to the upstream's identity.
- [ ] Adapters import the upstream concrete types directly. No parallel `*Protocol` is introduced.
- [ ] Assembly is deterministic across repeated calls on identical input.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.