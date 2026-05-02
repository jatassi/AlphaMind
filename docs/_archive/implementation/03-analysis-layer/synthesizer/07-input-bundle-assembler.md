---
status: not_started
completed_date:
commit_id:
---

# 07 — Input bundle assembler

## Goal

Implement the function that composes the user-message text the synthesizer's harness (story 08) sends to the LLM: the volatility regime label, the six upstream brief texts (each with its source label and freshness), and a brief reminder of the available portfolio-state tools. Output is a single deterministic string the LLM reads as its full input. Mirrors the shape of the domain-researcher input bundle assembler ([story 08 there](../domain-researchers/08-input-bundle-assembler.md)) — same byte-stable, snapshot-tested discipline.

## Reading

- `docs/design/03-analysis-layer/synthesizer.md` § Inputs — the six brief sources, the volatility regime label, the on-demand portfolio tools
- `docs/design/03-analysis-layer/synthesizer.md` § Portfolio state tools — usage pattern reminder the bundle frames for the LLM
- `docs/design/03-analysis-layer/synthesizer.md` § Source reference mechanism — the `[<prefix>-<index>]` citation contract the bundle text references in framing
- `docs/design/03-analysis-layer/synthesizer.md` § Contradiction and uncertainty handling — the contract the bundle text reminds the LLM of
- `docs/implementation/03-analysis-layer/domain-researchers/08-input-bundle-assembler.md` — the sibling assembler this story mirrors stylistically (determinism, snapshot tests, empty-section placeholders)
- `docs/architecture/llm-integration.md` § Prompt management — separation of system prompt (story 09) from user message (this story)

## Depends on

- 03 (`BriefBundle`, `BriefSource`)
- 04 (the extractor itself is not consumed here, but the prefix taxonomy framing the bundle reminds the LLM of comes from `SOURCE_PREFIXES`)
- The `RegimeLabel` model from the domain-researchers work tree (`alphamind.analysis.domain_researchers.input_bundle.RegimeLabel`, defined in their story 08). Reused here rather than redefined.

## Scope

In scope: under `src/alphamind/analysis/synthesizer/` —

- `input_bundle.py` defining:

  - `class SynthesizerInputBundle(BaseModel, frozen=True)` — the composed bundle as a value object (also used for diagnostic preservation in story 08):
    - `invocation_id: str`
    - `as_of: datetime` (UTC, tz-aware) — the synthesizer-stage start timestamp; not the same as any individual brief's freshness
    - `regime: RegimeLabel` (imported from the domain-researchers tree)
    - `bundles: tuple[BriefBundle, ...]` — every upstream brief, ordered as supplied (the runner orders them; see story 10)
    - `bundle_text: str` — the full concatenated user message ready for the LLM

  - `def assemble_synthesizer_input(invocation_id: str, as_of: datetime, regime: RegimeLabel, bundles: Iterable[BriefBundle]) -> SynthesizerInputBundle` — pure function. Composes `bundle_text` per the deterministic format below.

- Bundle text format (deterministic, byte-stable on identical inputs):

  ```
  # SYNTHESIZER INPUT BUNDLE
  Invocation: {invocation_id}
  As of: {as_of ISO 8601 UTC}

  ## VOLATILITY REGIME (universal context)
  Label: {regime.label}
  Transition state: {regime.transition_state}
  {Prior label: {regime.prior_label} when transition_state != "stable"}

  ## UPSTREAM BRIEFS

  ### {SOURCE_LABEL_1} (freshness: {freshness ISO 8601 UTC})
  {bundle_1.text — passed through verbatim}

  ### {SOURCE_LABEL_2} (freshness: {freshness ISO 8601 UTC})
  {bundle_2.text — passed through verbatim}

  ...

  ## PORTFOLIO STATE TOOLS (on-demand)

  - get_positions_summary — current open positions
  - get_active_theses_summary — active thesis snapshots
  - get_exposure_snapshot — sector / directional / gross exposure

  Call these only when an upstream finding plausibly relates to existing positions or
  exposure. On quiet days with no portfolio-relevant signals, none of these tools may
  be called.

  ## REFERENCE CITATION REMINDER

  Every claim in your output that traces to an upstream brief must cite the source
  reference in brackets, e.g., [SA-TECH-3] or [QR-4] or [CR-1]. Invented references
  will be caught by downstream validators.
  ```

- `SOURCE_LABEL` mapping (constant): `BriefSource → str` for the per-brief headings:
  - `BriefSource.SA_TECH → "TECH & SEMIS DOMAIN BRIEF (prefix SA-TECH)"`
  - `BriefSource.SA_FIN → "FINANCIALS DOMAIN BRIEF (prefix SA-FIN)"`
  - `BriefSource.SA_ENERGY → "ENERGY DOMAIN BRIEF (prefix SA-ENERGY)"`
  - `BriefSource.QR → "BASELINE QUALITATIVE BRIEF (prefix QR)"`
  - `BriefSource.AR → "ADAPTIVE RESEARCH FINDINGS (prefix AR)"`
  - `BriefSource.CR → "CORRELATION & REGIME BRIEF (prefix CR)"`

