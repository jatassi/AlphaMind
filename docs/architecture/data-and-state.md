# Data and state

SQLite (WAL mode) as the single shared database for both processes.

---

## Decision

All persistent and semi-persistent state lives in a **single SQLite database file**, accessed by both the pipeline process and the continuous monitor. WAL (write-ahead logging) mode enables concurrent reads with no contention; the low write frequency from each process makes write conflicts effectively nonexistent.

**SQLAlchemy** for database access, split by usage pattern: ORM models for portfolio state (rich relationships, lifecycle logic), Core/raw SQL for market data and distillation state (bulk I/O). Schema migrations via Alembic.

---

## Rationale

### Why SQLite

- **Zero operational overhead.** No server process, configuration, connection pooling, or port management. The database is a file; backup is `cp`.
- **Two-process concurrency is fine.** WAL mode allows concurrent readers with a single writer. The pipeline writes in bursts (8-10x/day); the monitor writes fills intermittently. Rare overlaps resolve via brief writer retries.
- **Data volume is modest.** Hundreds of MB, tens of thousands of rows in the largest tables (trailing market data). Well within SQLite's range.
- **Atomic transactions.** Portfolio state updates (position changes, P/L recalculations, thesis status transitions, cash adjustments) wrap in a single transaction.
- **Python ships with it.** No external dependencies for the core data layer.

### Why not PostgreSQL

PostgreSQL would work but adds a server process, connection management, and operational surface area unjustified at this scale. PostgreSQL's advantages — advanced query planning, concurrent write scaling, row-level locking, extensions — don't address any AlphaMind bottleneck.

If the system ever needs multiple machines or significantly higher write concurrency, PostgreSQL is the natural migration target. The SQL is standard enough that migration is a connection-string change plus minor dialect adjustments, not a rewrite.

### Why SQLAlchemy (split usage)

Two distinct data access patterns call for different approaches:

**ORM models for portfolio state.** Positions, theses, orders, cash, and the activity log have real relationships (order → thesis → position, bracket → parent order) and lifecycle logic (the OMS reads a position, checks state, updates fields, writes an activity log entry). This is the object-graph manipulation ORMs are built for. SQLAlchemy 2.0's mapped classes provide typed attributes, IDE autocomplete, and static analysis. Alembic provides schema migrations.

**Core / raw SQL for market data and distillation state.** Bulk I/O patterns: load 60 days of bars for 70 tickers, write 70 rows of updated baselines. SQLAlchemy Core's expression builder or raw SQL is more natural and efficient for batch operations.

**The existing dataclasses are a separate concern.** The Python dataclasses in the schema package are *LLM presentation models* — denormalized, token-optimized payloads serialized into agent context windows. A thin mapping layer converts between ORM models / query results and the dataclasses that agents consume. The shape efficient for storage (normalized, relational) differs from the shape efficient for LLM consumption (denormalized, context-optimized). The presentation layer is a future design topic.

---

## Four state categories

Four categories of state with different lifecycles, all in the same SQLite database but logically distinct.

### 1. Portfolio state (persistent, authoritative)

The system's memory. Positions, theses, orders, cash, P/L, activity log, historical resolutions. Defined by the execution layer spec ([categories 1-6](../design/01-data-layer/internal/portfolio-state.md)).

**Lifecycle:** Survives indefinitely across invocations. The authoritative record of everything the system has done.

**Writers:** Pipeline process (Phase 1: portfolio state updates from fills; Phase 2: new order submissions, thesis creation). The continuous monitor writes fills to the fill buffer; the pipeline processes them into portfolio state.

**Readers:** Pipeline process (data layer reads at invocation start; guardrail checks throughout; distillation layer reads for internal metrics). Continuous monitor reads pending orders to know what to watch.

**Key tables (conceptual):**
- `positions` — open and closed positions with instrument details, entry/exit data
- `theses` — active and resolved theses with structured components, bracket coverage
- `orders` — all orders with status, bracket relationships, linked thesis
- `cash_ledger` — cash balance and transaction history
- `activity_log` — every state-changing event with timestamps
- `thesis_resolutions` — historical thesis outcomes for feedback loop

### 2. Fill buffer (ephemeral, accumulating)

Fills produced by the continuous monitor between pipeline invocations. Drained at each pipeline invocation's Phase 1.

