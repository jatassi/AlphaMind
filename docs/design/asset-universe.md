# Asset universe

US equities only, across four sectors. ~60–80 tickers selected for liquidity, analyst coverage density, news flow volume, and volatility sufficient to generate opportunities on the 4–72 hour horizon. Rich information environment per ticker required — no penny stocks, low-float names, or thinly covered companies.

The authoritative ticker list is `config/assets.yaml`. Verification procedure, re-evaluation cadence, add/remove workflow, and the [candidate discovery](asset-universe-validation.md#candidate-discovery) mechanism are in [asset-universe-validation.md](asset-universe-validation.md). This doc is descriptive — sector composition, rationale, prose criteria.

### Sector: Technology (mega-cap + growth) — ~20 names

Anchor sector. Massive analyst coverage, high news density, strong social sentiment signal, prices reacting to both company-specific and macro catalysts. Split between mega-cap names that move on macro and mid/high-growth names that move on narrative shifts.

**Mega-cap tech:**
AAPL, MSFT, GOOG/GOOGL, AMZN, META, TSLA, NFLX, ORCL

**High-growth / AI-adjacent tech:**
PLTR, NOW, CRM, SNOW, SHOP, CRWD, PANW, DDOG, NET, ZS, COIN, SQ

### Sector: Semiconductors — ~15 names

Extremely volatile, heavily covered, rich in cross-asset signals. Supply chain dynamics create predictable ripple effects — TSMC earnings preview NVDA, export controls propagate through the chain. Distinct from broader tech because of unique catalyst structures.

**Core semis:**
NVDA, AMD, AVGO, TSM, MU, INTC, QCOM, MRVL, LRCX, KLAC, AMAT, ASML, ADI, CDNS, SNPS

### Sector: Financials — ~15 names

Highly rate-sensitive, with predictable movement around FOMC, CPI, and macro releases. Clear causal chains: hot CPI → rate expectations shift → bank stocks move in a direction the system can anticipate from data it already ingests. Mix of mega-cap banks, investment banks, fintech, payment networks.

**Banks + investment banks:**
JPM, BAC, GS, MS, C, WFC, SCHW, USB

**Payments + fintech:**
V, MA, AXP, PYPL, FIS, GPN

### Sector: Energy — ~15 names

Volatile, driven by identifiable catalysts (inventory reports, OPEC decisions, geopolitical events). A higher-difficulty test case — strong vs. weak theses here validate where the information synthesis edge does and doesn't work. Mix of integrated majors, E&P, midstream, services.

**Integrated majors:**
XOM, CVX, COP, EOG

**E&P + services:**
DVN, PXD, OXY, SLB, HAL, BKR

**Midstream + LNG:**
ET, EPD, LNG, KMI, WMB

### Selection criteria

Each ticker should satisfy:
- ADV >2M shares/day or >$50M notional/day (realistic paper trade fills)
- 10+ sell-side analysts (news density, earnings estimate breadth)
- Options chain with reasonable liquidity (flow signal ingestion)
- |Beta| ≥ 0.6 (intra-window dispersion at the 4–72h horizon, regardless of correlation direction)
- Market cap > $10B (institutional coverage floor)

The universe is static during any paper trading evaluation period. Add/remove between cycles based on performance data and changing market structure.
