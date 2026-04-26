# Broker adapter — Alpaca

The narrow layer between the OMS and Alpaca. Translates validated OMS commands into Alpaca REST calls, subscribes to Alpaca's `trade_updates` websocket, and pushes fill events into the fill buffer. The adapter does not contain business logic — validation and risk enforcement happen in the guardrail layer before a command reaches the adapter, and position/thesis state lives in the OMS.

This document specifies what the OMS can assume about Alpaca's behavior. It is not a generic "broker interface" — AlphaMind is committed to Alpaca, and this document describes Alpaca's actual surface area. A future migration to a different broker would require rewriting this document and the adapter implementation it describes; it would not require rewriting the OMS.

---

## Environment selection

A single configuration flag selects the environment by swapping the base URL:

- **Paper:** `https://paper-api.alpaca.markets` (REST), `wss://paper-api.alpaca.markets/stream` (websocket)
- **Live:** `https://api.alpaca.markets` (REST), `wss://api.alpaca.markets/stream` (websocket)

The API credentials differ per environment. All environment-specific knowledge lives in the adapter's configuration; the OMS is environment-unaware. Paper mode additionally engages the [paper-evaluation harness](paper-evaluation-harness.md) for live-execution estimation; live mode does not.

---

## Supported instruments

| Instrument | Supported | Notes |
|-----------|-----------|-------|
| US equities (long) | Yes | Commission-free on the standard retail Trading API |
| US equities (short) | Yes, ETB only | Alpaca does not accept new short opens on non-ETB names. Borrow fees are $0 on ETB. An existing short whose underlying drifts ETB→HTB overnight is auto-closed by Alpaca before the next open |
| US options (single-leg) | Yes | `order_class: simple`. Level 1 (covered calls, cash-secured puts) and Level 2 (long calls/puts) |
| US options (multi-leg) | Yes | `order_class: mleg`, up to **4 legs per strategy**. Level 3 approval required. All legs must be covered within the same mleg order; no equity + options combinations in a single order |
| Crypto spot | Yes | `order_class: simple` only |

AlphaMind's asset universe ([asset-universe.md](../asset-universe.md)) is US equities only — mega-cap tech, semis, financials, energy. All names are ETB; HTB borrow risk does not apply. Options are in scope for future expansion; crypto is not.

---

## Order types

| Type | Equities | Options | Crypto |
|------|----------|---------|--------|
| `market` | Yes | Yes | Yes |
| `limit` | Yes | Yes | Yes |
| `stop` | Yes | Yes | No |
| `stop_limit` | Yes | Yes | Yes |
| `trailing_stop` | Yes | No | No |

## Time-in-force

| TIF | Equities | Options | Crypto |
|-----|----------|---------|--------|
| `day` | Yes | **Yes (only)** | No |
| `gtc` | Yes | No | Yes |
| `ioc` | Yes | No | Yes |
| `fok` | Yes | No | No |
| `opg` (MOO/LOO) | Yes | No | No |
| `cls` (MOC/LOC) | Yes | No | No |

Options orders are day-only. The OMS must not attempt to place GTC options orders — the guardrail layer rejects these before they reach the adapter.

## Order classes

| Class | Equities | Options | Crypto | Notes |
|-------|----------|---------|--------|-------|
| `simple` | Yes | Yes | Yes | Default |
| `bracket` | Yes | **No** | No | Entry + `take_profit` (limit) + `stop_loss` (stop or stop-limit) as a single submission. Alpaca manages the full bracket lifecycle natively |
| `oco` | Yes | No | No | Pair of same-side exit orders. Take-profit + stop-loss after position is open |
| `oto` | Yes | No | No | Entry + one contingent child (take-profit OR stop-loss, not both) |
| `mleg` | No | Yes | No | Multi-leg options strategy, up to 4 legs |

**Critical consequence for options:** Alpaca does not support brackets, OCO, or OTO on options. AlphaMind's mandatory thesis-linked bracket requirement ([orders-and-brackets.md](orders-and-brackets.md)) is satisfied for options by the continuous monitor, which watches the underlying equity stream for price-based invalidation and submits a closing market order when the trigger fires. This is the same logic the monitor would run regardless of broker; Alpaca's gap is a constraint, not a new problem.

---

## Order submission — `POST /v2/orders`

The adapter constructs request bodies matching Alpaca's schema:

- Required: `symbol`, `side` (`buy` | `sell`), `type`, `time_in_force`
- Quantity: `qty` OR `notional` (mutually exclusive; `notional` is market-day-only)
- Conditional: `limit_price` (for limit/stop_limit), `stop_price` (for stop/stop_limit), `trail_price` or `trail_percent` (for trailing_stop)
- Optional: `client_order_id` (OMS writes its own order ID here for end-to-end correlation), `extended_hours` (limit-day/GTC only, bracket orders disallowed in extended hours)
- For brackets: `order_class: bracket` + `take_profit: { limit_price }` + `stop_loss: { stop_price, limit_price? }`
- For mleg: `order_class: mleg` + `legs: [{ symbol, side, ratio_qty, position_intent }]` (ratios must be in simplified form — GCD of ratios = 1)

