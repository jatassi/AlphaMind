# Data and state

SQLite (WAL mode) as the single shared database for both processes.

---

## Decision

All persistent and semi-persistent state lives in a **single SQLite database file**, accessed by both the pipeline process and the continuous monitor. WAL (write-ahead logging) mode enables concurrent reads from both processes with no contention, and the low write frequency from each process means write conflicts are effectively nonexistent.

**SQLAlchemy** for database access, split by usage pattern: ORM models for portfolio state (rich relationships, lifecycle logic), Core/raw SQL for market data and distillation state (bulk I/O). Schema migrations via Alembic.

---

## Rationale

### Why SQLite

- **Zero operational overhead.** No server process, no configuration, no connection pooling, no port management. The database is a file. Backup is `cp`.
- **Two-process concurrency is fine.** WAL mode allows concurrent readers with a single writer. The pipeline writes in bursts (8-10x/day); the monitor writes fills intermittently. These rarely overlap, and when they do, WAL handles it with brief writer retries — not a problem at this write frequency.
- **Data volume is modest.** Hundreds of MB, not GB. Tens of thousands of rows in the largest tables (trailing market data), not millions. SQLite handles this without breaking a sweat.
- **Atomic transactions.** Portfolio state updates (position changes, P/L recalculations, thesis status transitions, cash adjustments) that must be consistent can be wrapped in a single transaction.
- **Python ships with it.** No external dependencies for the core data layer.

### Why not PostgreSQL

PostgreSQL would work but adds a server process, connection management, and operational surface area that isn't justified at this scale. The system runs on one machine with two processes and modest data. PostgreSQL's advantages — advanced query planning, concurrent write scaling, row-level locking, extensions — don't address any bottleneck AlphaMind has.

If the system ever needs to run across multiple machines or handle significantly higher write concurrency, PostgreSQL is the natural migration target. The SQL is standard enough that migration would be a connection-string change plus minor dialect adjustments, not a rewrite.

### Why SQLAlchemy (split usage)

The system has two distinct data access patterns that call for different approaches:

**ORM models for portfolio state.** Positions, theses, orders, cash, and the activity log have real relationships (order → thesis → position, bracket → parent order) and lifecycle logic (the OMS reads a position, checks state, updates fields, writes an activity log entry). This is exactly the object-graph manipulation ORMs are built for. SQLAlchemy 2.0's mapped classes provide typed attributes, IDE autocomplete, and static analysis — valuable for a complex domain model. Alembic provides schema migrations as the model evolves.

**Core / raw SQL for market data and distillation state.** These are bulk I/O patterns: load 60 days of bars for 70 tickers, write 70 rows of updated baselines. The ORM's unit-of-work overhead adds nothing here. SQLAlchemy Core's expression builder or raw SQL is more natural and efficient for batch operations.

**The existing dataclasses are a separate concern.** The Python dataclasses in the schema package are *LLM presentation models* — they define the denormalized, token-optimized payloads that get serialized into agent context windows. They are not storage models and don't need to change. A thin mapping layer converts between ORM models / query results and the dataclasses that agents consume. This separation is deliberate: the shape that's efficient for database storage (normalized, relational) is different from the shape that's efficient for LLM consumption (denormalized, context-optimized). The presentation layer is a future design topic — there is significant room for token optimization in how distillation output is packaged for agents.

---

## Four state categories

The design spec identifies four categories of state with different lifecycles. All live in the same SQLite database but are logically distinct.

### 1. Portfolio state (persistent, authoritative)

The system's memory. Positions, theses, orders, cash, P/L, activity log, historical resolutions. Defined by the execution layer spec ([categories 1-6](../design/01-data-layer/internal/portfolio-state.md)).

**Lifecycle:** Survives indefinitely across invocations. Grows over the system's lifetime. The authoritative record of everything the system has done.

**Writers:** Pipeline process (Phase 1: portfolio state updates from fills; Phase 2: new order submissions, thesis creation). The continuous monitor never writes portfolio state directly — it writes fills to the fill buffer, and the pipeline processes them into portfolio state.

