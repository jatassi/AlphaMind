# Where production evidence lives, and how to query it

Used by `refine-issue`: **bug mode** builds the root-cause evidence chain (`bug-mode.md`);
**spec mode** validates the ground-truth data assumptions a design depends on (`spec-mode.md`).
Either way, every load-bearing claim is confirmed against one of these **primary** sources —
never against the issue's own narrative.

Per CLAUDE.md: **macOS = dev (read-only), Windows = prod.** You read prod; you never write
it. Scripts that hit vendor APIs need `.env` loaded (`source .env`) — prod doesn't
auto-source.

## Production DB (read-only over SMB)

`/Volumes/Users/jacks/AlphaMind/data/alphamind.db` — WAL-mode SQLite. Open `immutable=1`
so the dev Mac never blocks a prod writer:

```python
import sqlite3, json
db = '/Volumes/Users/jacks/AlphaMind/data/alphamind.db'
con = sqlite3.connect(f'file:{db}?mode=ro&immutable=1', uri=True)
con.row_factory = sqlite3.Row
cur = con.cursor()
# orient: SELECT name FROM sqlite_master WHERE type='table';  then PRAGMA table_info(<t>)
```

**Caveats:**

- `immutable=1` reads are frozen at the **last checkpoint** — you cannot see the live WAL.
  If a fact depends on the very latest state, note that the read is a checkpoint snapshot,
  or have the operator run the query `mode=ro` on the prod box.
- Logs are **Mountain Time**; IDs and DB timestamps are **UTC** — never compare them
  without converting.
- Map an invocation to the exact code that ran via `process_lifetimes.git_sha`.

Tables that carry the evidence for execution / persistence bugs: `positions`, `orders`,
`unattributed_fills`, `execution_history`, `activity_log`, `cash_ledger`,
`command_execution_summary` (inspect `commands_submitted` and the `*_completed_at`
stamps), `invocations` (`trigger_reason`). Pull the full row including
`details_json` / `raw_json` when the column set isn't enough.

## Live Alpaca (the broker's own record)

The broker is the irrefutable record of what *actually* happened to an order — use it to
confirm/refute "the fill was captured", "the position is flat", "this id is real":

```bash
set -a; source .env; set +a; uv run python -c "
from alpaca.trading.client import TradingClient
import os
c = TradingClient(os.environ['ALPACA_PAPER_KEY'], os.environ['ALPACA_PAPER_SECRET'], paper=True)
o = c.get_order_by_id('<alpaca_order_id>')
print(o.symbol, o.side, o.qty, o.filled_qty, o.filled_avg_price, o.status,
      o.order_class, o.client_order_id, o.position_intent, o.submitted_at, o.filled_at)
# c.get_open_position('<SYMBOL>')  -> raises if flat (the raise IS evidence)
# GetAccountActivitiesRequest -> the Activities ledger the operator sees in the UI
"
```

When the operator points at a concrete artifact from the Alpaca UI (e.g. the **Activities**
page showing exact proceeds), chase it down to the order/fill in the API and reconcile it
against the DB. A discrepancy between Alpaca and the DB is usually the heart of the bug.

## Archived invocation artifacts

Prod run artifacts live under `archive/<date>/inv-*` (**not** `.archive`). The PM
submission log (`decision/portfolio_manager/submission_log.json`) shows what each command
did at the broker seam (`status`, `rule`, the exact `last_error` / `attempts`). The
`run_type`, `trigger_reason`, and phase stamps situate the failure in the pipeline.

## The installed library, the code, and git history

- **Library source** — when the failure is in a vendor SDK, read the *installed* code and
  confirm the raise (e.g. confirm exactly where and why a client method rejects an input,
  before vs after any HTTP call). Don't assume library behavior; check it.
- **Code at file:line** — trace the actual path the data took. Every claim in the root
  cause cites a file:line you've read.
- **git log** — `git log --oneline -- <path>` and the related-issue history establish
  *which fix shipped when*. **Timing corroboration** is part of the chain: line up the
  failure timestamp against when a related fix merged to place the failure inside (or
  outside) the window that fix covered.

For noisy fan-out (grepping many tables/files/logs), dispatch a subagent and keep only the
confirmed findings — but run the load-bearing confirm/refute queries yourself, so you can
vouch for them.