- Determinism:
  - The bundle is byte-identical when called with byte-identical inputs (load-bearing for invocation-archive diffability and for prompt-cache hit rate per `cost-and-rate-limit-modeling.md`'s prompt-cache assumptions).
  - Iterate `bundles` in the supplied order — do NOT re-sort here. The runner is responsible for sort order; this assembler preserves it.
  - Use UTC timestamps consistently; do not convert to local time anywhere.
  - The `Prior label:` line appears only when `regime.transition_state != "stable"`.

- Empty-bundle handling:
  - An empty `bundles` tuple is structurally valid and renders the `## UPSTREAM BRIEFS` section with a single line `(no upstream briefs supplied)`. This is a degenerate case (the runner should fail-closed before reaching the assembler if any required brief is missing — see story 10) but the assembler handles it gracefully so test fixtures and degenerate-state diagnostics work.
  - An individual `BriefBundle` with empty `text` renders the section heading and a single line `(no findings in this brief)` rather than emitting a blank section body. Same rationale as the domain-researcher assembler's empty-section placeholders.

- Unit tests:
  - Identical inputs produce byte-identical `bundle_text` across repeated calls (deterministic snapshot test).
  - Each section is rendered with the documented heading format.
  - `Prior label:` line appears only when `transition_state != "stable"`.
  - Each `BriefSource` renders with its documented `SOURCE_LABEL`.
  - Brief-source ordering is preserved from the supplied `bundles` argument (no re-sort).
  - Empty `bundles` tuple renders `(no upstream briefs supplied)`.
  - A `BriefBundle` with empty `text` renders `(no findings in this brief)`.
  - All timestamps are ISO 8601 UTC; no local-time conversion anywhere; naive timestamps in `as_of` or `regime.prior_label` reject at construction (Pydantic validator on `SynthesizerInputBundle.as_of`).
  - A snapshot test asserts the full bundle text against a canonical fixture for at least one non-empty case (three sector briefs + one CR + one QR + one AR + a transitional regime).

Out of scope:
- The harness call that consumes the bundle (story 08).
- The retrieval store assembly — `assemble_retrieval_store` and `assemble_synthesizer_input` are independent transforms over the same `bundles` input; the runner (story 10) calls both.
- Token-budget enforcement — the harness sets `max_tokens` for output budgeting; input-side budget enforcement is per `llm-agent-failure-handling.md` (the harness counts input tokens and refuses to dispatch if oversized; this assembler is upstream of that check and produces whatever text the inputs warrant).
- The system prompt — that is delivered separately via `ClaudeAgentOptions(system_prompt=...)` in story 08.

## Notes

The bundle is the LLM's *user* message; the system prompt (story 09) is delivered separately. Per `llm-integration.md § Prompt management`: data payloads go in the user message, the role definition in the system prompt. This story produces the data payload only.

The `## REFERENCE CITATION REMINDER` and `## PORTFOLIO STATE TOOLS` sections at the bottom are deliberate framing repetition — the system prompt covers the same ground, but the proximity to the brief content reinforces both behaviors at the point of consumption. Per `feedback_no_decision_trails.md`, the reminder is stated positively ("must cite") rather than as a list of prohibitions.

The `RegimeLabel` import from the domain-researchers tree is the cleanest reuse — the synthesizer and domain researchers consume the same universal-broadcast value. Defining a parallel `RegimeLabel` here would be the kind of duplication `feedback_no_inventing_component_names.md` warns against.

The bundles' source order is the runner's responsibility per story 10. The natural order is: regime label first (universal context, already in the bundle's regime block), then `CR` (cross-asset framing), then sector briefs in alphabetical order (`SA-ENERGY`, `SA-FIN`, `SA-TECH`), then `QR` (qualitative), then `AR` (adaptive findings layered on top of everything else). This matches the analysis-layer execution flow per `cost-and-rate-limit-modeling.md` and gives the LLM a logical reading sequence: "here's the macro frame; here's what each sector saw; here's what the narrative says; here's what targeted investigation found." Documenting the convention here lets the runner cite this order without restating it.

Per [`feedback_simplify_before_building.md`](../../../../.claude/projects/-Users-jatassi-Git-AlphaMind/memory/feedback_simplify_before_building.md), this story does NOT introduce a `BundleSection` or `BundleRenderer` abstraction. The function builds the string directly. The rendering format is unlikely to fragment into per-section rendering rules; if it ever does, refactor at that point.

Per [`feedback_avoid_numeric_anchors.md`](../../../../.claude/projects/-Users-jatassi-Git-AlphaMind/memory/feedback_avoid_numeric_anchors.md), the framing text deliberately omits any "find at least N intersections" or "flag at least N contradictions" guidance. Synthesis density is whatever the upstream signals warrant; the system prompt's role is to set quality expectations, not numeric targets.

## Acceptance criteria

- [ ] `SynthesizerInputBundle` defined as a frozen Pydantic model with `invocation_id`, `as_of`, `regime`, `bundles`, `bundle_text` fields.
- [ ] `assemble_synthesizer_input(...)` returns a `SynthesizerInputBundle` whose `bundle_text` follows the documented format.
- [ ] Byte-identical inputs produce byte-identical `bundle_text` (verified by a deterministic snapshot test).
- [ ] The bundle includes the four sections in declared order: header, regime, upstream briefs, portfolio tools, citation reminder.
- [ ] `Prior label:` appears only when `regime.transition_state != "stable"`.
- [ ] Each `BriefSource` renders with its documented `SOURCE_LABEL` heading.
- [ ] Brief order is preserved from the supplied `bundles` argument (no re-sort).
- [ ] Empty `bundles` tuple renders `(no upstream briefs supplied)`.
- [ ] A `BriefBundle` with empty `text` renders `(no findings in this brief)`.
- [ ] All timestamps are ISO 8601 UTC; naive timestamps reject at construction.
- [ ] A snapshot test asserts the full bundle text against a canonical fixture for at least one non-empty case spanning all six brief sources.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
