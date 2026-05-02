---
status: not_started
completed_date:
commit_id:
---

# 03 — `BriefBundle` data model and reference-prefix taxonomy

## Goal

Implement the typed in-memory representation of a single upstream brief as the synthesizer consumes it: a name, the brief's full text body, the freshness timestamp, and the set of reference-ID prefixes the brief is known to produce. This is the uniform shape into which every upstream — sector briefs, the correlation/regime brief, qualitative brief, adaptive research findings — is normalized before retrieval-store assembly (story 05a) and before composition into the synthesizer's user message (story 07).

A small reference-prefix enumeration (`SA-TECH`, `SA-FIN`, `SA-ENERGY`, `QR`, `AR`, `CR` plus the per-section variants `SA-{SECTOR}-ANOM`, `SA-{SECTOR}-TC`, `QR-CW`) is consolidated here as the canonical taxonomy the reference-marker extractor (story 04) and the retrieval store (story 05a) reuse.

## Reading

- `docs/design/03-analysis-layer/synthesizer.md` § Inputs — the six brief sources and their reference prefixes
- `docs/design/03-analysis-layer/synthesizer.md` § Source reference mechanism — `[<prefix>-<index>]` format
- `docs/design/03-analysis-layer/synthesizer.md` § Retrieval store side-effect — downstream consumption pattern that motivates the typed structure
- `docs/design/testing/llm-output-validation.md` § Reference-ID taxonomy — producer-side format rules consolidated by upstream agent
- `docs/implementation/02-distillation-layer/11b-correlation-regime-brief-assembly.md` — `CorrelationRegimeBrief` shape (one of the upstream types this work tree adapts to `BriefBundle`)
- `docs/implementation/03-analysis-layer/domain-researchers/03-sector-brief-data-model.md` — `SectorBrief` shape and reference-ID regexes (another upstream type this work tree adapts)

## Depends on

- 02 (so the `synthesizer/` Python module path under `src/alphamind/analysis/synthesizer/` is established and the package is importable from tests).

## Scope

In scope: under `src/alphamind/analysis/synthesizer/` —

- `models.py` defining the Pydantic v2 dataclasses below. Use `pydantic.BaseModel` with `model_config = {"frozen": True}` so instances are hashable and immutable; downstream callers (extractor, retrieval store, bundle assembler) treat them as values, not records.

- Closed-set enums via `enum.StrEnum` (Python 3.13 native):
  - `BriefSource`: `SA_TECH = "sa_tech"`, `SA_FIN = "sa_fin"`, `SA_ENERGY = "sa_energy"`, `QR = "qr"`, `AR = "ar"`, `CR = "cr"`. Names the upstream agent class; one `BriefBundle` per source per invocation.
  - `ReferencePrefix`: every prefix family the synthesizer indexes. The taxonomy is fixed by the upstream contracts and the testing/llm-output-validation reference taxonomy:
    - `SA_TECH = "SA-TECH"`, `SA_TECH_ANOM = "SA-TECH-ANOM"`, `SA_TECH_TC = "SA-TECH-TC"`
    - `SA_FIN = "SA-FIN"`, `SA_FIN_ANOM = "SA-FIN-ANOM"`, `SA_FIN_TC = "SA-FIN-TC"`
    - `SA_ENERGY = "SA-ENERGY"`, `SA_ENERGY_ANOM = "SA-ENERGY-ANOM"`, `SA_ENERGY_TC = "SA-ENERGY-TC"`
    - `QR = "QR"`, `QR_CW = "QR-CW"`
    - `AR = "AR"`
    - `CR = "CR"`

- A `SOURCE_PREFIXES: dict[BriefSource, frozenset[ReferencePrefix]]` mapping declares which prefix families each source produces. The reference-marker extractor (story 04) consumes this; tests cite this map rather than re-listing prefixes in fixtures.
  - `BriefSource.SA_TECH → {SA_TECH, SA_TECH_ANOM, SA_TECH_TC}`
  - `BriefSource.SA_FIN → {SA_FIN, SA_FIN_ANOM, SA_FIN_TC}`
  - `BriefSource.SA_ENERGY → {SA_ENERGY, SA_ENERGY_ANOM, SA_ENERGY_TC}`
  - `BriefSource.QR → {QR, QR_CW}`
  - `BriefSource.AR → {AR}`
  - `BriefSource.CR → {CR}`

