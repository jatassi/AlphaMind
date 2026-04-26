# Open questions

- **Data source specifics:** Which exact APIs, what refresh rates, what costs?
- **Risk guardrails — specific values:** The multi-layer enforcement model and directory structure are defined in [06-risk-guardrails](06-risk-guardrails/README.md). Remaining work: specific limit values, regime-dependent parameter sets, breach behavior logic, and state delivery format (all stubbed)
- **Prompt engineering:** Detailed prompt design for each agent stage
- **Cost modeling:** Token costs per invocation across all agents, projected daily/monthly spend
- **Success criteria:** What paper trading performance justifies moving to live trading?
- **Feedback loops:** How do thesis tracking results feed back into prompt tuning?
- **Ticker refinement:** Resolved — methodology in [asset-universe-validation.md](asset-universe-validation.md); authoritative list at `config/universe.yaml`
