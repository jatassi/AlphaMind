---
status: not_started
completed_date:
commit_id:
---

# 11b — Correlation/regime brief assembly (CR brief)

## Goal

Assemble the correlation/regime brief the synthesizer agent consumes (reference prefix `CR`). This brief contains the category-7 cross-asset computations — intra-sector divergences, cross-sector rotation, intermarket regime signals, lead-lag gaps, correlation regime changes — plus the universal volatility regime label and the universal-broadcast macro context. The brief uses producer-side `CR-N` reference indexing the synthesizer cites in its output.

## Reading

- `docs/design/02-distillation-layer/external.md` § Output format — "Synthesizer agent (via correlation brief): Cross-asset correlation, lead-lag, regime outputs — category 7 computations"
- `docs/design/03-analysis-layer/synthesizer.md` § Inputs — `CR` row: "Category-7 cross-asset data — intra-sector divergences, cross-sector rotation, intermarket regime signals, lead-lag gaps, correlation regime changes"
- `docs/design/03-analysis-layer/synthesizer.md` § Source reference mechanism — `[CR-N]` citation contract the synthesizer uses to point back at this brief's findings
- `docs/design/testing/llm-output-validation.md` § Layer 3 — referential-integrity check that consumers run on `CR-N` citations
- Story 09's `regime.label` block — the universal regime context this brief embeds
- Stories 08c, 08d, 10 — the upstream blocks and aggregation primitives

## Depends on

- 10 (anomaly aggregation + partitioning).

## Scope

In scope: under `src/alphamind/distillation/correlation_brief.py` —

