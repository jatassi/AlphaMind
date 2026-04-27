# Venue configuration

Alpaca-specific venue rules the OMS needs to correctly compute portfolio state — properties of Alpaca's execution environment (settlement, session boundaries, regulatory constraints) affecting cash accounting, order duration semantics, and compliance, separate from broker adapter translation mechanics.

---

## Why this is separate from the broker adapter

The [broker adapter](broker-adapter.md) translates OMS commands into Alpaca REST calls and surfaces fill events. The rules below affect how the OMS interprets the *results* of orders, not how the adapter processes them. Settlement cycle determines when cash from a sale becomes available. Market hours determine when a "day" order expires. OMS accounting rules parameterized by the venue.

---

## Settlement

**Settlement cycle:** delay between trade execution and actual transfer of cash/securities. Per instrument class.

- US equities: T+1 (trade date plus one business day)
- US options: T+1

The OMS uses settlement cycle to compute **available cash** (distinct from total cash). After a sell fills, proceeds record immediately in total portfolio value and P/L, but don't become available for new purchases until settlement completes. Prevents overtrading on unsettled funds, which Alpaca would reject.

**Pre-settlement credit:** Alpaca extends credit against unsettled proceeds on margin accounts. AlphaMind runs on a margin account (required for short selling and Reg T buying power), so the OMS treats unsettled proceeds as available, subject to the margin model.

**Day trade settlement:** if the system opens and closes a position within the same session, settlement matters less (buy and sell settle together). If the system sells and immediately buys with the proceeds, the second trade's settlement depends on Alpaca's pre-settlement credit policy (normally enabled on margin accounts).

---

## Market hours and session boundaries

Session awareness serves two purposes:

**Order duration semantics.** A "day" order expires at session end. The OMS must know when sessions begin and end to correctly interpret `time_in_force: day` and set appropriate GTD timestamps.

- US equities regular session: 9:30 AM – 4:00 PM ET
- US equities extended hours (pre-market): 4:00 AM – 9:30 AM ET
- US equities extended hours (after-hours): 4:00 PM – 8:00 PM ET
- US options: 9:30 AM – 4:00 PM ET (no extended hours)

Session schedules are authoritative from Alpaca's `GET /v2/calendar` and `GET /v2/clock`, consulted by the scheduler and continuous monitor.

**Business day calculations.** Settlement cycles are measured in business days. The OMS caches Alpaca's calendar (excluding weekends and holidays) for settlement-date computation and refreshes periodically.

The OMS does not use session awareness to gate submission — Alpaca accepts orders regardless of time, and closed-venue orders queue until the next session opens. Session awareness is purely for accounting and duration calculation.

---

## Regulatory and account constraints

**Pattern day trader (PDT) rule:** Alpaca accounts under $25,000 equity are limited to three day trades in a rolling five-business-day period. Alpaca enforces server-side and surfaces the count via `GET /v2/account` (`daytrade_count`, `pattern_day_trader` flag). The OMS reads these fields on every invocation and mirrors them in its risk-budget accounting so the PM sees the current count and remaining headroom; Alpaca's server-side enforcement is authoritative.

AlphaMind's target capital deployment schedule ([paper-evaluation-harness.md](paper-evaluation-harness.md), [risk guardrails](../06-risk-guardrails/README.md)) keeps PDT non-binding at most tiers — the 4–72h hold horizon inherently avoids day-trade classification. The OMS tracks the count as defense against edge cases.

**Margin tiers.** Alpaca applies Reg T initial and maintenance margin:

- Long equity: 50% initial, 25% maintenance
- Short equity: 150% initial (50% margin + 100% short proceeds), 130% maintenance. Short opens are ETB-only; HTB shorting is not supported — see [broker-adapter.md § Supported instruments](broker-adapter.md)
- Options buying: 100% (fully paid)
- Short options: Alpaca's margin formula based on underlying price, strike distance, and volatility
- Overnight buying power: 2× (Reg T standard)
- Intraday buying power: 4× for PDT-qualified accounts with ≥$25k equity

**Margin interest rates:**
- Standard: 6.25% (base + 2.5%)
- Elite (accounts ≥$100k deposited): 4.75% (base + 1.0%)

Interest accrues daily and charges monthly on EOD debit balances. Intraday leverage does not accrue interest.

**Reg T ceiling.** Alpaca runs Reg T margin, not portfolio margin. Hedged or paired long/short positions are margined per-leg with no risk netting — a well-hedged book pays margin on the gross, not the net. Non-binding at current and near-term deployment scale; if future scale makes per-leg margining the capital bottleneck, the broker choice warrants revisiting.

**Wash sale tracking:** the OMS logs potential wash sale events (sells followed by buys of substantially identical securities within a 30-day window) for tax-lot accounting. Does not block trades on wash-sale rules — that's a tax optimization strategy, not a structural constraint. Alpaca surfaces cost-basis adjustments via `account/activities` after the fact.

---

## Configuration structure

Alpaca-specific configuration lives in a single config file loaded at adapter initialization:

- Settlement rules per instrument class (equities T+1, options T+1)
- Pre-settlement credit enabled (required for margin-account operation)
- Session schedule (via `GET /v2/calendar`)
- Regulatory rule set (PDT threshold $25k, margin percentages as listed above)
- Fee schedule reference (rates for SEC / TAF / CAT / OCC / ORF, used by the [paper-evaluation harness](paper-evaluation-harness.md) to estimate fee drag at fill time — Alpaca's actual EOD fee debits from `account/activities` remain authoritative)
- Margin interest tier (Standard or Elite, determined by account equity)

Configuration is immutable for the lifetime of the adapter instance. Changing config (e.g., crossing the Elite threshold) means a restart.

---

## Dependencies

- [Architecture](architecture.md) — defines the OMS as the owner of venue-level configuration
- [Broker adapter](broker-adapter.md) — the Alpaca-specific execution surface this configuration complements
- [Position model](position-model.md) — margin calculations reference the percentages supplied here
- [Paper-evaluation harness](paper-evaluation-harness.md) — fee-rate table used for live-execution estimation
