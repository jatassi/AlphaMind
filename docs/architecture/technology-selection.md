# Technology selection

Consolidated dependency list and version constraints. Every choice here traces back to a decision in the preceding architecture documents.

---

## Python runtime

| Item | Selection | Notes |
|------|-----------|-------|
| Python version | 3.13+ | Free-threaded build option, PEP 695 generic syntax, improved error messages, task groups in asyncio |
| Package manager | uv | Fast, modern, replaces pip + pip-tools + virtualenv |

---

## Core dependencies

### LLM integration

| Package | Purpose | Architectural reference |
|---------|---------|----------------------|
| `claude-agent-sdk` | Agent orchestration, tool-use loops, MCP tool registration | [LLM integration](llm-integration.md) |

Authentication via `CLAUDE_CODE_OAUTH_TOKEN` (Claude Max subscription). No `anthropic` SDK needed directly — the Agent SDK wraps it.

### Database

| Package | Purpose | Architectural reference |
|---------|---------|----------------------|
| `sqlalchemy` (2.0+) | ORM for portfolio state, Core for bulk data I/O | [Data and state](data-and-state.md) |
| `alembic` | Schema migrations | [Data and state](data-and-state.md) |

SQLite is the database engine (Python stdlib `sqlite3`). No additional DB driver needed.

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

**Note on TA-Lib:** Requires the C library `ta-lib` to be installed on the system (`brew install ta-lib` on macOS). The Python package `TA-Lib` is a thin wrapper. If TA-Lib installation proves problematic, `pandas-ta` is a pure-Python fallback with the same indicators — slower but no C dependency.

### Market calendar

| Package | Purpose | Architectural reference |
|---------|---------|----------------------|
| `exchange-calendars` | NYSE trading day detection, holiday schedules | [Infrastructure](infrastructure.md) |

Used by the scheduler to skip market holidays and adjust cadence for weekends.

---

## Market data providers

| Provider | Tier | Monthly cost | Data provided |
|----------|------|-------------|---------------|
| Polygon.io | Stocks Starter + Options Starter | $58 | OHLCV bars (all timeframes), last quote/trade, extended hours, options snapshots, ticker reference |
| Alpaca | Free | $0 | IEX trade data (order flow proxy), account-free market data, paper-trading execution |
| FRED | Free | $0 | Treasury yields, economic indicators, macro data |
| Finnhub | Free | $0 | Analyst recommendations, earnings calendar, company news |
| FINRA | Free | $0 | Short volume, ATS (dark pool) weekly data |

Additional qualitative data sources (news APIs, sentiment, prediction markets) are TBD — dependent on specific vendor selection during implementation. The data layer's adapter pattern means each source is an independent integration.

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

# Development
ruff
mypy
pytest
pytest-asyncio
pytest-cov
```

---

## What's deliberately excluded

| Not using | Why |
|-----------|-----|
| Docker / containers | Single-machine deployment, SQLite file sharing, no isolation benefit ([Infrastructure](infrastructure.md)) |
| PostgreSQL / Redis | Data volume and concurrency don't justify operational overhead ([Data and state](data-and-state.md)) |
| Celery / task queues | No distributed task execution needed ([Component boundaries](component-boundaries.md)) |
| LangChain / LangGraph | Agent SDK provides the orchestration primitives directly ([LLM integration](llm-integration.md)) |
| APScheduler v4 | Still alpha, known data integrity issues ([Infrastructure](infrastructure.md)) |
| FastAPI / web framework | No web UI or API server needed initially ([Infrastructure](infrastructure.md)) |
| Prometheus / Grafana | Over-engineered for a local paper trading system ([Infrastructure](infrastructure.md)) |
