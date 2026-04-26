# Asset universe

US equities only, across four sectors. Approximately 60–80 tickers total, selected for liquidity, analyst coverage density, news flow volume, and sufficient volatility to generate opportunities on the 4–72 hour time horizon. No penny stocks, low-float names, or thinly covered companies — the system needs a rich information environment per ticker.

The authoritative machine-readable ticker list lives at `config/assets.yaml`. The procedure for verifying a ticker against the criteria below, the operator-driven re-evaluation cadence, the add/remove workflow, and the [candidate discovery](asset-universe-validation.md#candidate-discovery) mechanism (which surfaces names outside the universe that would qualify if added) are specified in [asset-universe-validation.md](asset-universe-validation.md). This doc carries the descriptive view — sector composition, rationale per sector, criteria as prose.

### Sector: Technology (mega-cap + growth) — ~20 names

The anchor sector. Massive analyst coverage, high news density, strong social sentiment signal, and prices that react meaningfully to both company-specific and macro catalysts. Split between mega-cap names that move primarily on macro and mid/high-growth names that move on narrative shifts.

**Mega-cap tech:**
AAPL, MSFT, GOOG/GOOGL, AMZN, META, TSLA, NFLX, ORCL

**High-growth / AI-adjacent tech:**
PLTR, NOW, CRM, SNOW, SHOP, CRWD, PANW, DDOG, NET, ZS, COIN, SQ

### Sector: Semiconductors — ~15 names

Extremely volatile, heavily covered, and rich in cross-asset signals. Supply chain dynamics create predictable ripple effects — TSMC earnings preview NVDA, export controls propagate through the chain. Treated as a distinct sector from broader tech due to unique catalyst structures.

**Core semis:**
NVDA, AMD, AVGO, TSM, MU, INTC, QCOM, MRVL, LRCX, KLAC, AMAT, ASML, ADI, CDNS, SNPS

### Sector: Financials — ~15 names

Highly rate-sensitive, creating predictable movement around FOMC, CPI, and macro data releases. Clear causal chains the system can exploit: hot CPI → rate expectations shift → bank stocks move in a direction the system can anticipate from data it's already ingesting. Mix of mega-cap banks, investment banks, fintech, and payment networks.

**Banks + investment banks:**
JPM, BAC, GS, MS, C, WFC, SCHW, USB

**Payments + fintech:**
V, MA, AXP, PYPL, FIS, GPN

### Sector: Energy — ~15 names

Volatile and driven by identifiable catalysts (inventory reports, OPEC decisions, geopolitical events). Included as a higher-difficulty test case — if the system generates strong theses in tech/semis/financials but weak ones in energy, that validates where the information synthesis edge does and doesn't work. Mix of integrated majors, E&P, midstream, and services.

**Integrated majors:**
XOM, CVX, COP, EOG

**E&P + services:**
DVN, PXD, OXY, SLB, HAL, BKR

**Midstream + LNG:**
ET, EPD, LNG, KMI, WMB

### Selection criteria

Each ticker in the universe should satisfy:
- Average daily volume sufficient for realistic paper trade fills (>2M shares/day or >$50M notional/day)
- Covered by 10+ sell-side analysts (ensures news density and earnings estimate data)
- Options chain with reasonable liquidity (enables flow signal ingestion)
- |Beta| ≥ 0.6 (sufficient intra-window dispersion at the 4–72h horizon, regardless of correlation direction)
- Market cap > $10B (institutional coverage floor)

The universe is static during any given paper trading evaluation period. Tickers may be added or removed between evaluation cycles based on performance data and changing market structure.
