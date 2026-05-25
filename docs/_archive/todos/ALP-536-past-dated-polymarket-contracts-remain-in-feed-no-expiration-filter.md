## Symptom

Prediction-market contracts whose resolution dates have already passed continue to appear in the per-invocation snapshot fed to QR and to the universal-context `qual.prediction_market_delta` slice. They are surfaced as live probabilities (with their static stale value) rather than being filtered out as resolved/expired.

## Evidence

E2E invocation: `/Volumes/Users/jacks/AlphaMind/.archive/verify-debug-e2e/invocations/inv-20260518T111140Z-7b54d0d6/`

Invocation date: 2026-05-18.

`analysis/qualitative_researcher/user_message.md`:

* Line 125: `[0x1460e5fe...] Iran closes its airspace by **May 6**? (polymarket/conflict): prob=0.0005, ... expires=2026-05-31T00:00:00Z` — the *question* asks about an event by May 6, twelve days before the invocation, yet the contract is still listed as live.
* Line 206: `[0xc7425245...] Will Donald Trump visit China on **May 3, 2026**? (polymarket/china_policy): prob=0.0005, ... expires=2026-05-31T00:00:00Z [LOW LIQUIDITY]` — same shape, 15 days past the question's reference date.

Separately, the `qual.prediction_market_delta` block in `analysis/synthesizer/user_message.md` line 740 onwards repeats the same past-dated contracts as part of per-contract trailing history.

## Root cause hypothesis

The polymarket ingestion path filters on `contract.expires_at > now` (which is correctly `2026-05-31` for both contracts above). But the *question itself* references a date that has already passed (`May 6` for the Iran contract, `May 3` for the Trump-China contract). The vendor leaves these listed with their last-traded probability until formal resolution, and our pipeline forwards them verbatim.

Two plausible filter additions:

1. Parse the question text for explicit dates and drop contracts whose referenced date is in the past.
2. Drop contracts whose `volume_24h_usd` is below a threshold AND whose `delta_pp_since_prior` has been 0 for N consecutive snapshots — captures "effectively resolved, awaiting formal close" without needing date extraction.

Option 2 is more robust (no NLP) and fits the existing low-liquidity flag pattern.

## Scope

Add an expiration/staleness filter to the polymarket ingestion or to the per-invocation snapshot assembly that drops or downweights contracts whose referenced event is past or whose probability has been flat through the entire trailing-history window.

## Acceptance criteria

- [ ] Contracts whose question text references a date earlier than the current invocation date are excluded from the per-invocation snapshot (or surfaced under a separate "RESOLVED — AWAITING SETTLEMENT" section that downstream agents can ignore).
- [ ] Contracts with `volume_24h_usd < threshold` AND zero delta across all trailing-history snapshots are downweighted in the QR digest's top-N ranking.

## Verification

* Re-run on 2026-05-19 or later; confirm the Iran-airspace-by-May-6 and Trump-visit-China-on-May-3 contracts no longer appear in the live snapshot.
* Spot-check that the polymarket ingestion logs a count of contracts excluded for staleness.

## Notes

Not bootstrap-related — these contracts have been present in feeds for at least 12 days (since 2026-05-06) without being filtered.