- **`CorrelationRegimeBrief`** dataclass:
  - `text: str` — the assembled brief body.
  - `reference_index: dict[str, str]` — mapping from `CR-{N}` reference IDs to the source `block_id` of the finding the reference covers. Used to populate the brief store / retrieval index per [`synthesizer.md § Retrieval store side-effect`](../../../design/03-analysis-layer/synthesizer.md#retrieval-store-side-effect) — the synthesizer indexes its inputs as-emitted.
  - `freshness_min: datetime` — oldest contributing block's `freshness_ts`.
- **`assemble_correlation_brief(blocks: Iterable[OutputBlock], invocation_id: str) -> CorrelationRegimeBrief`**:
  1. Filter blocks to those with `audience` containing `OutputAudience.CORRELATION_REGIME_BRIEF`. Also include the `regime.label` block (universal broadcast — but specifically embedded here as the regime context the synthesizer reasons over, not just one item among many).
  2. Decompose blocks into individual findings. Each finding becomes a `[CR-N]` entry. The decomposition: a single block may carry multiple findings in its payload (e.g., `q7.lead_lag` carries one finding per pair); each finding gets its own `CR-N`. A simple block with one logical finding (e.g., `q7.cross_sector_rotation`) becomes a single `CR-N`.
  3. Assign sequential indexing: `CR-1`, `CR-2`, ..., `CR-N`. The order is deterministic — sort findings by category then by source `block_id` then by intra-payload natural key.
  4. Build the document framing per the suggested layout below.
- **Document framing**:

  ```
  CORRELATION & REGIME BRIEF
  Invocation: {invocation_id}
  Effective freshness: {freshness_min ISO8601 UTC}

  === REGIME ===
  [CR-1] {regime label} ({transition state}, indicator agreement {N}/4)
    Prior label: {prior_label or "n/a (first invocation)"}
    Invocations held: {invocations_held}
    Underlying: VIX {value}, term-structure basis {value}, VVIX percentile {value}, realized vol {value}
    {if regime_skip_emergency: + emergency-skip flag, ladder skip from {old} → {new}}

  === INTRA-SECTOR CORRELATION ===
  [CR-N] {sector}: {finding summary}
    Detail: {key data points}
    {anomaly mention if attached}

  === CROSS-SECTOR ROTATION ===
  [CR-N] {finding}
    ...

  === INTERMARKET REGIME SIGNALS ===
  [CR-N] {relationship}: {finding}
    ...

  === LEAD-LAG ===
  [CR-N] {pair}: {finding}
    ...

  === CORRELATION REGIME CHANGE ===
  [CR-N] {finding}
    ...

  === UNIVERSAL CONTEXT ===
  {format_blocks_for_audience(universal_blocks_excluding_regime_label, UNIVERSAL_BROADCAST)}

  {anomaly summary section from story 10's assemble_audience_output, scoped to CORRELATION_REGIME_BRIEF audience}
  ```

  Section ordering matches the design doc's "category 7" enumeration. Empty sections are omitted entirely.

- **Reference-index population**: `reference_index["CR-1"] = "regime.label"`, `reference_index["CR-7"] = "q7.lead_lag"` (or richer payload-pointer form like `"q7.lead_lag::funding_to_credit"` if multi-finding blocks need disambiguation). The exact indexing scheme is up to the implementer but must be reversible — the synthesizer's retrieval tool must be able to resolve a `CR-N` back to the originating block payload.
- **Cross-platform validity**: `CR-N` IDs are sequential per invocation and never re-used across invocations (each invocation produces its own brief from scratch). The reference index lives only for the duration of this invocation's brief store; it does not persist as state.
- Unit tests:
  - `assemble_correlation_brief` produces a document with `CR-N` entries in the documented sections.
  - The regime section is always populated (even on first deploy with `prior_label = None`).
  - Sequential indexing is contiguous with no gaps; no `CR-N` ID is reused within the brief.
  - `reference_index` correctly maps every `CR-N` back to a source block.
  - Empty sections (no Q7 lead-lag findings this invocation) are omitted from the document.
  - Document is byte-identical across repeated calls.
  - `freshness_min` reflects the oldest contributing block.
  - The anomaly summary section appears at the end and is scoped to the `CORRELATION_REGIME_BRIEF` audience.
  - The universal-context section excludes the `regime.label` block (it's already embedded in the regime section).

Out of scope:
- Brief-store persistence (orchestrator's responsibility — story 12).
- Invocation archive write-out.
- The synthesizer's downstream consumption logic (analysis layer).

## Notes

The `CR-N` indexing convention is load-bearing — the synthesizer's output cites `[CR-N]` references that downstream agents (analyst, strategist, PM) resolve via the retrieval tool. Per [`llm-output-validation.md § Layer 3 — Referential integrity`](../../../design/testing/llm-output-validation.md), invented `CR-N` references are caught by the referential-integrity validator at the consumer side. This story produces clean, sequential IDs and the reverse index that powers retrieval.

The decomposition decision (block → multiple findings vs. block → one finding) needs care. The sensible cut: each `CR-N` corresponds to one logical claim the synthesizer might cite. A `q7.lead_lag` block covering five pairs produces five `CR-N`s — the synthesizer might cite "[CR-7]" for the funding-to-credit overdue lag specifically, not the entire block. Coordinate the decomposition with the 08d implementation; the cleanest approach is for 08d to emit one block per pair (rather than one block listing all pairs), letting this story produce one CR per block trivially.

The regime-section embedding of the universal `regime.label` block is intentional: the synthesizer needs the regime label as the framing context for every other CR finding. Embedding it in-line (as `CR-1`, always) makes citation easy ("the current low_vol_compression regime [CR-1] explains why..."). Other universal-broadcast blocks (macro, breadth) live in the trailing universal-context section without `CR-N` IDs — the synthesizer can cite them indirectly through the underlying source block IDs, or via the retrieval tool by `block_id`.

The "no `CR-N` reused across invocations" property matches how the synthesizer handles its own reference space — every invocation is a fresh context window per the design's [fresh-context principle](../../../design/llm-agent-failure-handling.md#context-overflow). The reference index's lifetime is one invocation; persistence is the brief-store's job, not this primitive's.

If the decomposition turns out to need disambiguation richer than `block_id` alone, use `block_id::natural_key` as the index value (e.g., `"q7.lead_lag::funding_to_credit"`). The retrieval tool resolves the natural key against the block's payload structure.

## Acceptance criteria

- [ ] `CorrelationRegimeBrief` dataclass exists with `text`, `reference_index`, `freshness_min`.
- [ ] `assemble_correlation_brief` produces a document with `CR-N` entries in the documented sections.
- [ ] Regime section is always populated, including on first deploy.
- [ ] `CR-N` indexing is sequential, contiguous, and unique within the brief.
- [ ] `reference_index` correctly maps every `CR-N` back to a source block (or block + payload key).
- [ ] Empty sections are omitted from the document.
- [ ] Document text is byte-identical across repeated calls.
- [ ] `freshness_min` reflects the oldest contributing block.
- [ ] Anomaly summary section appears at the end, scoped to `CORRELATION_REGIME_BRIEF` audience.
- [ ] Universal-context section excludes the `regime.label` block (no duplication with the regime section).
- [ ] Unit tests cover all of the above with fixture data.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
