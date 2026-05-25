End-to-end verification on 2026-05-04 (against commit aa9778c, after fixing the [ALP-288](https://linear.app/alphamind-jatassi/issue/ALP-288/migrate-analysis-layer-text-format-agents-to-json-schema-structured) `max_turns=1` regression in `domain_researchers/harness.py`) surfaced three issues in the synthesizer output that `verify_synthesizer.py` did not flag. None blocked the phase-5 PASS verdict, but each violates an explicit design contract or polish bar in `docs/design/03-analysis-layer/synthesizer.md`.

Run archive: `.archive/verify-pipeline-20260504/invocations/20260504T192329Z-verify-pipeline/analysis/synthesizer/`

## 1\. Un-numbered `[CR]` correlation-pair references — contract violation

**Severity:** High (contract)

**Issue:** The synthesizer output emits four correlation-divergence references without an index:

* `(2.38σ [CR])` for AMZN:DDOG
* `(2.40σ [CR])` for INTC:MRVL
* `(2.30σ)` for GOOGL:META
* `(2.29σ)` for GOOG:META

The design at `docs/design/03-analysis-layer/synthesizer.md` § Output (line 92) calls reference-ID embedding "the single load-bearing constraint" — every claim must cite `[<prefix>-<index>]`. Bare `[CR]` does not resolve to any retrieval-store entry, so a decision-layer agent following the trace gets nothing.

**Why the verifier missed it:** `verify_synthesizer.py`'s reference-extraction regex did not match `[CR]` (no index) as a reference, so it was neither resolved nor flagged as invented. The verdict's "Invented references: 0" is true under the regex but misses these contract violations.

**Fix:**

* Tighten the synthesizer prompt's reference-ID rules to forbid bare `[CR]` and require `[CR-N]` form.
* Extend the Layer-3 referential-integrity check (`docs/design/testing/llm-output-validation.md`) to flag any known-prefix bracket lacking an index as an invented reference.

**Source:** `response.md` lines 15, 17, 25 (and others).

## 2\. TC citation asymmetry across sectors — consistency

**Severity:** Medium (consistency)

**Issue:** When the synthesizer's prose paraphrases a sector researcher's `thesis_candidate`, it sometimes cites the `SA-*-TC-*` ID and sometimes cites only the underlying finding. The synthesis cited `SA-TECH-TC-1` (ORCL long) and `SA-TECH-TC-2` (META short) where it engaged thesis-level conviction, but for `SA-FIN-TC-1` (V long), `SA-FIN-TC-2` (PRU short), `SA-ENERGY-TC-1` (DVN long), and `SA-ENERGY-TC-2` (SLB short) the prose paraphrases TC content (e.g., V's transaction-volume-decoupling thesis appears as a Contradictions framing citing only `SA-FIN-2`, the underlying price-action finding) without citing the TC.

By the design's "every claim traces back to its source" rule ([synthesizer.md](<http://synthesizer.md>) § Source reference mechanism), TC content paraphrased into prose should carry the TC ID alongside any underlying finding ID — not as a propagation requirement (the synthesizer is filter+composition, not propagator), but to keep the trace complete when the synthesizer chooses to engage with TC-originated content.

**Fix:** Tighten the synthesizer prompt to require: when prose claims trace to a thesis candidate's `catalyst`, `conviction_sketch`, `key_risk`, or direction fields, the corresponding `SA-*-TC-*` ID must appear alongside any underlying finding/anomaly ID.

**Source:** Compare `response.md` § Intersections "ORCL multi-source bullish convergence" (cites `[SA-TECH-TC-1]` ✓) vs § Contradictions "V's sector immunity vs. financials breadth collapse" (paraphrases `SA-FIN-TC-1`'s catalyst without citing it).

## 3\. Opening sentence is thinking-residue — polish

**Severity:** Medium (polish)

**Issue:** The synthesizer output begins with:

> The upstream briefs surface multiple high-density signals across a volatile session. Before producing the synthesis, I need portfolio state to determine whether the financials sector collapse, specific-name directional reads (META, NVDA, ORCL, V), and the dense event calendar map to held positions or active theses.---

This is the model narrating its pre-tool-call reasoning, not the synthesis itself. The actual synthesis starts at "**Regime and structural degradation baseline**" on the next line. The text is visible to all three downstream consumers (analyst, strategist, PM) at invocation start.

**Fix:** Add a directive to the synthesizer system prompt: do not narrate tool-call rationale; the output must begin with the synthesis prose itself (the regime baseline section under current structure).

**Source:** `response.md` line 1.

---

## Acceptance criteria

- [ ] Synthesizer prompt rejects bare `[CR]` references; clean re-run produces only `[CR-N]` form for correlation references.
- [ ] Synthesizer prompt requires `SA-*-TC-*` citation when paraphrasing TC content; clean re-run cites the TC ID for any paraphrased thesis-candidate prose.
- [ ] Synthesizer prompt directive blocks pre-tool-call thinking-residue; clean re-run begins with the synthesis prose (no "I need portfolio state" preamble).
- [ ] `verify_synthesizer.py` reference-extraction regex updated to flag known-prefix-without-index as invented; new test case covering bare `[CR]` exits with WARN/FAIL rather than PASS.

## Background

The verification that surfaced these findings used commit aa9778c with two on-the-spot fixes (uncommitted): `domain_researchers/harness.py` `max_turns=1` → `2` and `agents.yaml` `adaptive_researcher.output_token_budget` 16K → 24K. Findings would have been the same on a clean post-[ALP-288](https://linear.app/alphamind-jatassi/issue/ALP-288/migrate-analysis-layer-text-format-agents-to-json-schema-structured) baseline — they are downstream of the JSON-Schema migration only insofar as they reflect post-migration synthesizer output, not the migration mechanics themselves.