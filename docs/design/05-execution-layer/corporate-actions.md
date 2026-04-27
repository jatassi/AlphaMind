# Corporate action processing

How the OMS integrates Alpaca-emitted corporate-action events into local position state. Position-layer mechanics — quantity, cost basis, ticker, status — plus cash-ledger movements and activity log entries. Bracket lifecycle ([orders-and-brackets.md § Corporate action handling](orders-and-brackets.md#corporate-action-handling)) and the strategist's per-action-type re-evaluation ([strategist.md § Corporate-action-pending positions](../04-decision-layer/strategist.md#corporate-action-pending-positions)) are specified elsewhere; this doc is the mechanical foundation they depend on.

---

## Scope

Covered:

- Position quantity, cost basis, ticker, and status mutations from Alpaca CA activities
- Cash credits and debits (long dividends, short dividend obligations, fractional cash-out, merger proceeds)
- Spin-off child position creation
- Phase 1 integration sequencing alongside fill processing
- Idempotency on Phase 1 retry
- Ex-date detection and application timing
- Activity log catalog additions

---

## Source of truth: Alpaca

Alpaca handles corporate actions natively ([broker-adapter.md § Known gaps](broker-adapter.md#known-gaps-relative-to-alphaminds-order-vocabulary)) — it computes split ratios, applies fractional-share cash-outs, allocates spin-off cost basis, etc. The OMS reads what Alpaca reports rather than deriving from market data or external CA calendars.

Two Alpaca surfaces serve as inputs:

1. **`GET /v2/account/activities`** — per-CA event records. Each carries the activity type (discriminator), ticker, transaction time, and per-action parameters (ratio for splits, amount for dividends, deal price for mergers, allocation for spin-offs). This is the trigger and parameter source.
2. **`GET /v2/positions` and `GET /v2/account`** — post-adjustment authoritative state. Used at end of Phase 1 to reconcile local state against Alpaca's view. On any unexplained delta, local state moves to match Alpaca's per the existing reconciliation discipline ([broker-adapter.md § Account state queries](broker-adapter.md#account-state-queries)).

The OMS reads, applies, and verifies — no parallel computation of Alpaca's behavior.

---

## Per-action-type matrix

Each row is one category of corporate action. The Alpaca activity-code column lists the relevant `activity_type` values from `/v2/account/activities`; the exact enum is verified against Alpaca's docs at implementation time and may include sub-codes (tax-classification variants, etc.) that route into the same handler.

| Action | Alpaca codes | Quantity | Cost basis | Ticker | Status | Cash impact | Activity log events |
|---|---|---|---|---|---|---|---|
| **Forward stock split** (e.g., 4:1) | `SPLIT` (ratio > 1) | × ratio | ÷ ratio per share | unchanged | open | none | `corporate_action_applied`, `bracket_cancelled_corporate_action` |
| **Reverse stock split** (e.g., 1:10) | `SPLIT` (ratio < 1) | ÷ ratio | × ratio per share | unchanged | open | credit if Alpaca cashes out a fractional residual | `corporate_action_applied`, `bracket_cancelled_corporate_action`, `cash_credited` (reason `fractional_share_cash_out`) |
| **Stock dividend** | `DIV` with stock-distribution flag | × (1 + rate) | ÷ (1 + rate) per share | unchanged | open | none | `corporate_action_applied`, `bracket_cancelled_corporate_action` |
| **Cash dividend (long)** | `DIV` (cash) | unchanged | unchanged | unchanged | open | credit (Alpaca-reported amount) | `corporate_action_applied`, `bracket_cancelled_corporate_action`, `cash_credited` (reason `cash_dividend_long`) |
| **Cash dividend (short obligation)** | `PTC` or short-dividend pass-through code | unchanged | unchanged | unchanged | open | debit (Alpaca-reported amount) | `corporate_action_applied`, `bracket_cancelled_corporate_action`, `cash_debited` (reason `cash_dividend_short_obligation`) |
| **Cash merger / acquisition** | `MA` (cash consideration) | zeroed | unchanged on record (closure realizes P/L) | unchanged | closed | credit (deal proceeds) | `corporate_action_applied`, `bracket_cancelled_corporate_action`, `position_closed` (exit method `corporate_action_cash_merger`), `cash_credited` (reason `cash_merger_proceeds`) |
| **Stock merger** | `MA` (stock consideration), often paired with `NC`/`SC` for the symbol change | per exchange ratio | per Alpaca allocation | new acquirer ticker | open | partial credit if cash-and-stock mix | `corporate_action_applied`, `bracket_cancelled_corporate_action`, optional `cash_credited` |
| **Spin-off** | `SPIN` | parent unchanged; child created | parent reduced by Alpaca-allocated portion; child carries the balance | parent unchanged; child = spun-off ticker | parent open; child open with `corporate_action_adjustment_needed` | none on either | `corporate_action_applied` (parent), `bracket_cancelled_corporate_action` (parent), `position_opened` on child (mechanism `spin_off_from_parent`) |
| **Symbol / name change** | `NC`, `SC` | unchanged | unchanged | new ticker | open | none | `corporate_action_applied`, `bracket_cancelled_corporate_action` |

The `corporate_action_applied` entry carries a structured payload sufficient to reconstruct the change: action type, Alpaca activity ID, ticker (and `new_ticker` for symbol changes and stock mergers), ratio/amount as reported by Alpaca, pre/post quantity, pre/post cost basis, signed cash impact, parent position ID (spin-offs), resulting position status. This is the audit-trail single-source-of-truth event; standard lifecycle events (`position_closed`, `position_opened`, `cash_credited`, `cash_debited`) are emitted alongside for downstream consumers that already process those generically.

---

## Spin-off child positions

Spin-offs are the one structural exception to the otherwise-mandatory thesis and bracket bindings on every position. When Alpaca reports a `SPIN` activity, the parent position retains its thesis and its (now-cancelled) bracket; a new child position is created in the local state representing the spun-off shares. The child carries:

- A unique position ID
- `instrument_type: equity`, `ticker:` the spun-off symbol, `quantity:` the Alpaca-reported allocation, `cost_basis:` the portion Alpaca allocates to the spin-off
- `thesis_id: null`
- `bracket_id: null`
- `status: open`
- `corporate_action_adjustment_needed: true`
- `origin: spin_off_from_<parent_position_id>` — provenance reference for the strategist's context and the audit trail

The child appears in [raw state category 1a](../01-data-layer/internal/portfolio-state.md) and is delivered to the strategist as a flagged position alongside the parent. The strategist applies its per-action defaults per [strategist.md § Corporate-action-pending positions](../04-decision-layer/strategist.md#corporate-action-pending-positions). The flagged orphan state is transient by design — the strategist resolves it in the same invocation that integrated the spin-off, the same first-class-assessment-item rule that applies to all other CA-flagged positions.

If the spun-off ticker is in the asset universe and signals warrant, the analyst may pitch a fresh OPEN on it at a subsequent invocation; that proposal is processed independently through the normal new-entry path.

---

## Phase 1 integration sequence

Corporate action integration runs alongside fill integration in [Phase 1 of the invocation cycle](state-persistence.md#phase-1-write-path-fill-integration). The OMS:

1. **Query Alpaca for unprocessed activities.** `GET /v2/account/activities?after=<cursor>` returns all activities (fills, CAs, fees, etc.) since the last successful Phase 1. CA-related activity types are filtered into a separate processing queue.
2. **Merge with unprocessed fills chronologically.** Each fill record carries `fill_timestamp`; each CA activity carries `transaction_time`. Sort the merged set ascending. Chronological ordering matters when a fill straddles an ex-date — fills before the CA reflect pre-action quantities, fills after reflect post-action quantities. In practice the overlap is rare (CAs typically apply outside market hours; fills typically don't) but the ordering rule is unconditional.
3. **Process each event in order.** Fills follow the existing Phase 1 sequence ([state-persistence.md § Phase 1 write path](state-persistence.md#phase-1-write-path-fill-integration)). CA activities follow the per-action-type matrix above: apply the mutation to local position state from the activity record's parameters, write activity log entries, cancel the bracket via the engine path described in [orders-and-brackets.md § Corporate action handling](orders-and-brackets.md#corporate-action-handling), set the `corporate_action_adjustment_needed` flag, create spin-off children where applicable.
4. **Reconcile.** After processing the merged batch, compare local position state to `GET /v2/positions` and local cash to `GET /v2/account`. Any unexplained delta is logged as a reconciliation alert and resolved toward Alpaca's authoritative state.
5. **Mark processed atomically.** Both unprocessed fills and unprocessed CA activities are marked processed as part of the Phase 1 transaction commit.

The full sequence is one atomic transaction. If any step fails, the transaction rolls back: fills remain unprocessed, CA activities remain in the unprocessed-activities ledger, and the next Phase 1 retries the full set. This matches the existing fail-closed mid-pipeline policy ([mid-pipeline-failure-handling.md](../mid-pipeline-failure-handling.md)) — no checkpoint, no partial commit.

---

## Idempotency

A `processed_corporate_actions` ledger tracks which Alpaca activity IDs the OMS has integrated. One row per CA activity, keyed on `alpaca_activity_id` (deduplication anchor), with `processing_invocation_id`, `processing_timestamp`, and `processing_status` (`processed` after the Phase 1 transaction commits). On Phase 1 retry, the OMS skips any activity ID already present in the ledger.

This parallels the fill records' `processing_status` field. A separate ledger (rather than full CA-activity records persisted locally) keeps the writes narrow: the deduplication key is the only thing that needs to survive across retries, since `/v2/account/activities` is queryable as the authoritative store and the activity log entry written at integration time captures everything needed for human and feedback-loop audit.

---

## Ex-date detection and timing

Per [orders-and-brackets.md § Corporate action handling](orders-and-brackets.md#corporate-action-handling), scheduled CAs are applied at the first scheduled invocation on or after ex-date. The 9:00 AM ET pre-open invocation is the normal case: position-level adjustments fire first in Phase 1, brackets are cancelled, and then the strategist and PM run in Phase 2 with the flagged positions in context, producing fresh brackets before the 9:30 AM market open.

Detection mechanism: the `GET /v2/account/activities?after=<cursor>` query at the start of Phase 1 returns any CA activities posted since the last invocation. Alpaca posts CA activities on or after ex-date according to its own internal schedule; the OMS does not pre-fetch upcoming CAs.

Mid-day intraday CAs are picked up at the next regularly-scheduled invocation. During the gap between Alpaca posting the activity and the OMS integrating it, the local position record is briefly stale; the position-level max-loss guardrail ([rules-and-limits.md](../06-risk-guardrails/rules-and-limits.md)) and the continuous monitor's breach detection ([architecture.md § 4b](architecture.md)) operate against the stale local quantity until the next Phase 1 reconciles. This is accepted as the cost of the regularly-scheduled invocation cadence rather than introducing an out-of-band CA-integration trigger.

Trading halts, bankruptcies, and delistings are surfaced through different Alpaca channels and have their own handling.

---

## Options positions

For options positions, Alpaca pre-adjusts strike, contract count, and contract multiplier per OCC standards on splits and stock dividends. The OMS reads the post-adjustment values from `GET /v2/positions` (option positions are returned with their adjusted contract spec) and updates the local options position record. The same uniform cancel-and-review bracket policy applies — the monitor-managed underlying-stream stop is dropped, and the strategist proposes either a fresh stop re-scaled to the new strike or a close.

Mergers and spin-offs on optioned underlyings are structurally messier: option contracts may convert into adjusted contracts on a different underlying or into deliverable cash. Alpaca surfaces these as a sequence of activities. The OMS consumes them mechanically — apply Alpaca's post-adjustment state — and the strategist's emphatic-default-close stance for these cases ([strategist.md § Corporate-action-pending positions](../04-decision-layer/strategist.md#corporate-action-pending-positions)) covers the thesis-survival question.

Greeks on the post-CA position are recomputed at the next greeks refresh ([architecture.md § 4d](architecture.md)) using the new contract spec. Pre-CA greek values are stale by definition and not migrated.

---

## Activity log catalog additions

The activity log event types named in the per-action-type matrix are defined in [state-persistence.md § Event type catalog](state-persistence.md#tier-2--lifecycle-entities). This doc adds:

- `corporate_action_applied` — new event type. Detail: action type, Alpaca activity ID, ticker (and `new_ticker` if applicable), ratio or amount as reported by Alpaca, pre-action quantity, post-action quantity, pre-action cost basis, post-action cost basis, signed cash impact, parent position ID (spin-off only), resulting position status
- `bracket_cancelled_corporate_action` — new event type, complementary to the existing `bracket_dissolved` and `bracket_modified`. Detail: bracket ID, cancellation reason (`corporate_action_<action_type>`), cancelled leg order IDs
- `cash_credited` — new reason values: `cash_dividend_long`, `cash_merger_proceeds`, `fractional_share_cash_out`
- `cash_debited` — new reason value: `cash_dividend_short_obligation`
- `position_closed` — new exit method: `corporate_action_cash_merger`
- `position_opened` — new mechanism value: `spin_off_from_parent` (parent position ID in the detail)

All other CA-related state changes use the existing event vocabulary unchanged.

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