**Readers:** Pipeline process (data layer reads at invocation start; guardrail checks throughout; distillation layer reads for internal metrics). The continuous monitor reads pending orders to know what to watch.

**Key tables (conceptual):**
- `positions` — open and closed positions with instrument details, entry/exit data
- `theses` — active and resolved theses with structured components, bracket coverage
- `orders` — all orders with status, bracket relationships, linked thesis
- `cash_ledger` — cash balance and transaction history
- `activity_log` — every state-changing event with timestamps
- `thesis_resolutions` — historical thesis outcomes for feedback loop

### 2. Fill buffer (ephemeral, accumulating)

Fills produced by the continuous monitor between pipeline invocations. Drained at each pipeline invocation's Phase 1.

**Lifecycle:** Rows accumulate between invocations (minutes to hours), then are read and marked as processed. Not truly ephemeral — fills should be retained for audit even after processing — but the "active" buffer is small.

**Writers:** Continuous monitor (writes a fill whenever a trigger condition is met).

**Readers:** Pipeline process (Phase 1: reads all unprocessed fills, marks them processed).

**Key tables:**
- `fill_reports` — fills with order ID, timestamp, price, quantity, slippage, fees, processed flag

### 3. Brief store (per-invocation, read-heavy)

Analysis briefs keyed by reference ID for retrieval by decision layer agents. Built during the analysis layer, consumed during the decision layer, no longer needed after the invocation completes.

**Lifecycle:** Created and consumed within a single pipeline invocation. Retained for audit/debugging but not actively queried after the invocation ends.

**Writers:** Pipeline process (analysis layer writes briefs as each agent completes).

**Readers:** Pipeline process (decision layer retrieval tool fetches briefs by reference ID).

**Key tables:**
- `briefs` — invocation ID, reference prefix (SA-TECH, QR, AR, etc.), reference index, content, created timestamp
- The retrieval tool queries: `SELECT content FROM briefs WHERE invocation_id = ? AND ref_id = ?`

### 4. Distillation state (rolling, persistent)

Rolling baselines, regime classifications, composite signals that persist across invocations. Updated each invocation, read at the start of the next.

**Lifecycle:** Persistent but constantly updated. Each invocation reads the current state, computes new values, and writes updates. Historical values may be retained for trend detection (e.g., trailing 20-day baselines).

**Writers:** Pipeline process (distillation layer updates after computation).

**Readers:** Pipeline process (distillation layer reads at start of computation; analysis layer reads regime classification).

**Key tables:**
- `rolling_baselines` — per-ticker trailing averages (volume, ATR, spread, etc.)
- `composites` — funding stress, expectations scorecards, etc.
- `regime_state` — volatility regime classification, correlation regime, etc.

---

## Market data storage

Raw and historical market data (OHLCV bars, options data, macro series) also lives in SQLite. The distillation layer needs trailing windows (20-60 days) of historical data for baseline computation and correlation matrices.

**Volume estimate:** 70 tickers × 5 timeframes × 60 days × ~200 bytes per bar ≈ 4 MB for price data alone. Adding other categories (order flow, options, macro) might bring this to 50-100 MB total. Well within SQLite's range.

**Retention:** Market data older than the longest trailing window (60-90 days) can be pruned or archived. Active queries never need to look back further.

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
- Pipeline is the dominant writer. All pipeline writes happen in transactions per-phase.
- Monitor writes are infrequent (fills) and small (single row inserts).
- Read-read concurrency: unlimited in WAL mode.
- Read-write concurrency: readers never block writers and vice versa in WAL mode.
- Write-write contention: effectively zero — the pipeline and monitor rarely write simultaneously, and when they do, SQLite's default busy timeout (configurable) handles the brief wait.
- **Busy timeout** set to 5-10 seconds on both connections as a safety net.

---

## Configuration

```
journal_mode = WAL
busy_timeout = 5000      -- 5 seconds
foreign_keys = ON
synchronous = NORMAL     -- good balance of durability and performance for WAL mode
```