- `BriefBundle` model with fields:
  - `source: BriefSource` — which upstream produced this brief.
  - `text: str` — the full brief body as text the synthesizer's LLM will see and the extractor will scan for `[PREFIX-N]` markers. Empty string is allowed (a "no qualifying findings" brief is structurally valid; sequential indexing will simply yield no entries).
  - `freshness: datetime` — UTC, tz-aware. The freshness timestamp the upstream brief reports; surfaces into the synthesizer's bundle so the LLM can see the as-of for each source.
  - `prefixes: frozenset[ReferencePrefix]` — the prefix families this bundle's text is expected to use; defaulted to `SOURCE_PREFIXES[source]` via a Pydantic `model_validator(mode="after")` if not explicitly passed. Explicit override is permitted for tests but always reduces (subset of) the source's declared prefixes — supplying a prefix not in `SOURCE_PREFIXES[source]` is rejected at construction.

- Reference-ID parsing helper (small, pure, stateless):
  - `parse_reference_id(ref_id: str) -> tuple[ReferencePrefix, int] | None` — returns the `(prefix, index)` pair if `ref_id` is well-formed (matches one of the declared prefixes followed by `-<positive integer>`), else `None`. Used by story 04's extractor and story 05a's lookup. The function consults `ReferencePrefix` (longest-match wins so `SA-TECH-ANOM-3` resolves as `(SA_TECH_ANOM, 3)` not `(SA_TECH, ANOM-3)`).

- Unit tests:
  - `BriefSource` and `ReferencePrefix` have exactly the documented members (regression check against accidental additions).
  - `SOURCE_PREFIXES` covers every `BriefSource` member; every prefix in every value is a member of `ReferencePrefix`.
  - `BriefBundle` constructs with explicit `prefixes` matching the source's declared set.
  - `BriefBundle` constructs with omitted `prefixes` and the validator fills the default from `SOURCE_PREFIXES`.
  - `BriefBundle` rejects `prefixes` containing a value outside `SOURCE_PREFIXES[source]` (e.g., a `BriefSource.SA_TECH` bundle declaring `QR` prefix).
  - `BriefBundle` rejects naive (tz-unaware) `freshness`.
  - `BriefBundle` accepts `text == ""` (empty brief is structurally valid).
  - `BriefBundle` is hashable (test by adding to a `frozenset`).
  - `parse_reference_id("SA-TECH-3")` returns `(ReferencePrefix.SA_TECH, 3)`.
  - `parse_reference_id("SA-TECH-ANOM-3")` returns `(ReferencePrefix.SA_TECH_ANOM, 3)` — longest match.
  - `parse_reference_id("SA-TECH-TC-1")` returns `(ReferencePrefix.SA_TECH_TC, 1)`.
  - `parse_reference_id("QR-4")` returns `(ReferencePrefix.QR, 4)`.
  - `parse_reference_id("QR-CW-1")` returns `(ReferencePrefix.QR_CW, 1)`.
  - `parse_reference_id("CR-7")` returns `(ReferencePrefix.CR, 7)`.
  - `parse_reference_id("XX-1")` returns `None` (unknown prefix).
  - `parse_reference_id("SA-TECH--1")`, `parse_reference_id("SA-TECH-0")` (zero), `parse_reference_id("SA-TECH-3a")` (non-integer suffix), `parse_reference_id("")` all return `None`.

Out of scope:
- The text-scanning extractor that finds `[PREFIX-N]` markers in a brief body (story 04).
- The retrieval store assembly that uses `BriefBundle` as input (story 05a).
- The adapters that convert `SectorBrief` / `CorrelationRegimeBrief` / future qualitative-research and adaptive-research outputs into `BriefBundle` instances (story 05a covers the canonical adapters).
- The synthesizer's bundle assembler that composes `BriefBundle.text` blocks into the LLM's user message (story 07).

