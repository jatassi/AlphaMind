The per-sector input bundle is \~15K tokens vs. the \~9K target in `docs/design/cost-and-rate-limit-modeling.md:128`. The distillation slice (`SectorOutput.text`) dumps verbose YAML `per_ticker:` blocks — e.g., `q1.divergence_flags` produces 58 lines for 17 financials tickers even when every row is `divergence_pairs: []` and `rsi_1d` near 50.

**Impact (2026-05-03):** 17-ticker financials sector produced 62.8 KB / 1461-line bundles that timed out the 120s SDK budget twice. Budget was temporarily bumped to 240s / 16000 ctx in `config/agents.yaml`.

**Approaches:**

**(A) Compact format** — collapse each indicator's `per_ticker:` map to one row per ticker (e.g., `AXP rsi_1d=51.42 div=[]`), \~3× whitespace savings.

**(B) Anomaly-pruned format** — drop per-ticker rows for tickers with no anomaly in that block, leaving only the block header + noteworthy entries.

**Touches:** `src/alphamind/distillation/sector_assembly.py`, per-q format helpers in `q1/output_blocks.py`, `q3_options/`, `q6/`, `q7/`. Stretch: enforce a `context_token_budget` ceiling in `src/alphamind/analysis/domain_researchers/input_bundle.py`.

**Once compressed:** revert budget bumps in `config/agents.yaml` (240→120s, 16000→8000 ctx) for the three sector researchers.