The OMS provides `client_order_id` on every submission. This is the OMS's stable identifier; Alpaca returns its own `id` which the adapter maps to the client ID. The OMS never sees Alpaca's internal IDs.

**Acknowledgment semantics:** Alpaca's response is an order record, not a fill. The adapter treats this as submission acknowledgment only. Fill state arrives later via the `trade_updates` websocket.

**Rejection reasons** (mapped from Alpaca HTTP responses):
- `422` with validation errors → submission rejected, OMS logs and marks the command abandoned ([architecture.md](architecture.md))
- `403` with `insufficient_buying_power` or `insufficient_shares` → gateway rejection distinct from guardrail rejection; the PM sees this and may adjust on the next invocation
- Asset-level rejections (symbol halted, non-shortable on a short-sell, options level not approved) → also `403` / `422` with specific error codes

---

## Order cancellation — `DELETE /v2/orders/{id}`

Cancellation is fire-and-forget: the REST call acknowledges receipt, and the terminal state (`canceled`, or a race-condition `filled`) arrives via `trade_updates`.

## Order modification — `PATCH /v2/orders/{id}`

Alpaca's modification semantics are **cancel-and-replace**: the response carries a **new order ID**, and the OMS's `client_order_id` → Alpaca-ID mapping must be updated on acknowledgment. The adapter handles this mapping; the OMS's external identifier remains stable.

**Race window.** A `200` success from the PATCH endpoint does not guarantee replacement. If the original order fills between the PATCH arrival and the replace taking effect, the replacement is rejected (arrives as a `replace_rejected` event on `trade_updates`) and the original's fill takes precedence. During the window, Alpaca reserves buying power equal to the larger of (old order, new order). The OMS handles this race identically to a cancel race: whichever terminal event arrives first wins.

**Status restrictions.** Orders in `accepted`, `pending_new`, `pending_cancel`, or `pending_replace` cannot be replaced. The OMS must let these states settle before issuing a PATCH.

**Unreplaceable orders.** Notional orders and OTO orders cannot be replaced at all — modification of those requires explicit cancel + new submission at the OMS level. The OMS avoids notional orders for this and other reasons ([orders-and-brackets.md](orders-and-brackets.md)); OTO is avoided in favor of bracket.

**Fields modifiable via PATCH**: `limit_price`, `stop_price`, `qty`, `trail_price`, `trail_percent`, `time_in_force`. Bracket/OCO children can have their `limit_price` and `stop_price` modified in the same way.

---

## Fill stream — `trade_updates` websocket

The adapter subscribes to `wss://{paper|api}.alpaca.markets/stream` and authenticates with the account's API key/secret. It listens to the `trade_updates` channel (binary-framed, JSON or MessagePack codec). Events arrive as they occur on Alpaca's execution venue (paper: their simulation; live: real exchanges).

### Event types consumed

| Event | Meaning | OMS action |
|-------|---------|------------|
| `new` | Order accepted by the venue | Update order state to `pending` |
| `fill` | Full fill | Emit fill report, order terminal |
| `partial_fill` | Partial fill | Emit fill report, order remains `partially_filled` |
| `canceled` | Order canceled | Order terminal; cancel the bracket's sibling legs if this was a bracket leg (Alpaca does this natively for brackets it manages; OMS handles sibling cancellation only for strategies Alpaca does not manage) |
| `expired` | TIF expired (e.g., day order at close) | Order terminal |
| `replaced` | PATCH replacement took effect | Update Alpaca-ID mapping; new order inherits OMS `client_order_id` |
| `replace_rejected` | PATCH race lost | Discard the replacement; original order remains active |
| `stopped` | Order stopped (e.g., guaranteed-fill scenarios on some venues) | Treated as a variant of fill for OMS purposes |
| `rejected` | Post-acceptance rejection (rare — halts, corporate actions) | Order terminal as `rejected` |
| `done_for_day` | Day order with remaining quantity at session close | Order terminal (will not receive further events today) |
| `order_replace_rejected` | See `replace_rejected` | — |

### Fill buffer model

The continuous monitor owns the websocket subscription and writes each fill event into the fill buffer (`state-persistence.md`). The OMS drains the buffer during Phase 1 of each pipeline invocation (`architecture.md`). Buffer writes are durable — a monitor restart does not lose in-flight fills as long as the state DB is intact.

**Re-subscription on disconnect.** If the websocket disconnects, the monitor reconnects and re-queries Alpaca's `GET /v2/orders` with a `since` parameter to recover any events missed during the outage. Alpaca orders are authoritative; any disagreement between buffered state and Alpaca's reported state is resolved toward Alpaca.

---

## Fee reporting

Alpaca does not include regulatory fees in per-fill events. Fees accrue intraday and are charged at end-of-day, surfaced via `GET /v2/account/activities` as separate `FEE` activities. Fee types reported:

