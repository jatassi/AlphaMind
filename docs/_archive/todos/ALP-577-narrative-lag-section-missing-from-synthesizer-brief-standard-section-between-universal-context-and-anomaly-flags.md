# NARRATIVE LAG section missing from synthesizer brief — standard section between UNIVERSAL CONTEXT and ANOMALY FLAGS

## Symptom

In invocation `inv-20260519T030654Z-60f10023`, the synthesizer's input bundle (`user_message.md`) contains the consolidated CORRELATION & REGIME BRIEF with the following ordered sections:

```
=== REGIME ===              (line 12)
=== INTRA-SECTOR CORRELATION === (line 18)
=== CROSS-SECTOR ROTATION ===    (line 32)
=== INTERMARKET REGIME SIGNALS === (line 37)
=== LEAD-LAG ===                  (line 50)
=== CORRELATION REGIME CHANGE === (line 76)
=== UNIVERSAL CONTEXT ===         (line 83)
=== ANOMALY FLAGS (48) ===        (line 1375)
```

Per the e2e-verification-post-mortem skill's documentation of the standard brief structure, the section ordering should include a NARRATIVE LAG section: "=== NARRATIVE LAG === — count vs qualifying news". This section is absent entirely from this invocation's brief.

Two interpretations:

1. **Section conditionally omitted when empty**: If the narrative-lag computation has 0 qualifying news vs count, the section may be elided. This is the news-pipeline-empty consequence noted in \[\[[ALP-567](https://linear.app/alphamind-jatassi/issue/ALP-567/news-pipeline-failure-at-0306-utc-0-headlines-collected-across-all)\]\] — with 0 headlines collected, narrative lag computation has nothing to lag. But the section should still appear with explicit "0 qualifying / N count" or "unavailable: news pipeline empty" rather than vanishing entirely.
2. **Section dropped entirely** (bug): The brief assembler may have a bug where the NARRATIVE LAG section's render call was removed or short-circuits on empty data.

Either way, downstream agents have no signal that narrative lag was supposed to be in the brief — they can't tell "no narrative lag detected" from "narrative lag never computed" from "narrative lag section accidentally elided".

## Evidence

`analysis/synthesizer/user_message.md` — grep for "narrative lag" or "NARRATIVE LAG" returns zero matches:

```bash
grep -ni "narrative lag\|narrative_lag" analysis/synthesizer/user_message.md
# (no matches)
```

Section ordering directly from the brief:

* Line 76: `=== CORRELATION REGIME CHANGE ===`
* Line 83: `=== UNIVERSAL CONTEXT ===`
* Line 1375: `=== ANOMALY FLAGS (48) ===`

The NARRATIVE LAG section should appear between UNIVERSAL CONTEXT and ANOMALY FLAGS per the skill's documented brief structure.

## Root cause \[hypothesis\]

Two candidates:

1. **Brief assembler section render conditional on data presence**: If the assembler's render code is roughly `if narrative_lag_data: render_section()`, then empty data drops the section entirely. The fix is to render the section header with explicit "unavailable" or "0 qualifying" reason text instead.
2. **NARRATIVE LAG section was removed from the brief assembler**: If recent refactoring removed the section's render call, this is a regression. Check git history of the brief-assembler module.

Hypothesis 1 is more likely given the news pipeline failure (\[\[[ALP-567](https://linear.app/alphamind-jatassi/issue/ALP-567/news-pipeline-failure-at-0306-utc-0-headlines-collected-across-all)\]\]) — the upstream data feeding narrative lag is empty, so the section render correctly identifies "nothing to render" but incorrectly elides instead of explaining.

## Scope

1. Locate the brief-assembler code that emits the consolidated CORRELATION & REGIME BRIEF sections. Confirm whether the NARRATIVE LAG section is conditional on data presence.
2. If conditional, change the render logic to always emit the section header with explicit empty-state text (e.g., "=== NARRATIVE LAG === \\n No qualifying narratives for window (news pipeline empty)" or similar).
3. Audit all brief sections for the same conditional-elision pattern. Other sections that may silently drop on empty data should also surface their empty state explicitly.
4. Document the contract: every brief section has a fixed position and either renders data or renders an explicit empty-state marker, never drops silently.

## Acceptance criteria

- [ ] NARRATIVE LAG section appears in every synthesizer brief — either with data or with explicit empty-state reason
- [ ] All other consolidated-brief sections (REGIME, INTRA-SECTOR CORRELATION, CROSS-SECTOR ROTATION, INTERMARKET REGIME SIGNALS, LEAD-LAG, CORRELATION REGIME CHANGE, UNIVERSAL CONTEXT, ANOMALY FLAGS, NARRATIVE LAG) follow the same "never silently drop" contract
- [ ] Brief assembler test verifies all expected sections present in a no-data invocation

## Verification

* Read the brief-assembler code path. Identify each section's render call and confirm whether it short-circuits on empty data.
* Unit test the brief assembler with synthetic inputs where NARRATIVE LAG data is empty. Confirm the assembled output contains the `=== NARRATIVE LAG ===` header followed by explicit empty-state text.
* Repeat for every section to confirm "never silently drop" applies uniformly.
* Inspect a future synthesizer `user_message.md`; grep for `narrative lag` and confirm the section header is present.

## Notes

This is related to \[\[[ALP-567](https://linear.app/alphamind-jatassi/issue/ALP-567/news-pipeline-failure-at-0306-utc-0-headlines-collected-across-all)\]\] (news pipeline empty) — if NARRATIVE LAG depends on news data, the empty news pipeline is the upstream cause. But the brief-assembler bug (silently dropping the section) is independent of the upstream issue and should be fixed separately. The contract is: empty data → render section with empty-state text; never drop the section header.
