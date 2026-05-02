---
status: done
completed_date: 2026-04-28
commit_id: a45cc870b7ce46f3994d8e2ef3451ba5824fde03
---

# 10 — Anomaly aggregation and per-consumer partitioning

## Goal

Implement the central aggregation layer that collects every `AnomalyFlag` emitted by the per-category stories (08*), groups them by consumer audience, and produces the `Anomaly flags` summary view that downstream agents scan first. The partitioning logic also routes each `OutputBlock` to its correct consumer so story 11a/11b can assemble per-consumer output documents without re-deciding routing.

## Reading

- `docs/design/02-distillation-layer/external.md` § Output format — "Anomaly flags: Binary flags plus magnitude per detected anomaly, grouped for easy scanning"
- `docs/design/02-distillation-layer/external.md` § 3 Anomaly detection — the full list of flags this story aggregates
- Story 05's `OutputBlock`, `AnomalyFlag`, `OutputAudience` — the inputs and partition keys
- Stories 08* and 09 — the producers of `OutputBlock` and `AnomalyFlag` instances
- `docs/design/03-analysis-layer/domain-researchers/tech-semis.md` § Domain researcher output contract — the analyst-side anomaly severity taxonomy this story respects

## Depends on

- 05, 08a, 08b, 08c, 08d, 08e, 08f, 09.

## Scope

In scope: under `src/alphamind/distillation/aggregation.py` —

- **`partition_blocks(blocks: Iterable[OutputBlock]) -> dict[OutputAudience, list[OutputBlock]]`**: groups blocks by audience. A block carrying multiple audiences appears in each. Within each audience's list, blocks are sorted by `block_id` ascending (deterministic order).
- **`AnomalySummary`** dataclass:
  - `flag: AnomalyFlag`
  - `source_block_id: str` — the `block_id` of the `OutputBlock` the flag was attached to.
  - `audiences: frozenset[OutputAudience]` — the audiences from the source block.
  - `flagged_at: datetime` — the source block's `freshness_ts`.
  - `calibration_state: CalibrationState` — passthrough from source block (consumers weight `bootstrap`-tagged anomalies lower).
- **`collect_anomalies(blocks: Iterable[OutputBlock]) -> list[AnomalySummary]`**: walks every block, extracts every `AnomalyFlag`, wraps in `AnomalySummary` with the source-block context. Returns a flat list across all blocks.
- **`group_anomalies_by_audience(summaries: Iterable[AnomalySummary]) -> dict[OutputAudience, list[AnomalySummary]]`**: groups by each audience the anomaly's source block is targeted to. An anomaly on a `UNIVERSAL_BROADCAST` block appears in every audience's list (universal anomalies are visible to every consumer). Within each audience, summaries are sorted by severity (severity ranking: `investigate_now` > `investigate_if_persists` > `note_for_context`), then by magnitude (descending), then by `source_block_id` (ascending) for deterministic ordering.
- **`format_anomaly_summary(summaries: Iterable[AnomalySummary]) -> str`**: renders the "Anomaly flags" block per `external.md § Output format`'s "grouped for easy scanning" guidance. Format suggestion:

  ```
  === ANOMALY FLAGS ({total_count}) ===

  --- INVESTIGATE NOW ({count}) ---
  - {flag.name} | magnitude {magnitude:.2f} | source {source_block_id} | calibration {calibration_state}
  - ...

  --- INVESTIGATE IF PERSISTS ({count}) ---
  - ...

  --- NOTE FOR CONTEXT ({count}) ---
  - ...
  ```

  Empty severity sections are omitted entirely (no `(0)` headers).

- **`assemble_audience_output(audience: OutputAudience, blocks: Iterable[OutputBlock]) -> str`**: convenience entry point combining `partition_blocks`, `collect_anomalies`, `group_anomalies_by_audience`, and `format_anomaly_summary` to produce the per-audience document body — the anomaly summary at the top, followed by `format_block` (story 05) for each routed block in `block_id` order.
- Unit tests:
  - `partition_blocks` correctly distributes blocks across audiences; multi-audience blocks appear in each.
  - `collect_anomalies` extracts every flag from every block and preserves source-block context in `AnomalySummary`.
  - `group_anomalies_by_audience` puts universal anomalies in every audience's list.
  - Severity ordering: a fixture with mixed-severity flags renders with `investigate_now` flags first, then `investigate_if_persists`, then `note_for_context`.
  - Within a severity, descending-magnitude ordering applies.
  - Empty severity sections are omitted from the rendered summary.
  - Bootstrap-tagged anomalies appear in the summary with their calibration state visible.
  - `assemble_audience_output` produces byte-identical output on repeated calls (composability of story 05's deterministic formatter).
  - Empty-input case: zero blocks, zero anomalies → `assemble_audience_output` returns a non-empty string with the `=== ANOMALY FLAGS (0) ===` header (or an explicit "no flags" indicator); the format choice is up to the implementer but must be deterministic.

Out of scope:
- Per-consumer document framing (sector header / `CR` brief framing) — stories 11a / 11b own these.
- Persistence of anomaly summaries (the orchestrator may write them to the invocation archive; the data structure here is in-memory only).
- Brief-store population (the brief store holds analysis-layer briefs keyed by reference ID; distillation outputs are tagged blocks, not briefs).

## Notes

The "grouped for easy scanning" instruction in `external.md` is what motivates the severity-ordered presentation. Domain researchers and the synthesizer scan anomalies before drilling into the indicator detail; severity-first ordering puts the items most likely to drive action at the top.

The universal-broadcast handling for anomalies is subtle: a `regime_skip_emergency` anomaly attached to the `regime.label` block (audience `UNIVERSAL_BROADCAST`) must appear in every per-sector output AND in the correlation/regime brief AND in the universal context. This is what makes the universal-broadcast contract load-bearing — all-audience visibility for cross-cutting signals.

The bootstrap calibration state on anomalies is consumer-visible by design. Per `threshold-calibration.md § Downstream propagation`: "a 2.5σ volume anomaly on a `bootstrap` baseline is weaker than the same anomaly on a `calibrated` baseline." The summary line format includes the calibration state explicitly so downstream LLMs can weight conviction without parsing the underlying block.

The `assemble_audience_output` entry point is the one the orchestrator (story 12) calls per audience. Stories 11a and 11b can either call this directly or wrap it with sector / `CR` framing. Pick whichever lets 11a / 11b stay thin.

Determinism: the rendering must be byte-identical across runs for the same inputs. This is what makes the invocation archive diffable — operators reading historical archives compare regime labels and anomaly evolution across runs.

## Acceptance criteria

- [ ] `partition_blocks` distributes blocks correctly across audiences; multi-audience blocks appear in each.
- [ ] `collect_anomalies` produces a flat list of `AnomalySummary` covering every `AnomalyFlag` on every input block.
- [ ] `AnomalySummary` carries source-block context (id, audiences, freshness, calibration state).
- [ ] `group_anomalies_by_audience` correctly distributes universal-broadcast anomalies to every audience.
- [ ] Severity ordering puts `investigate_now` first, then `investigate_if_persists`, then `note_for_context`.
- [ ] Within severity, descending-magnitude ordering applies.
- [ ] Empty severity sections are omitted from rendered output.
- [ ] Calibration state is visible on every anomaly line.
- [ ] `assemble_audience_output` produces byte-identical output across repeated calls.
- [ ] Empty-input case returns a deterministic, non-empty document (zero-flag header).
- [ ] Unit tests cover all of the above with fixture blocks.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
