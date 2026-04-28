---
status: done
completed_date: 2026-04-28
commit_id: 476f855e1a9c1a0451e9e38095cc634eb67675eb
---

# 11a — Sector-scoped output assembly

## Goal

Assemble the three per-sector output documents the domain researcher agents (tech-semis / financials / energy) consume. Each document is the per-sector slice of distillation output: indicators, anomalies, divergences scoped to that sector's tickers, plus the universal-broadcast items (macro, regime, breadth) that frame interpretation.

## Reading

- `docs/design/02-distillation-layer/external.md` § Output format — "Sector analyst agents: Their sector's tickers with full distillation (indicators, anomalies, divergences)"
- `docs/design/03-analysis-layer/domain-researchers/tech-semis.md` § Inputs — "Distillation output for tech & semis tickers ... Per-ticker indicators ... anomaly flags, and divergence detections — all scoped to the ~35 tech and semiconductor names"
- `docs/design/03-analysis-layer/domain-researchers/financials.md` and `energy.md` — sibling consumers (same input shape, different sector scope)
- `docs/design/01-data-layer/collector/storage.md` § `sector_classification` — the table that scopes tickers to their `domain_researcher` (`tech_semis` / `financials` / `energy`)
- Stories 05's `OutputAudience`, 10's `assemble_audience_output`

## Depends on

- 10 (anomaly aggregation + partitioning).

## Scope

In scope: under `src/alphamind/distillation/sector_assembly.py` —

- **`SectorOutput`** dataclass:
  - `audience: OutputAudience` (one of `SECTOR_TECH_SEMIS`, `SECTOR_FINANCIALS`, `SECTOR_ENERGY`).
  - `sector_label: str` — the human-readable label matching the domain researcher's `Sector brief: {sector_label}` template.
  - `text: str` — the assembled document body.
  - `tickers: tuple[str, ...]` — the tickers scoped to this sector (read from `sector_classification.alphamind_sector` for the sector that maps to the `domain_researcher` field).
  - `block_ids: tuple[str, ...]` — every block_id included in the output (for audit).
  - `freshness_min: datetime` — the oldest `freshness_ts` of any block in the output (downstream consumers use this as the document's effective freshness).
- **`assemble_sector_output(audience: OutputAudience, blocks: Iterable[OutputBlock], session) -> SectorOutput`** — the main entry point:
  1. Read the sector's tickers from `sector_classification` for the corresponding `domain_researcher` value.
  2. Filter the input blocks: keep blocks whose `audience` set includes the requested sector audience, OR includes `UNIVERSAL_BROADCAST`.
  3. Within the filtered set, further restrict per-ticker blocks to tickers that match the sector's roster (a Q1 block tagged as `q1.technicals` carries a per-ticker payload — only emit blocks whose payload ticker is in the sector roster).
  4. Build the document framing per the suggested layout below.
- **Document framing** (the structured-text shape the document conforms to; deterministic so the archive diffs cleanly):

  ```
  DISTILLATION OUTPUT — {sector_label}
  Invocation: {invocation_id}
  Tickers: {comma-separated ticker list}
  Effective freshness: {freshness_min ISO8601 UTC}

  === UNIVERSAL CONTEXT ===
  {format_blocks_for_audience(universal_blocks_in_set, UNIVERSAL_BROADCAST)}

  === SECTOR INDICATORS ===
  {format_blocks_for_audience(sector_blocks_in_set, audience)}

  {assemble_audience_output(audience, all_blocks_for_this_audience)'s anomaly summary section — story 10 owns the "ANOMALY FLAGS" header rendering}
  ```

  The actual interleaving (universal context first vs. anomalies first) is up to the implementer but must be consistent across the three sectors. Recommended order: anomaly summary at top (most actionable), then universal context, then sector indicators (deepest detail last).

- The story produces three `SectorOutput` instances per invocation — one per sector. The orchestrator (story 12) calls `assemble_sector_output` three times.
- **`invocation_id` source**: passed in as an argument from the orchestrator; this story does not generate it.
- Unit tests:
  - For each of the three sectors, the assembly produces a `SectorOutput` whose `tickers` matches the `sector_classification` roster.
  - Per-ticker blocks for tickers outside the sector are excluded from that sector's output.
  - Universal-broadcast blocks appear in every sector output.
  - Multi-sector blocks (e.g., a pair-trade signature spanning tech and financials) appear in the outputs of all sectors they target.
  - `freshness_min` reflects the oldest block's `freshness_ts`.
  - Output is byte-identical on repeated calls.
  - Anomaly summary integrates correctly (composability with story 10).
  - Empty case: a sector with no firing blocks still produces a well-formed document (with empty sector indicators and the universal context).

Out of scope:
- The universal-broadcast-only output (consumed by all agents — story 11b's `CR` brief is the synthesizer's specific universal+correlation document; the analyst- and PM-side universal context comes via the synthesizer's brief and is not separately assembled here).
- Brief-store persistence (orchestrator concern in story 12).
- Invocation archive write-out (orchestrator concern).

## Notes

The mapping between `OutputAudience` enum values and `sector_classification.domain_researcher` strings needs a small lookup helper (e.g., `OutputAudience.SECTOR_TECH_SEMIS` ↔ `domain_researcher = "tech_semis"`). Centralize in this module, not scattered across callers.

Per-ticker block payload inspection: stories 08* emit per-ticker blocks where the ticker is in the payload (`q1.technicals` blocks contain ticker keys). To filter by sector roster, this story has to look inside the payload. Define a small convention: per-ticker blocks include a `ticker: str` field at the payload root, OR group themselves under a `per_ticker: dict[str, ...]` payload key. Either way, the sector filter inspects this field and excludes any per-ticker entry whose ticker isn't in the sector roster. Coordinate the convention with the 08* stories — recommended: `payload["ticker"]` for single-ticker blocks; `payload["per_ticker"][ticker]` for grouped blocks.

The "Effective freshness" line is one of the load-bearing parts of this story: domain researchers care whether their input is freshly computed or carries stale data. Surfacing the oldest block's freshness as the document's effective freshness is the conservative choice — a downstream agent reading the document knows nothing newer than this is reflected.

The framing convention (what goes first, what's grouped) should match the expectations the domain researcher prompts encode. Read the tech-semis researcher's expected input shape and align the framing so the agent doesn't have to scan-and-search.

## Acceptance criteria

- [ ] `SectorOutput` dataclass exists with the documented fields.
- [ ] `assemble_sector_output` produces a `SectorOutput` per-sector via the documented filtering rules.
- [ ] Per-ticker blocks are filtered against the sector's `sector_classification` roster.
- [ ] Universal-broadcast blocks appear in every sector output.
- [ ] Multi-sector blocks appear in every sector they target.
- [ ] `freshness_min` reflects the oldest block's freshness.
- [ ] Document text is byte-identical across repeated calls (deterministic).
- [ ] Anomaly summary integrates via story 10's primitives.
- [ ] Empty-block case produces a well-formed document.
- [ ] Unit tests cover all three sectors with fixture data.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
