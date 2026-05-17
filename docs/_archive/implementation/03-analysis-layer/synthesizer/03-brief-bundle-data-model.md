# 03 — `BriefBundle` data model and reference-prefix taxonomy

## Goal

Implement the typed in-memory representation of a single upstream brief as the synthesizer consumes it: a source identifier, the brief's full text body, the freshness timestamp, and the set of reference-ID prefixes the brief is known to produce. This is the uniform shape into which every upstream — sector briefs, the correlation/regime brief, qualitative brief, adaptive research findings — is normalized before retrieval-store assembly (story 05a) and before composition into the synthesizer's user message (story 07).

A small reference-prefix enumeration is consolidated here as the canonical taxonomy the reference-marker extractor (story 04) and the retrieval store (story 05a) reuse.

## Reading

* `docs/design/03-analysis-layer/synthesizer.md` § Inputs — the six brief sources and their reference prefixes.
* `docs/design/03-analysis-layer/synthesizer.md` § Source reference mechanism — `[<prefix>-<index>]` format.
* `docs/design/03-analysis-layer/synthesizer.md` § Retrieval store side-effect — downstream consumption pattern that motivates the typed structure.
* `docs/design/testing/llm-output-validation.md` § Reference-ID taxonomy — producer-side format rules consolidated by upstream agent.
* `src/alphamind/analysis/_shared.py` — canonical `Sector`, `SignalQuality`, `AnomalySeverity`, `TokensUsed`. **Reuse** `Sector` for the sector members of `BriefSource` (do not introduce a parallel `sa_tech` / `sa_fin` / `sa_energy` vocabulary).
* `src/alphamind/analysis/domain_researchers/models.py` — `SectorBrief` shape; `SECTOR_PREFIX` mapping `Sector` to the sector-prefix string. Canonical sector-prefix-string source.
* `src/alphamind/distillation/correlation_brief.py` — `CorrelationRegimeBrief` shape; has pre-rendered `text`, `reference_index`, `freshness_min`. `CR-N` prefix.
* `src/alphamind/analysis/qualitative_research/models.py` — `QualitativeBrief` shape (informs how `QR-N` and `QR-CW-N` prefixes appear in upstream output).
* `src/alphamind/analysis/adaptive_research/models.py` — `AdaptiveBrief` shape; `AR-N` prefix.
* Parent issue § Notes for the orchestrator § Reference-prefix taxonomy and § Sector reuse.

## Depends on

Nothing — this is the foundation type for the rest of the work tree.

## Scope

Module path is `src/alphamind/analysis/synthesizer/models.py`. The module defines a `BriefSource` enum, a `ReferencePrefix` enum, a `SOURCE_PREFIXES` mapping, the `BriefBundle` Pydantic model, and the `parse_reference_id` helper.

#### BriefSource enum members

* `SA_TECH` with value `Sector.TECH_SEMIS.value` (the string `"tech_semis"`) — round-trips through `Sector`.
* `SA_FIN` with value `Sector.FINANCIALS.value` (the string `"financials"`).
* `SA_ENERGY` with value `Sector.ENERGY.value` (the string `"energy"`).
* `QR` with value `"qr"`.
* `AR` with value `"ar"`.
* `CR` with value `"cr"`.

#### ReferencePrefix enum members

* Tech sector trio — `SA_TECH = "SA-TECH"`, `SA_TECH_ANOM = "SA-TECH-ANOM"`, `SA_TECH_TC = "SA-TECH-TC"`.
* Financials sector trio — `SA_FIN = "SA-FIN"`, `SA_FIN_ANOM = "SA-FIN-ANOM"`, `SA_FIN_TC = "SA-FIN-TC"`.
* Energy sector trio — `SA_ENERGY = "SA-ENERGY"`, `SA_ENERGY_ANOM = "SA-ENERGY-ANOM"`, `SA_ENERGY_TC = "SA-ENERGY-TC"`.
* Qualitative pair — `QR = "QR"`, `QR_CW = "QR-CW"`.
* Adaptive — `AR = "AR"`.
* Correlation/regime — `CR = "CR"`.

#### SOURCE_PREFIXES mapping

A `dict[BriefSource, frozenset[ReferencePrefix]]`. Each `BriefSource` maps to the set of `ReferencePrefix` families that source produces. Sector sources cover their three variants (base, ANOM, TC). `QR` covers QR plus QR_CW. `AR` and `CR` each cover their single base prefix.

#### BriefBundle model

Frozen Pydantic model with four fields.

* `source: BriefSource`.
* `text: str` — the full brief body.
* `freshness: datetime` — must be timezone-aware (validator rejects naive).
* `prefixes: frozenset[ReferencePrefix]` — defaults to `SOURCE_PREFIXES[source]`; validator rejects values outside `SOURCE_PREFIXES[source]`.

#### parse_reference_id helper

Signature `parse_reference_id(ref_id: str) -> tuple[ReferencePrefix, int] | None`. Longest-match parsing. `SA-TECH-ANOM-3` resolves to `(SA_TECH_ANOM, 3)`, not `(SA_TECH, ANOM-3)`. `QR-CW-2` resolves to `(QR_CW, 2)`. Returns `None` for unknown prefix, missing index, non-integer index, zero index, or empty input.

#### Tests

Test module path is `tests/analysis/synthesizer/test_models.py`. Test cases listed below.

* `test_brief_source_values_match_sector` — three sector members round-trip through `Sector`.
* `test_reference_prefix_enum_complete` — exact member set.
* `test_source_prefixes_cover_every_source` — every `BriefSource` keys into `SOURCE_PREFIXES`.
* `test_briefbundle_default_prefixes` — omitting `prefixes` defaults to `SOURCE_PREFIXES[source]`.
* `test_briefbundle_rejects_foreign_prefixes` — `prefixes` containing a value outside `SOURCE_PREFIXES[source]` fails validation.
* `test_briefbundle_rejects_naive_freshness` — naive datetime fails.
* `test_briefbundle_hashable` — frozen plus hashable.
* `test_parse_reference_id_subtype_longest_match` — `SA-TECH-ANOM-3` parses to `(SA_TECH_ANOM, 3)`.
* `test_parse_reference_id_qr_cw` — `QR-CW-1` parses to `(QR_CW, 1)`.
* `test_parse_reference_id_returns_none_on_unknown` — unknown prefix, missing index, non-integer index, zero index, empty input all return `None`.

## Out of scope

* The text-scanning extractor (story 04).
* The retrieval store (story 05a).
* Adapters from upstream value objects to `BriefBundle` (story 05a).
* The bundle assembler (story 07).

## Acceptance criteria

- [ ] `src/alphamind/analysis/synthesizer/models.py` defines `BriefSource`, `ReferencePrefix`, `BriefBundle`, `SOURCE_PREFIXES`, `parse_reference_id`.
- [ ] Sector members of `BriefSource` use `Sector` enum values verbatim (round-trip property asserted in test).
- [ ] `parse_reference_id` correctly resolves all sub-typed prefix families with longest-match semantics.
- [ ] `BriefBundle` rejects naive `freshness`.
- [ ] `BriefBundle` rejects `prefixes` outside `SOURCE_PREFIXES[source]`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.