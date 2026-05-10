# Corporate action processing

How the OMS integrates Alpaca-emitted corporate-action events into local position state. Position-layer mechanics — quantity, cost basis, ticker, status — plus cash-ledger movements and activity log entries. Bracket lifecycle ([orders-and-brackets.md § Corporate action handling](orders-and-brackets.md#corporate-action-handling)) and the strategist's per-action-type re-evaluation ([strategist.md § Corporate-action-pending positions](../04-decision-layer/strategist.md#corporate-action-pending-positions)) are specified elsewhere; this doc is the mechanical foundation they depend on.

---

## Scope

- Position quantity, cost basis, ticker, and status mutations from Alpaca CA activities
- Cash credits and debits (long dividends, short dividend obligations, fractional cash-out, merger proceeds)
- Spin-off child position creation
- Phase 1 integration sequencing alongside fill processing
- Idempotency on Phase 1 retry
- Ex-date detection and application timing
- Activity log catalog additions

---

## Source of truth: Alpaca

Alpaca handles corporate actions natively ([broker-adapter.md § Known gaps](broker-adapter.md#known-gaps-relative-to-alphaminds-order-vocabulary)) — computing split ratios, applying fractional-share cash-outs, allocating spin-off cost basis. The OMS reads what Alpaca reports rather than deriving from market data or external CA calendars.

Two Alpaca surfaces serve as inputs:

1. **`GET /v1beta1/corporate-actions`** (v1beta1 Corporate Actions Market Data API) — per-CA event records with per-action-type typed shapes. Each typed model extends `ModelWithID` and carries a UUID `id` plus per-event parameters (`new_rate` / `old_rate` for splits, `rate` for dividends, `acquirer_*` / `acquiree_*` for mergers, `source_*` / `new_*` for spin-offs, `old_*` / `new_*` for name changes). The trigger and parameter source.
2. **`GET /v2/positions` and `GET /v2/account`** — post-adjustment authoritative state. Used at end of Phase 1 to reconcile local state against Alpaca's view. On any unexplained delta, local state moves to match Alpaca's per the reconciliation discipline ([broker-adapter.md § Account state queries](broker-adapter.md#account-state-queries)).

The OMS reads, applies, and verifies — no parallel computation of Alpaca's behavior.

Note on PTC (short-dividend pass-through): v1beta1 does not surface a separate PTC event code. The fetcher discriminates a `CashDividend` event into `CASH_DIVIDEND_LONG` vs. `CASH_DIVIDEND_SHORT` locally based on the matching position's `direction` (LONG / SHORT).

Note on transaction-time anchoring: v1beta1 events carry date fields (`ex_date`, `process_date`, `effective_date`) rather than datetimes. The fetcher anchors each event at midnight UTC on its primary date (`ex_date` where present, else `process_date` / `effective_date`). The Phase 1 chronological merge ([Phase 1 integration sequence](#phase-1-integration-sequence)) compares this against fill `fill_timestamp` (a precise datetime); fills on the ex-date resolve before the CA after midnight-UTC anchoring as the design intends.

---

## Per-action-type matrix

Each row is one category of corporate action. The "v1beta1 typed model" column names the `alpaca.data.models.corporate_actions` class returned by `GET /v1beta1/corporate-actions`; the fetcher discriminates the local `CorporateActionType` from that model plus, for cash dividends, the matched position's direction.

| Action | v1beta1 typed model | Local `CorporateActionType` | Quantity | Cost basis | Ticker | Status | Cash impact | Activity log events |
|---|---|---|---|---|---|---|---|---|
| **Forward stock split** (e.g., 4:1) | `ForwardSplit` | `SPLIT` | × ratio (`new_rate / old_rate`) | ÷ ratio per share | unchanged | open | none on the event; handler computes any fractional residual itself | `corporate_action_applied`, `bracket_cancelled_corporate_action` |
| **Reverse stock split** (e.g., 1:10) | `ReverseSplit` | `REVERSE_SPLIT` | ÷ ratio (`new_rate / old_rate`) | × ratio per share | unchanged | open | none on the event; handler computes the fractional residual itself when needed | `corporate_action_applied`, `bracket_cancelled_corporate_action`, `cash_credited` (reason `fractional_share_cash_out`) |
| **Stock dividend** | `StockDividend` | `STOCK_DIVIDEND` | × (1 + rate) | ÷ (1 + rate) per share | unchanged | open | none | `corporate_action_applied`, `bracket_cancelled_corporate_action` |
| **Cash dividend (long)** | `CashDividend` (position direction LONG) | `CASH_DIVIDEND_LONG` | unchanged | unchanged | unchanged | open | credit (`rate × position quantity`) | `corporate_action_applied`, `bracket_cancelled_corporate_action`, `cash_credited` (reason `cash_dividend_long`) |
| **Cash dividend (short obligation)** | `CashDividend` (position direction SHORT) | `CASH_DIVIDEND_SHORT` | unchanged | unchanged | unchanged | open | debit (`-(rate × position quantity)`) | `corporate_action_applied`, `bracket_cancelled_corporate_action`, `cash_debited` (reason `cash_dividend_short_obligation`) |
| **Cash merger / acquisition** | `CashMerger` | `CASH_MERGER` | zeroed | unchanged on record (closure realizes P/L) | unchanged | closed | credit (`rate × position quantity`) | `corporate_action_applied`, `bracket_cancelled_corporate_action`, `position_closed` (exit method `corporate_action_cash_merger`), `cash_credited` (reason `cash_merger_proceeds`) |
| **Stock merger** | `StockMerger` / `StockAndCashMerger` | `STOCK_MERGER` | per `acquirer_rate / acquiree_rate` | per Alpaca allocation | new acquirer ticker | open | for `StockAndCashMerger`: partial credit (`cash_rate × position quantity`); for pure `StockMerger`: none | `corporate_action_applied`, `bracket_cancelled_corporate_action`, optional `cash_credited` |
| **Spin-off** | `SpinOff` | `SPIN_OFF` | parent unchanged; child created with `new_rate / source_rate` allocation per parent share | parent reduced by Alpaca-allocated portion; child carries the balance | parent unchanged; child = `new_symbol` | parent open; child open with `corporate_action_adjustment_needed` | none on either | `corporate_action_applied` (parent), `bracket_cancelled_corporate_action` (parent), `position_opened` on child (mechanism `spin_off_from_parent`) |
| **Symbol / name change** | `NameChange` | `SYMBOL_CHANGE` | unchanged | unchanged | `new_symbol` | open | none | `corporate_action_applied`, `bracket_cancelled_corporate_action` |

The `corporate_action_applied` entry carries a structured payload sufficient to reconstruct the change: action type, Alpaca activity ID, ticker (and `new_ticker` for symbol changes and stock mergers), ratio/amount as reported by Alpaca, pre/post quantity, pre/post cost basis, signed cash impact, parent position ID (spin-offs), resulting position status. This is the audit-trail single-source-of-truth event; standard lifecycle events (`position_closed`, `position_opened`, `cash_credited`, `cash_debited`) are emitted alongside for downstream consumers that already process those generically.

---

## Spin-off child positions

Spin-offs are the one structural exception to the otherwise-mandatory thesis and bracket bindings. On a `SPIN` activity, the parent retains its thesis and (now-cancelled) bracket; a new child position represents the spun-off shares with:

- Unique position ID
- `instrument_type: equity`, `ticker:` spun-off symbol, `quantity:` Alpaca-reported allocation, `cost_basis:` Alpaca's allocated portion
- `thesis_id: null`
- `bracket_id: null`
- `status: open`
- `corporate_action_adjustment_needed: true`
- `origin: spin_off_from_<parent_position_id>` — provenance reference

The child appears in [raw state category 1a](../01-data-layer/internal/portfolio-state.md) and is delivered to the strategist as a flagged position alongside the parent. The strategist applies its per-action defaults per [strategist.md § Corporate-action-pending positions](../04-decision-layer/strategist.md#corporate-action-pending-positions). The orphan state is transient — resolved in the same invocation that integrated the spin-off.

If the spun-off ticker is in the asset universe and signals warrant, the analyst may pitch a fresh OPEN at a subsequent invocation through the normal new-entry path.

---

## Phase 1 integration sequence

CA integration runs alongside fill integration in [Phase 1](state-persistence.md#phase-1-write-path-fill-integration). The OMS:

1. **Query Alpaca for unprocessed activities.** `GET /v1beta1/corporate-actions?start=<cursor_date>&end=<today>&types=...` returns typed CA events for the configured set of types. Symbols not matched to a local position are filtered out; activities whose `id` (UUID) is already in `corporate_action_integration_ledger` are skipped (dedup absorbs the lookback overlap that protects against Alpaca's late posts).
2. **Merge with unprocessed fills chronologically.** Each fill carries `fill_timestamp` (a precise datetime); each CA event carries an `ex_date` / `process_date` / `effective_date` (a date) which the fetcher anchors at midnight UTC. Sort ascending. Ordering matters when a fill straddles an ex-date — fills before the CA reflect pre-action quantities, fills after reflect post-action. CAs typically apply outside market hours, and midnight-UTC anchoring guarantees an intra-day fill on the ex-date resolves before the CA.
3. **Process each event in order.** Fills follow the existing Phase 1 sequence ([state-persistence.md § Phase 1 write path](state-persistence.md#phase-1-write-path-fill-integration)). CA activities follow the per-action-type matrix: apply the mutation, write activity log entries, cancel the bracket via [orders-and-brackets.md § Corporate action handling](orders-and-brackets.md#corporate-action-handling), set `corporate_action_adjustment_needed`, create spin-off children where applicable.
4. **Reconcile.** Compare local position state to `GET /v2/positions` and local cash to `GET /v2/account`. Unexplained deltas are logged as reconciliation alerts and resolved toward Alpaca.
5. **Mark processed atomically.** Both unprocessed fills and unprocessed CA activities are marked processed as part of the Phase 1 transaction commit.

The full sequence is one atomic transaction. On any failure, the transaction rolls back; fills and CA activities remain unprocessed, and the next Phase 1 retries the full set. Matches the fail-closed mid-pipeline policy ([mid-pipeline-failure-handling.md](../mid-pipeline-failure-handling.md)) — no checkpoint, no partial commit.

---

## Idempotency

A `processed_corporate_actions` ledger tracks integrated Alpaca activity IDs. One row per CA activity keyed on `alpaca_activity_id` (deduplication anchor), with `processing_invocation_id`, `processing_timestamp`, and `processing_status` (`processed` after the Phase 1 transaction commits). On Phase 1 retry, the OMS skips activity IDs already in the ledger.

Parallels the fill records' `processing_status` field. The narrow ledger keeps writes minimal — `/v1/corporate-actions` (v1beta1 Corporate Actions Market Data API) is the authoritative queryable store, and the activity log entry at integration time captures everything needed for audit.

---

## Ex-date detection and timing

Per [orders-and-brackets.md § Corporate action handling](orders-and-brackets.md#corporate-action-handling), scheduled CAs are applied at the first scheduled invocation on or after ex-date. The 9:00 AM ET pre-open invocation is the normal case: position-level adjustments fire first in Phase 1, brackets are cancelled, and the strategist and PM run in Phase 2 with the flagged positions in context, producing fresh brackets before the 9:30 AM open.

Detection: the `GET /v1beta1/corporate-actions?start=<cursor_date>&end=<today>` query at Phase 1 start returns CA events whose `ex_date` (or `process_date` / `effective_date`) falls in the lookback window. Alpaca posts CA events on or after ex-date on its own schedule; the OMS does not pre-fetch. The lookback (`config.fetcher_lookback_days`, default 7) absorbs any late posts; the per-event UUID `id` dedup against the ledger prevents double-counting on the overlap.

Mid-day intraday CAs are picked up at the next scheduled invocation. During the gap, the local position record is briefly stale; the position-level max-loss guardrail ([rules-and-limits.md](../06-risk-guardrails/rules-and-limits.md)) and the continuous monitor's breach detection ([architecture.md § 4b](architecture.md)) operate against the stale local quantity until the next Phase 1 reconciles. Trading halts, bankruptcies, and delistings are surfaced through different Alpaca channels.

---

## Options positions

For options, Alpaca pre-adjusts strike, contract count, and contract multiplier per OCC standards on splits and stock dividends. The OMS reads post-adjustment values from `GET /v2/positions` and updates the local options position record. The cancel-and-review bracket policy applies — the monitor-managed underlying-stream stop is dropped, and the strategist proposes a fresh stop re-scaled to the new strike or a close.

Mergers and spin-offs on optioned underlyings are messier: contracts may convert into adjusted contracts on a different underlying or into deliverable cash. Alpaca surfaces these as a sequence of activities; the OMS applies Alpaca's post-adjustment state, and the strategist's default-close stance ([strategist.md § Corporate-action-pending positions](../04-decision-layer/strategist.md#corporate-action-pending-positions)) covers the thesis-survival question.

Greeks on the post-CA position are recomputed at the next greeks refresh ([architecture.md § 4d](architecture.md)) using the new contract spec. Pre-CA greeks are stale and not migrated.

---

## Activity log catalog additions

Event types named in the matrix are defined in [state-persistence.md § Event type catalog](state-persistence.md#tier-2--lifecycle-entities). This doc adds:

- `corporate_action_applied` — new. Detail: action type, Alpaca activity ID, ticker (and `new_ticker` if applicable), ratio/amount, pre/post quantity, pre/post cost basis, signed cash impact, parent position ID (spin-off only), resulting position status
- `bracket_cancelled_corporate_action` — new, complementary to `bracket_dissolved` and `bracket_modified`. Detail: bracket ID, cancellation reason (`corporate_action_<action_type>`), cancelled leg order IDs
- `cash_credited` — new reason values: `cash_dividend_long`, `cash_merger_proceeds`, `fractional_share_cash_out`
- `cash_debited` — new reason value: `cash_dividend_short_obligation`
- `position_closed` — new exit method: `corporate_action_cash_merger`
- `position_opened` — new mechanism: `spin_off_from_parent` (parent position ID in detail)

---

## Cross-references

- Bracket lifecycle on CA: [orders-and-brackets.md § Corporate action handling](orders-and-brackets.md#corporate-action-handling)
- Strategist re-evaluation per action type: [strategist.md § Corporate-action-pending positions](../04-decision-layer/strategist.md#corporate-action-pending-positions)
- Phase 1 fill integration sequence: [state-persistence.md § Phase 1 write path](state-persistence.md#phase-1-write-path-fill-integration)
- Activity log event catalog: [state-persistence.md § Event type catalog](state-persistence.md#tier-2--lifecycle-entities)
- Reconciliation discipline (Alpaca authoritative): [broker-adapter.md § Account state queries](broker-adapter.md#account-state-queries)
- Fail-closed mid-pipeline policy: [mid-pipeline-failure-handling.md](../mid-pipeline-failure-handling.md)
- Position-model fields and instrument types: [position-model.md](position-model.md)
- Continuous monitor breach detection and greeks refresh: [architecture.md § 4b, § 4d](architecture.md)
- Position-level max-loss guardrail: [rules-and-limits.md](../06-risk-guardrails/rules-and-limits.md)
