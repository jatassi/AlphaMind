# Data and state

SQLite (WAL mode) as the single shared database for both processes.

---

## Decision

All persistent and semi-persistent state lives in a **single SQLite database file**, accessed by both processes. WAL mode enables concurrent reads with no contention; low write frequency from each process makes write conflicts effectively nonexistent.

**SQLAlchemy** for database access, split by usage pattern: ORM models for portfolio state (rich relationships, lifecycle logic), Core/raw SQL for market data and distillation state (bulk I/O). Schema migrations via Alembic.

---

## Rationale

### Why SQLite

- **Zero operational overhead.** No server process, config, pooling, or ports. The database is a file; backup is `cp`.
- **Two-process concurrency is fine.** WAL mode allows concurrent readers with a single writer. Pipeline writes in bursts (8-10x/day); monitor writes fills intermittently. Rare overlaps resolve via brief writer retries.
- **Modest data volume.** Hundreds of MB, tens of thousands of rows in the largest tables (trailing market data) — well within SQLite's range.
- **Atomic transactions** wrap portfolio state updates (position changes, P/L recalculations, thesis transitions, cash adjustments).
- **Python ships with it.**

### Why not PostgreSQL

PostgreSQL adds a server process, connection management, and operational surface unjustified at this scale; its advantages (advanced query planning, concurrent write scaling, row-level locking, extensions) don't address any AlphaMind bottleneck.

If the system ever needs multiple machines or higher write concurrency, PostgreSQL is the natural migration — connection-string change plus minor dialect adjustments, not a rewrite.

### Why SQLAlchemy (split usage)

**ORM models for portfolio state.** Positions, theses, orders, cash, and the activity log have real relationships (order → thesis → position, bracket → parent order) and lifecycle logic (read, check state, update, log). SQLAlchemy 2.0's mapped classes give typed attributes, IDE autocomplete, and static analysis; Alembic handles migrations.

**Core / raw SQL for market data and distillation state.** Bulk I/O — load 60 days of bars for 70 tickers, write 70 rows of updated baselines. Expression builder or raw SQL is more natural and efficient.

**Dataclasses are a separate concern.** The Python dataclasses in the schema package are *LLM presentation models* — denormalized, token-optimized payloads serialized into agent context windows. A thin mapping layer converts between ORM models / query results and what agents consume; the presentation layer is a future design topic.

---

## Four state categories

Logically distinct lifecycles, all in the same SQLite database.

### 1. Portfolio state (persistent, authoritative)

The system's memory across invocations — positions, theses, orders, cash, P/L, activity log, historical resolutions. Defined by the execution layer spec ([categories 1-6](../design/01-data-layer/internal/portfolio-state.md)).

**Writers:** Pipeline (Phase 1: state updates from fills; Phase 2: new orders, thesis creation). Continuous monitor writes fills to the fill buffer, not portfolio state directly.
**Readers:** Pipeline (data layer at invocation start; guardrails; distillation for internal metrics). Continuous monitor reads pending orders.

**Key tables (conceptual):**
- `positions` — open and closed positions with instrument details, entry/exit data
- `theses` — active and resolved theses with structured components, bracket coverage
- `orders` — all orders with status, bracket relationships, linked thesis
- `cash_ledger` — cash balance and transaction history
- `activity_log` — every state-changing event with timestamps
- `thesis_resolutions` — historical thesis outcomes for feedback loop

### 2. Fill buffer (ephemeral, accumulating)

Fills produced by the continuous monitor between invocations. Rows accumulate (minutes to hours); Phase 1 reads them, marks them processed, and retains them for audit.

**Writers:** Continuous monitor (one fill per trigger condition met).
**Readers:** Pipeline (Phase 1: reads unprocessed fills, marks them processed).

**Key tables:**
- `fill_reports` — fills with order ID, timestamp, price, quantity, slippage, fees, processed flag

### 3. Brief store (per-invocation, read-heavy)

Analysis briefs keyed by reference ID — built during analysis, consumed during decision, retained for audit.

**Writers:** Pipeline (analysis writes briefs as each agent completes).
**Readers:** Pipeline (decision layer retrieval tool fetches by reference ID).

**Key tables:**
- `briefs` — invocation ID, reference prefix (SA-TECH, QR, AR, etc.), reference index, content, created timestamp
- Retrieval: `SELECT content FROM briefs WHERE invocation_id = ? AND ref_id = ?`

### 4. Distillation state (rolling, persistent)

Rolling baselines, regime classifications, composite signals. Each invocation reads current state, computes new values, writes updates. Historical values may be retained for trend detection (e.g., trailing 20-day baselines).

**Writers:** Pipeline (distillation layer, after computation).
**Readers:** Pipeline (distillation at start of computation; analysis reads regime classification).

**Key tables:**
- `rolling_baselines` — per-ticker trailing averages (volume, ATR, spread, etc.)
- `composites` — funding stress, expectations scorecards, etc.
- `regime_state` — volatility regime classification, correlation regime, etc.

---

## Market data storage

Raw and historical market data (OHLCV bars, options, macro series) also lives in SQLite. Distillation needs trailing 20-60 day windows for baselines and correlation matrices.

**Volume:** 70 tickers × 5 timeframes × 60 days × ~200 bytes/bar ≈ 4 MB for price data; with order flow, options, and macro, 50-100 MB total.

**Retention:** Data older than the longest trailing window (60-90 days) can be pruned or archived; active queries never look back further.

**Key tables:**
- `ohlcv_bars` — ticker, timeframe, timestamp, OHLCV fields — the foundational table
- Per-category tables for order flow snapshots, options data, macro readings
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

- **WAL mode** enabled at database creation; both processes open the same file.
- Pipeline is the dominant writer (per-phase transactions). Monitor writes are infrequent and small (single-row fill inserts).
- WAL: reads never block writes or each other. Write-write contention effectively zero; rare overlaps resolve via SQLite's busy timeout.
- **Busy timeout** set to 5-10 seconds on both connections as a safety net.

---

## Configuration

```
journal_mode = WAL
busy_timeout = 5000      -- 5 seconds
foreign_keys = ON
synchronous = NORMAL     -- good balance of durability and performance for WAL mode
```