## Notes

Why Pydantic v2 over plain `@dataclass(frozen=True)`: the cross-field validator on `prefixes ⊆ SOURCE_PREFIXES[source]` is the kind of constraint Pydantic expresses cleanly; the runtime cost is acceptable on a per-invocation basis (one bundle per upstream, six total).

The `ReferencePrefix` enum encodes the prefix *string* (e.g., `"SA-TECH-ANOM"`) as the value. The `parse_reference_id` longest-match guarantee depends on this string form: when scanning a candidate `SA-TECH-ANOM-3`, the function tries the longest prefix family first (`SA-TECH-ANOM` → match, integer suffix `3`) before falling back to `SA-TECH`. Implementing the longest-match logic deterministically requires a stable iteration order — sort prefixes by descending string length at lookup time, or precompute a reverse-sorted tuple at module load.

The `prefixes` field on `BriefBundle` exists rather than re-deriving from `source` at every consumer because the qualitative and adaptive briefs have not yet been implemented. When their work trees land, those producers may carry richer structure (e.g., a qualitative producer might emit a brief with a deferred / partial prefix set — for now, the contract from `qualitative-research.md` is `QR-N` + `QR-CW-N` always, but the field gives forward flexibility without forcing a model change). The default-from-source behavior keeps the common case clean.

The `freshness` timestamp is plain UTC `datetime`. No surrogate "freshness band" enum — the synthesizer's LLM consumes the literal timestamp via story 07's bundle assembler. Per `docs/design/01-data-layer/api-failure-handling.md` and `mid-pipeline-failure-handling.md`, the no-stale-fallback discipline upstream means freshness in practice is always within one invocation cadence; a stale brief means the upstream aborted, not that this layer accepts it.

Anti-pattern: do NOT add a `signal_quality` field on `BriefBundle`. Each upstream brief surfaces its own `signal_quality` (HIGH | MODERATE | LOW | DEGRADED) inside its text body — the synthesizer reads it and weights accordingly. Lifting it to the bundle would create a parallel structured surface the synthesizer's prompt would have to reconcile against the in-text version; the design's "single load-bearing constraint is reference-ID embedding" holds.

Per [`feedback_no_inventing_component_names.md`](../../../../.claude/projects/-Users-jatassi-Git-AlphaMind/memory/feedback_no_inventing_component_names.md), the names introduced here (`BriefBundle`, `BriefSource`, `ReferencePrefix`, `SOURCE_PREFIXES`, `parse_reference_id`) are the implementation lexicon for this work tree; downstream stories cite them by name.

## Acceptance criteria

- [ ] `src/alphamind/analysis/synthesizer/models.py` defines `BriefSource`, `ReferencePrefix`, `BriefBundle`, `SOURCE_PREFIXES`, and `parse_reference_id`.
- [ ] `BriefSource` and `ReferencePrefix` enums have exactly the documented members (verified by a unit test asserting the member set).
- [ ] `SOURCE_PREFIXES` keys cover every `BriefSource` member; every value is a `frozenset[ReferencePrefix]`.
- [ ] `BriefBundle` constructed with omitted `prefixes` defaults to `SOURCE_PREFIXES[source]`.
- [ ] `BriefBundle` rejects `prefixes` containing a value outside `SOURCE_PREFIXES[source]`.
- [ ] `BriefBundle` rejects naive (tz-unaware) `freshness`.
- [ ] `BriefBundle` is hashable.
- [ ] `parse_reference_id` correctly resolves all six upstream prefix families including the longest-match case (`SA-TECH-ANOM-3` → `(SA_TECH_ANOM, 3)`, not `(SA_TECH, anom-3)`).
- [ ] `parse_reference_id` returns `None` for unknown prefix, missing index, non-integer index, zero index, or empty input.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