**Lifecycle:** Rows accumulate between invocations (minutes to hours), then are read and marked as processed. Fills are retained for audit after processing; the "active" buffer is small.

**Writers:** Continuous monitor (one fill per trigger condition met).

**Readers:** Pipeline process (Phase 1: reads all unprocessed fills, marks them processed).

**Key tables:**
- `fill_reports` — fills with order ID, timestamp, price, quantity, slippage, fees, processed flag

### 3. Brief store (per-invocation, read-heavy)

Analysis briefs keyed by reference ID for retrieval by decision layer agents. Built during analysis, consumed during decision.

**Lifecycle:** Created and consumed within a single pipeline invocation. Retained for audit/debugging.

**Writers:** Pipeline process (analysis layer writes briefs as each agent completes).

**Readers:** Pipeline process (decision layer retrieval tool fetches briefs by reference ID).

**Key tables:**
- `briefs` — invocation ID, reference prefix (SA-TECH, QR, AR, etc.), reference index, content, created timestamp
- The retrieval tool queries: `SELECT content FROM briefs WHERE invocation_id = ? AND ref_id = ?`

### 4. Distillation state (rolling, persistent)

Rolling baselines, regime classifications, composite signals persisting across invocations.

**Lifecycle:** Each invocation reads the current state, computes new values, writes updates. Historical values may be retained for trend detection (e.g., trailing 20-day baselines).

**Writers:** Pipeline process (distillation layer updates after computation).

**Readers:** Pipeline process (distillation layer at start of computation; analysis layer reads regime classification).

**Key tables:**
- `rolling_baselines` — per-ticker trailing averages (volume, ATR, spread, etc.)
- `composites` — funding stress, expectations scorecards, etc.
- `regime_state` — volatility regime classification, correlation regime, etc.

---

## Market data storage

Raw and historical market data (OHLCV bars, options data, macro series) also lives in SQLite. The distillation layer needs trailing windows (20-60 days) for baseline computation and correlation matrices.

**Volume estimate:** 70 tickers × 5 timeframes × 60 days × ~200 bytes per bar ≈ 4 MB for price data; with other categories (order flow, options, macro), 50-100 MB total.

**Retention:** Data older than the longest trailing window (60-90 days) can be pruned or archived. Active queries never look back further.

**Key tables:**
- `ohlcv_bars` — ticker, timeframe, timestamp, OHLCV fields — the foundational table
- Per-category tables for order flow snapshots, options data, macro readings, etc.
- Indexed on (ticker, timeframe, timestamp) for efficient trailing window queries

---

## Access patterns summary

| Process | Phase | Operation | Frequency |
|---------|-------|-----------|-----------|
| Pipeline | Phase 1 (collect) | Read unprocessed fills | 8-10x/day |
| Pipeline | Phase 1 (collect) | Write portfolio state updates | 8-10x/day |
| Pipeline | Data collection | Write raw market data | 8-10x/day |
| Pipeline | Distillation | Read trailing market data windows | 8-10x/day |
| Pipeline | Distillation | Read/write rolling baselines and composites | 8-10x/day |
| Pipeline | Analysis | Write briefs to brief store | 8-10x/day |
| Pipeline | Decision | Read briefs by reference ID | 8-10x/day, multiple reads per invocation |
| Pipeline | Phase 2 (execute) | Write new orders | 8-10x/day |
| Monitor | Continuous | Read pending orders (on new order arrival) | 8-10x/day |
| Monitor | Continuous | Read trigger prices from orders | Continuous (in-memory after initial load) |
| Monitor | Continuous | Write fill reports | Sporadic (when triggers fire) |

---

## Concurrency model

- **WAL mode** enabled at database creation. Both processes open the same file.
- Pipeline is the dominant writer; all pipeline writes happen in transactions per-phase.
- Monitor writes are infrequent (fills) and small (single row inserts).
- Read-read: unlimited in WAL mode.
- Read-write: readers never block writers and vice versa in WAL mode.
- Write-write contention: effectively zero; rare overlaps resolve via SQLite's busy timeout.
- **Busy timeout** set to 5-10 seconds on both connections as a safety net.

---

## Configuration

```
journal_mode = WAL
busy_timeout = 5000      -- 5 seconds
foreign_keys = ON
synchronous = NORMAL     -- good balance of durability and performance for WAL mode
```
