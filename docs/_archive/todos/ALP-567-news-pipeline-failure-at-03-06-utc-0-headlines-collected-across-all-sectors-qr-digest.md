# News pipeline failure at 03:06 UTC — 0 headlines collected across all sectors + QR digest

## Symptom

In invocation `inv-20260519T030654Z-60f10023`, the news pipeline collected zero headlines for all sectors and the cross-sector QR digest. The empty state is cross-cutting: the qualitative researcher's NEWS DIGEST block shows `Headlines: 0 collected, 0 shown below`, and each domain researcher's input bundle shows `### HEADLINES (top 0 by composite score)`. All three news_search tool calls in the qualitative researcher and adaptive researcher returned `unavailable`.

The qualitative researcher correctly identified this as systematic ("All tool calls returned unavailable — confirming the news API failure is systematic, not a query issue. The empty digest is a real data gap, not a quiet session.") and downgraded signal_quality to "moderate" with explicit reason. But the upstream cause — whether the news collector wasn't running, the API key is missing, the 03:06 UTC pre-market timing has no fresh headlines to collect, or the API itself returned errors — is not surfaced anywhere in the invocation outputs.

This is distinct from \[\[[ALP-535](https://linear.app/alphamind-jatassi/issue/ALP-535/qr-news-digest-empty-while-sector-bundles-populated-cross-sector-roll)\]\] (which was about cross-sector roll-up dropping headlines while sector bundles had content). Here, every layer's news ingestion is empty.

## Evidence

`analysis/qualitative_researcher/user_message.md` line 19-34:

```
## NEWS DIGEST

=== NEWS DIGEST (invocation inv-20260519T030654Z-60f10023, covering 2026-05-18T03:06:54Z → 2026-05-19T03:06:54Z) ===
Headlines: 0 collected, 0 shown below

--- MACRO / CROSS-SECTOR (top 5) ---

--- TECH / SEMIS (top 5) ---

--- FINANCIALS (top 5) ---

--- ENERGY (top 5) ---
```

`analysis/tech_semis_researcher/user_message.md` line 1625: `### HEADLINES (top 0 by composite score)`
`analysis/financials_researcher/user_message.md` line 1458: `### HEADLINES (top 0 by composite score)`
`analysis/energy_researcher/user_message.md` line 1409: `### HEADLINES (top 0 by composite score)`

Adaptive researcher findings (`analysis/adaptive_researcher/response_initial.md`, multiple findings):

* "News searches for CTRA returned unavailable (03:06 UTC pre-market data gap, lookback extended to 168 hours)"
* "news_search for META/GOOG divergence catalysts returned unavailable"
* "All news searches for WFC returned unavailable (03:06 UTC pre-market data gap)"

## Root cause \[hypothesis\]

Three candidate causes; the invocation artifacts do not disambiguate:

1. **News collector not running at 03:06 UTC** — if the collector is scheduled on market hours only, an off-hours invocation would see no fresh rows. The pipeline should surface this as "out-of-hours collection paused" rather than "0 headlines" without context.
2. **API authentication / quota failure** — Finnhub or polygon news endpoint returning errors that get swallowed into empty result sets. Check `collector.err.log` for the 24h window ending 2026-05-19T03:06.
3. **Time-window filter rejecting all rows** — the digest is "covering 2026-05-18T03:06:54Z → 2026-05-19T03:06:54Z" but if collector timestamps are stored in a different timezone or with offset issues, the window may exclude all rows.

## Scope

1. Investigate why news_search tool calls return `unavailable` at 03:06 UTC. Distinguish collector-not-running vs collector-running-but-returning-empty vs API-error-swallowed-as-empty.
2. Surface the upstream cause in the qualitative researcher's NEWS DIGEST block — distinguish "0 collected because collector off-hours" from "0 collected because API failed" from "0 collected because window filter rejected".
3. If the 03:06 UTC pre-market timing is a structural collector pause, document it explicitly so off-hours invocations carry a clear signal-quality caveat at the harness level rather than at the agent level.
4. Confirm whether the news_search MCP tool (used by adaptive/QR) returns a distinct error code vs empty result for "no data" vs "API down". If both currently surface as `unavailable`, distinguish them.

## Acceptance criteria

- [ ] News digest distinguishes "0 collected — collector off-hours" from "0 collected — pipeline error" with explicit reason text
- [ ] news_search MCP tool returns distinct error codes for "no data available" vs "vendor API error" vs "tool unavailable"
- [ ] Unit test confirms the digest assembler emits explicit reason annotations for each cause
- [ ] Domain researcher input HEADLINES section carries the same reason annotation when count is 0

## Verification

* Query `collector.err.log` and `collector.log` for the 24h window ending 2026-05-19T03:06 — confirm the upstream cause (collector paused / API error / window-filter rejection).
* Query the news table directly for the same window to verify whether rows exist and the digest assembler is filtering them out, or rows are genuinely absent.
* Unit test the digest assembler with three synthetic inputs (collector-off-hours, vendor-API-error, empty-real-result) — confirm each produces distinguishable reason text.
* Inspect a single in-hours invocation's `qualitative_researcher/user_message.md` and a single off-hours invocation's; the NEWS DIGEST block should show explicit, distinct reason text in each case.

## Notes

The qualitative researcher's adversarial discipline (explicit "systematic" diagnosis, signal_quality downgrade) is doing the right thing here, but is reasoning from absence rather than from a signal the pipeline gives it. Surfacing the upstream cause prevents agents from re-diagnosing the same condition every off-hours run.
