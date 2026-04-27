# Technology selection

Consolidated dependency list and version constraints. Each choice traces to a preceding architecture document.

---

## Python runtime

| Item | Selection | Notes |
|------|-----------|-------|
| Python version | 3.13+ | Free-threaded build option, PEP 695 generics, improved error messages, asyncio task groups |
| Package manager | uv | Replaces pip + pip-tools + virtualenv |

---

## Core dependencies

### LLM integration

| Package | Purpose | Architectural reference |
|---------|---------|----------------------|
| `claude-agent-sdk` | Agent orchestration, tool-use loops, MCP tool registration | [LLM integration](llm-integration.md) |

Authentication via `CLAUDE_CODE_OAUTH_TOKEN` (Claude Max subscription). The Agent SDK wraps `anthropic`; no direct dependency.

### Database

| Package | Purpose | Architectural reference |
|---------|---------|----------------------|
| `sqlalchemy` (2.0+) | ORM for portfolio state, Core for bulk data I/O | [Data and state](data-and-state.md) |
| `alembic` | Schema migrations | [Data and state](data-and-state.md) |

SQLite is the database engine (Python stdlib `sqlite3`); no additional driver.

### Configuration

| Package | Purpose | Architectural reference |
|---------|---------|----------------------|
| `pyyaml` | YAML loader for the `config/` tree | [Configuration management](../design/configuration-management.md) |
| `pydantic` (2.0+) | Typed config models, parse-time validation | [Configuration management § Validation](../design/configuration-management.md#validation) |
| `python-dotenv` | `.env` loader for secrets referenced by env-var name from YAML | [Configuration management § Principles](../design/configuration-management.md#principles) |

### Scheduling

| Package | Purpose | Architectural reference |
|---------|---------|----------------------|
| `apscheduler` (3.x, latest 3.11.2) | In-process pipeline scheduling with CronTrigger | [Infrastructure](infrastructure.md) |

### Async HTTP

| Package | Purpose | Architectural reference |
|---------|---------|----------------------|
| `httpx` | Async HTTP client for external API calls (data collection layer) | [System characterization](system-characterization.md) |
| `websockets` | Websocket client for real-time price feed (continuous monitor) | [Component boundaries](component-boundaries.md) |

### Numerical computation

| Package | Purpose | Architectural reference |
|---------|---------|----------------------|
| `numpy` | Array operations, statistical computations | [System characterization](system-characterization.md) |
| `pandas` | Time-series manipulation, trailing window operations | [System characterization](system-characterization.md) |
| `ta-lib` (via `TA-Lib` Python wrapper) | Technical indicators (RSI, MACD, Bollinger, etc.) | [System characterization](system-characterization.md) |

**Note on TA-Lib:** Requires the C library `ta-lib` (`brew install ta-lib` on macOS); the Python package is a thin wrapper. `pandas-ta` is a pure-Python fallback — slower, no C dependency.

### Market calendar

| Package | Purpose | Architectural reference |
|---------|---------|----------------------|
| `exchange-calendars` | NYSE trading day detection, holiday schedules | [Infrastructure](infrastructure.md) |

Used by the scheduler to skip holidays and adjust weekend cadence.

### Command center backend

| Package | Purpose | Architectural reference |
|---------|---------|----------------------|
| `fastapi` | Async web framework — public-facing surface plus per-process loopback control surfaces | [Command center § Tech stack](../design/command-center.md#tech-stack) |
| `uvicorn[standard]` | ASGI server hosting FastAPI in each of pipeline, monitor, command center | [Command center § Tech stack](../design/command-center.md#tech-stack) |
| `aiosqlite` | Async SQLite driver paired with SQLAlchemy 2.0's async session | [Command center § Backend](../design/command-center.md#backend) |
| `webauthn` (`py_webauthn`) | WebAuthn relying-party logic for browser authentication | [Command center § Backend](../design/command-center.md#backend) |

Frontend: TypeScript / React / Vite / shadcn/ui — BaseUI variants / Tailwind / TanStack (Router, Query, Table) / React Hook Form + Zod / Recharts / `openapi-typescript`. Node LTS for the toolchain. Frontend-package detail in [command-center.md § Frontend](../design/command-center.md#frontend); the FastAPI-emitted OpenAPI schema is the single source of truth for shared types.

---

## Market data providers

### Quantitative

| Provider | Tier | Monthly cost | Data provided |
|----------|------|-------------|---------------|
| Polygon.io | Stocks Starter + Options Starter | $58 | OHLCV bars (all timeframes), last quote/trade, extended hours, options snapshots, ticker reference, `/v2/reference/news` |
| Alpaca | Free | $0 | IEX trade data (order flow proxy), account-free market data, paper-trading execution |
| FRED | Free | $0 | Treasury yields, economic indicators, macro data |
| Finnhub | Free | $0 | Analyst recommendations, earnings calendar, company news |
| FINRA | Free | $0 | Short volume, ATS (dark pool) weekly data |

### Qualitative

| Category | Provider(s) | Monthly cost | Data provided |
|----------|-------------|-------------|---------------|
| Qual1 — News & sentiment | Marketaux + Finnhub + Polygon `/v2/reference/news` | $0 (Marketaux + Finnhub free; Polygon news bundled in Stocks Starter above) | Per-article metadata, vendor sentiment scores, ticker entity tags. Three independent streams overlaid by the credibility-tier model in [news_sentiment.yaml](../design/01-data-layer/mappings/news_sentiment.yaml) |
| Qual2 — Social sentiment | Deferred | — | No viable free-tier vendor; StockTwits API registration closed and Finnhub social-sentiment endpoint returns 403 on free tier. The qualitative researcher's `social_sentiment` tool reports unavailable; per [api-failure-handling.md § Criticality tiers](../design/01-data-layer/api-failure-handling.md), Qual2 is Optional and the absence is a budgeted outcome |
| Qual3 — Prediction markets | Kalshi + Polymarket | $0 (public read APIs) | Contract probabilities, liquidity, bid/ask trajectories. Categories per [qualitative.md § 3](../design/01-data-layer/external/qualitative.md): macro/policy (Kalshi) and election/event (Polymarket) |

The data layer's adapter pattern keeps each source an independent integration.

---

## Development tools

| Tool | Purpose |
|------|---------|
| `uv` | Package management, virtual environments |
| `ruff` | Linting and formatting (replaces flake8 + black + isort) |
| `mypy` | Static type checking |
| `pytest` | Testing |
| `pytest-asyncio` | Async test support |

---

## Full dependency summary

```
# Core
claude-agent-sdk
sqlalchemy>=2.0
alembic
apscheduler>=3.11,<4
httpx
websockets

# Configuration
pyyaml
pydantic>=2.0
python-dotenv

# Numerical
numpy
pandas
scipy                 # Black-Scholes / greeks per guardrail-evaluation.md
TA-Lib                # requires system ta-lib library

# Market calendar
exchange-calendars

# Market data clients
polygon-api-client    # Polygon.io
alpaca-py             # Alpaca
finnhub-python        # Finnhub
fredapi               # FRED

# Command center backend
fastapi
uvicorn[standard]
aiosqlite
webauthn              # py_webauthn on PyPI as `webauthn`

# Development
ruff
mypy
pytest
pytest-asyncio
pytest-cov
```

Manifests pin minimum-version floors only; reproducibility comes from `uv.lock` (Python) and `package-lock.json` (frontend). Cross-platform validation runs in CI on a Windows + macOS matrix. See [command-center.md § Versioning policy](../design/command-center.md#versioning-policy) for detail.

---

## What's deliberately excluded

| Not using | Why |
|-----------|-----|
| Docker / containers | Single-machine deployment, SQLite file sharing, no isolation benefit ([Infrastructure](infrastructure.md)) |
| PostgreSQL / Redis | Data volume and concurrency don't justify operational overhead ([Data and state](data-and-state.md)) |
| Celery / task queues | No distributed task execution needed ([Component boundaries](component-boundaries.md)) |
| LangChain / LangGraph | Agent SDK provides the orchestration primitives directly ([LLM integration](llm-integration.md)) |
| APScheduler v4 | Still alpha, known data integrity issues ([Infrastructure](infrastructure.md)) |
| Prometheus / Grafana | Over-engineered for a local paper trading system ([Infrastructure](infrastructure.md)) |