- **TAF** (FINRA Trading Activity Fee) — equity sells
- **CAT** (Consolidated Audit Trail) — all executed shares
- **SEC** (Section 31) — equity and option sells
- **ORF** (Options Regulatory Fee) — options
- **OCC** (Options Clearing Corporation) — options

The OMS accounts for fills pre-fee at fill time. At end-of-day (pre-close invocation or a dedicated reconciliation pass), it reads the day's fee activities, attributes them to their originating fills where possible, and applies the aggregate as a single `fee_reconciliation` event in the activity log. The [paper-evaluation harness](paper-evaluation-harness.md) estimates the expected fee impact at fill time using the current published rate table, for live-execution estimation; the actual debit still arrives via EOD reconciliation in live mode.

**Commission:** $0 on US-listed equities and options via the standard retail Trading API. No commission field is populated in the fill stream.

---

## Account state queries

The adapter exposes thin wrappers over Alpaca REST endpoints the OMS uses for state reconciliation:

- `GET /v2/account` — cash balance, equity, buying power (day and overnight), regt_buying_power, maintenance margin, day trade count, pattern day trader flag
- `GET /v2/positions` — current positions, authoritative. The OMS reconciles its internal position state against this on every invocation's Phase 1
- `GET /v2/orders` — order state query, used for disconnect recovery and spot reconciliation
- `GET /v2/account/activities` — EOD fee reconciliation, dividend activities, corporate action activities, and any other non-fill account debits/credits
- `GET /v2/assets/{symbol}` — asset metadata (`shortable` flag, `fractionable`, `tradable`, `easy_to_borrow`). Used by the guardrail layer when validating short-sell eligibility
- `GET /v2/calendar`, `GET /v2/clock` — trading calendar and current market state. Consulted by the scheduler and the continuous monitor

Alpaca's positions and account endpoints are the source of truth for the OMS's reconciliation. If local OMS state disagrees with Alpaca's response, Alpaca wins and the OMS logs a reconciliation delta.

---

## Rate limits

Alpaca throttles the Trading API at **200 requests/minute per account** on the retail tier. This is ample for AlphaMind's 8–10 invocations/day with ≤ 20 simultaneous positions — the dominant request budget is fill-reconciliation queries and the submission bursts during Phase 2, neither of which approach the limit.

The websocket has no per-subscription rate limit; it delivers events as they occur.

---

## Known gaps relative to AlphaMind's order vocabulary

These are cases where Alpaca's surface is narrower than AlphaMind's design; each is handled at the OMS/monitor layer rather than being worked around at the adapter layer:

1. **Brackets on options.** Unsupported. The continuous monitor evaluates options bracket stops against the underlying equity stream and submits a closing market order on trigger. Options P/L-based targets are monitored via the derived-pricing model (see [architecture.md § 4d — Greeks refresh orchestration](architecture.md)).

2. **Brackets in extended hours.** Unsupported. AlphaMind does not currently trade in extended hours; if it ever does, extended-hours orders must be submitted as atomic limits with the monitor handling protective legs.

3. **Trailing stop in bracket/OCO.** Alpaca currently supports trailing stops only as single atomic orders. The monitor will emulate trailing-stop behavior for bracket legs that require it by repeatedly PATCHing the stop leg's `stop_price` as the favorable-side price moves; this is an OMS-level emulation not pushed to Alpaca.

4. **OTO replace.** Unsupported at Alpaca. AlphaMind does not use OTO — brackets cover the OTO use case.

5. **Fractional shorts.** Alpaca restricts fractional orders to market + day TIF and disallows any action that would produce a net-short fractional position. AlphaMind does not use fractional sizing; orders are in whole shares and whole contracts.

6. **Corporate action handling.** Alpaca handles corporate actions natively (splits, dividends, mergers) and surfaces the adjustments via `account/activities`; post-adjustment position state is reflected in `GET /v2/positions` and `GET /v2/account`. The OMS-side integration mechanics — per-action quantity/cost-basis/cash mutations, spin-off child position creation, Phase 1 sequencing, idempotency, and activity log entries — are specified in [corporate-actions.md](corporate-actions.md). Bracket lifecycle is in [orders-and-brackets.md § Corporate action handling](orders-and-brackets.md#corporate-action-handling) and strategist re-evaluation is in [strategist.md § Corporate-action-pending positions](../04-decision-layer/strategist.md#corporate-action-pending-positions).

---

## Dependencies

- [architecture.md](architecture.md) — places this adapter within the four-component engine architecture
- [orders-and-brackets.md](orders-and-brackets.md) — the order vocabulary the adapter translates
- [paper-evaluation-harness.md](paper-evaluation-harness.md) — what runs on top of paper fills
- [state-persistence.md](state-persistence.md) — where the fill buffer lives
- [venue-configuration.md](venue-configuration.md) — Alpaca-specific venue rules (settlement, sessions, margin, PDT)
