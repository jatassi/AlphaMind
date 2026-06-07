"""Layer-2/3 cross-field-invariant validator for AnalystOutput — ALP-296.

Implements producer-side checks from
``docs/design/testing/llm-output-validation.md`` for the analyst agent's
output:

* **Layer 2** — structural invariants Pydantic cannot see across siblings
  (leg-id pairing, underlying/instrument equality, hard-leg defense, unique
  recommendation IDs, sector-in-active-set, asset-type feature-disabled
  defense, entry-window deadline not in the past, time-horizon consistency
  with the entry window, conviction-band sizing deviation).

* **Layer 3** — referential integrity: every ``[XX-N]`` reference embedded
  in narrative fields must resolve to an entry in the per-invocation
  :class:`~alphamind.analysis.synthesizer.retrieval.RetrievalStore`.

The validator returns the *full* error and warning inventory; the harness
(story 07) surfaces only the first error in the corrective-retry message.
Errors disqualify the output (``is_valid=False``); warnings do not.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime

from alphamind.analysis.synthesizer.models import (
    REF_ID_RE,
    find_bare_prefix_citations,
    parse_reference_id,
)
from alphamind.analysis.synthesizer.retrieval import RetrievalStore
from alphamind.commands.validation_results import (
    ValidationError,
    ValidationResult,
    ValidationWarning,
)
from alphamind.decision.analyst.models import (
    AnalystOutput,
    InstrumentEquity,
    PriceCondition,
    Recommendation,
    WatchlistEntry,
)

__all__ = [
    "DEFAULT_CONVICTION_BANDS",
    "DEFAULT_PRICE_STALENESS_TOLERANCE_PCT",
    "ValidationError",
    "ValidationResult",
    "ValidationWarning",
    "validate_analyst_output",
]


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# (lower_pct, upper_pct) per conviction level. See
# ``docs/design/04-decision-layer/analyst.md`` § Conviction scale.
DEFAULT_CONVICTION_BANDS: dict[int, tuple[float, float]] = {
    1: (0.25, 0.75),
    2: (0.5, 1.5),
    3: (1.0, 3.0),
    4: (2.0, 4.0),
    5: (3.0, 5.0),
}

# Substring the guardrail validation tool uses to flag a recommendation that
# tripped the active profile's feature-flag gate (e.g., options on a profile
# that forbids them). Matched against ``cumulative_impact_note`` and
# per-rule ``rule`` text.
_FEATURE_DISABLED_TOKEN = "feature_disabled"

# Maximum tolerated drift between the analyst's implied per-share entry anchor
# and the latest ``ohlcv_bars`` close (as a percentage of the live close) before
# the recommendation is flagged for redraft (ALP-742). A bracket sized against a
# price this far from the live close produces target/stop levels the broker
# rejects as incoherent (the 2026-05-29 JPM-short / CRWD-long cancellations).
# Mirrors the ``DEFAULT_CONVICTION_BANDS`` pattern: a module default the caller
# may override; it is not (yet) YAML-wired.
DEFAULT_PRICE_STALENESS_TOLERANCE_PCT: float = 5.0


# ---------------------------------------------------------------------------
# Layer-2 per-recommendation checks (a)-(i)
# ---------------------------------------------------------------------------


def _check_invalidation_leg_id_pairing(
    rec: Recommendation, *, field_prefix: str
) -> Iterable[ValidationError]:
    """(a) Bidirectional leg_id pairing between rationale and legs.

    Every ``invalidation_rationale[].leg_id`` must match an existing
    ``invalidation_legs[].leg_id`` and vice versa. Missing in either
    direction is an error.
    """
    leg_ids = {leg.leg_id for leg in rec.invalidation_legs}
    rationale_ids = {r.leg_id for r in rec.invalidation_rationale}
    for j, rationale in enumerate(rec.invalidation_rationale):
        if rationale.leg_id not in leg_ids:
            yield ValidationError(
                field_path=f"{field_prefix}.invalidation_rationale[{j}].leg_id",
                rule="invalidation_leg_id_pairing",
                message=(
                    f"invalidation_rationale leg_id {rationale.leg_id!r} does not match any "
                    f"invalidation_legs[].leg_id in {rec.recommendation_id}"
                ),
            )
    for i, leg in enumerate(rec.invalidation_legs):
        if leg.leg_id not in rationale_ids:
            yield ValidationError(
                field_path=f"{field_prefix}.invalidation_legs[{i}].leg_id",
                rule="invalidation_leg_id_pairing",
                message=(
                    f"invalidation_legs leg_id {leg.leg_id!r} has no matching "
                    f"invalidation_rationale entry in {rec.recommendation_id}"
                ),
            )


def _check_underlying_matches_instrument(
    rec: Recommendation, *, field_prefix: str
) -> Iterable[ValidationError]:
    """(b) ``underlying`` equals ``instrument.ticker`` (equity) or ``instrument.underlying``."""
    if isinstance(rec.instrument, InstrumentEquity):
        expected = rec.instrument.ticker
        instrument_field = "ticker"
    else:
        expected = rec.instrument.underlying
        instrument_field = "underlying"
    if rec.underlying != expected:
        yield ValidationError(
            field_path=f"{field_prefix}.underlying",
            rule="underlying_matches_instrument",
            message=(
                f"underlying {rec.underlying!r} does not match instrument's "
                f"{instrument_field} {expected!r} in {rec.recommendation_id}"
            ),
        )


def _check_at_least_one_hard_leg(
    rec: Recommendation, *, field_prefix: str
) -> Iterable[ValidationError]:
    """(c) At least one ``invalidation_legs[]`` entry has ``is_hard=True``.

    Defensive cross-check; the schema's per-leg ``allOf`` already forces
    price/time legs to be hard. Catches the all-event-legs case.
    """
    if not any(leg.is_hard for leg in rec.invalidation_legs):
        yield ValidationError(
            field_path=f"{field_prefix}.invalidation_legs",
            rule="at_least_one_hard_invalidation_leg",
            message=(
                f"recommendation {rec.recommendation_id} has no hard (price- or time-based) "
                "invalidation leg; at least one is_hard=True leg is required"
            ),
        )


def _check_recommendation_ids_unique(
    recommendations: tuple[Recommendation, ...],
) -> Iterable[ValidationError]:
    """(d) ``recommendation_id`` is unique within the invocation."""
    seen: set[str] = set()
    for i, rec in enumerate(recommendations):
        if rec.recommendation_id in seen:
            yield ValidationError(
                field_path=f"recommendations[{i}].recommendation_id",
                rule="recommendation_id_unique",
                message=(
                    f"recommendation_id {rec.recommendation_id!r} is duplicated within the "
                    "invocation; IDs must be unique"
                ),
            )
        seen.add(rec.recommendation_id)


def _check_sector_active(
    rec: Recommendation, *, field_prefix: str, active_sectors: frozenset[str]
) -> Iterable[ValidationError]:
    """(e) ``sector`` is a member of ``active_sectors``."""
    if rec.sector not in active_sectors:
        yield ValidationError(
            field_path=f"{field_prefix}.sector",
            rule="sector_not_active",
            message=(
                f"sector {rec.sector!r} is not in active_sectors "
                f"{sorted(active_sectors)!r} for {rec.recommendation_id}"
            ),
        )


def _check_asset_type_feature_disabled(
    rec: Recommendation, *, field_prefix: str
) -> Iterable[ValidationError]:
    """(f) Defense-in-depth for the feature-flag gate.

    The guardrail tool returns ``FAIL`` with reason ``feature_disabled`` for
    instruments disabled by the active portfolio profile (e.g., options on a
    profile that forbids them). A well-behaved analyst would have dropped or
    revised the recommendation; this check catches the case where it didn't.
    """
    result = rec.guardrail_validation_result
    if result.overall != "FAIL":
        return
    note = result.cumulative_impact_note or ""
    rule_text = " ".join(p.rule for p in result.per_rule)
    if _FEATURE_DISABLED_TOKEN not in note and _FEATURE_DISABLED_TOKEN not in rule_text:
        return
    yield ValidationError(
        field_path=f"{field_prefix}.instrument.asset_type",
        rule="asset_type_feature_disabled",
        message=(
            f"recommendation {rec.recommendation_id} carries asset_type "
            f"{rec.instrument.asset_type!r} but guardrail_validation_result.overall=FAIL "
            "with feature_disabled semantics; the analyst should have dropped or revised it"
        ),
    )


def _check_entry_window_not_expired(
    rec: Recommendation,
    *,
    field_prefix: str,
    reference_clock: datetime,
) -> Iterable[ValidationError]:
    """(g) ``entry_window.deadline`` is not before ``reference_clock``.

    Compares against ``output.timestamp`` (passed in via ``reference_clock``)
    so the validator is deterministic — no wall-clock reads.
    """
    if rec.entry_window is None:
        return
    if rec.entry_window.deadline < reference_clock:
        yield ValidationError(
            field_path=f"{field_prefix}.entry_window.deadline",
            rule="entry_window_not_expired",
            message=(
                f"entry_window.deadline {rec.entry_window.deadline.isoformat()} is before "
                f"output.timestamp {reference_clock.isoformat()} for {rec.recommendation_id}"
            ),
        )


def _check_time_horizon_consistency(
    rec: Recommendation,
    *,
    field_prefix: str,
    reference_clock: datetime,
) -> Iterable[ValidationWarning]:
    """(h) Soft check: entry-window distance fits inside ``time_expectation_hours``.

    The deadline + a reasonable resolution window should not exceed the
    declared horizon. Mismatch is a WARNING, not an error — unusual but not
    necessarily wrong (e.g., a ``binary`` decay window before an event with a
    longer holding tail).
    """
    if rec.entry_window is None:
        return
    deadline_hours = (rec.entry_window.deadline - reference_clock).total_seconds() / 3600.0
    if deadline_hours > rec.time_expectation_hours:
        yield ValidationWarning(
            field_path=f"{field_prefix}.time_expectation_hours",
            rule="time_horizon_consistency",
            message=(
                f"entry_window.deadline is ~{deadline_hours:.1f}h after output.timestamp "
                f"but time_expectation_hours is {rec.time_expectation_hours} for "
                f"{rec.recommendation_id}"
            ),
        )


def _check_conviction_band_deviation(
    rec: Recommendation,
    *,
    field_prefix: str,
    bands: dict[int, tuple[float, float]],
) -> Iterable[ValidationWarning]:
    """(i) Soft band check: ``position_size.pct_of_portfolio`` inside the conviction band.

    Per the parent issue's pre-resolved decision E, deviations are surfaced
    as a :class:`ValidationWarning` (rule ``conviction_band_deviation``) and
    never as an error. Recommendations whose conviction level is missing
    from ``bands`` are not flagged — caller controls the table.
    """
    band = bands.get(rec.conviction_level)
    if band is None:
        return
    lower, upper = band
    pct = rec.position_size.pct_of_portfolio
    if pct < lower or pct > upper:
        yield ValidationWarning(
            field_path=f"{field_prefix}.position_size.pct_of_portfolio",
            rule="conviction_band_deviation",
            message=(
                f"conviction {rec.conviction_level} (band {lower}-{upper}%) but "
                f"pct_of_portfolio is {pct} for {rec.recommendation_id}"
            ),
        )


# ---------------------------------------------------------------------------
# Layer-2 (j)-(k) — bracket price coherence vs the fill-collection reference price (ALP-742)
# ---------------------------------------------------------------------------


def _live_equity_close(rec: Recommendation, underlying_prices: Mapping[str, float]) -> float | None:
    """The reference close for *rec*'s underlying, or ``None`` to skip the check.

    Returns ``None`` for a non-equity instrument — option/strategy targets
    reference net P/L, not an underlying price, and options manage their
    protective leg through the continuous monitor rather than a broker bracket —
    or for a ticker absent / non-positive in ``underlying_prices`` (the guardrail
    tool already returns ``UNAVAILABLE`` for unpriced tickers upstream).

    The value is the guardrail library's ``MarketInputs.underlying_prices`` entry
    — the freshest reference price the system holds for the ticker: a live broker
    ``current_price`` for a held name, and for an unheld candidate a fill-collection batch
    live-quote mid when one was captured (ALP-753), else the freshest recorded
    ``ohlcv_bars`` close of any timeframe (an intraday 15min/1h/4h close that
    supersedes the lagging daily close on a fast move, per ALP-747). It is the
    same price substrate the analyst's own inputs are built from, so a coherent
    bracket the analyst draws against its inputs stays coherent here.
    """
    if not isinstance(rec.instrument, InstrumentEquity):
        return None
    live = underlying_prices.get(rec.underlying)
    if live is None or live <= 0:
        return None
    return live


def _protective_stop_price(rec: Recommendation) -> float | None:
    """Return the recommendation's protective-stop trigger price, or ``None``.

    Mirrors the equity OPEN bracket builder
    (``broker_adapter.order_equity._bracket_params``), which takes the *first*
    price-invalidation leg as the bracket's ``stop_loss``. An OTO bracket (no
    price leg — only a hard time leg) returns ``None``.
    """
    for leg in rec.invalidation_legs:
        if leg.type == "price" and isinstance(leg.condition, PriceCondition):
            return float(leg.condition.trigger_price)
    return None


def _bracket_base_price(rec: Recommendation, *, live: float) -> float | None:
    """The price the broker fills the entry against — the ``base_price`` its
    bracket-coherence check uses — or ``None`` when the base is ambiguous.

    * ``market`` and *enter-now* ``limit`` (a ``limit`` with no ``entry_window``)
      fill at the prevailing quote. The marketable-limit rewrite (ALP-738) prices
      an enter-now limit *through the touch* before submission, so the broker's
      base is the live close, not the analyst's resting limit — grounding the
      check on ``live`` stays consistent with the downstream rewrite and with
      ``entry_pricing._preserves_bracket_geometry``.
    * a *patient-retest* ``limit`` (``limit`` with an ``entry_window``) rests at
      its stated ``limit_price`` away from the market; the broker brackets
      against that limit, so the check uses it.
    * ``stop_limit`` (breakout) triggers at ``stop_price`` and fills at
      ``limit_price``; which the broker treats as ``base_price`` is ambiguous and
      the entry is deliberately away from the live quote, so the directional
      check is skipped (``None``). A broker-incoherent breakout bracket still
      fails closed at submission rather than silently filling.
    """
    entry = rec.entry_order
    if entry.type == "stop_limit":
        return None
    if entry.type == "limit" and rec.entry_window is not None:
        # Patient retest — rests at the stated limit (limit_price is required for
        # a limit entry by the EntryOrder validator; fall back defensively).
        return float(entry.limit_price) if entry.limit_price is not None else live
    # market, or enter-now limit (repriced to marketable ≈ the live quote).
    return live


def _check_bracket_directional_coherence(
    rec: Recommendation,
    *,
    field_prefix: str,
    underlying_prices: Mapping[str, float],
) -> Iterable[ValidationError]:
    """(j) Bracket geometry must straddle the broker's base price directionally.

    For a **short** the protective stop sits *above* and the target *below* the
    base price; for a **long** the target sits *above* and the stop *below*.
    A stale price anchor produces a bracket that fails this ordering, which the
    broker rejects as ``stop_loss``/``take_profit`` ``must be >= base_price``
    (the 2026-05-29 JPM-short / CRWD-long cancellations).
    """
    live = _live_equity_close(rec, underlying_prices)
    if live is None:
        return
    base = _bracket_base_price(rec, live=live)
    if base is None:
        return  # stop_limit breakout — base ambiguous; broker still fails closed.
    # ``_live_equity_close`` already narrowed the instrument to equity.
    assert isinstance(rec.instrument, InstrumentEquity)
    target = float(rec.target.price)
    stop = _protective_stop_price(rec)
    is_short = rec.instrument.direction == "short"

    if is_short and target >= base:
        yield ValidationError(
            field_path=f"{field_prefix}.target.price",
            rule="bracket_directional_coherence",
            message=(
                f"short {rec.underlying} ({rec.recommendation_id}): target {target} must be below "
                f"the entry reference {base:.2f} ({rec.underlying} reference price {live:.2f}); "
                "a short profits as price falls"
            ),
        )
    elif not is_short and target <= base:
        yield ValidationError(
            field_path=f"{field_prefix}.target.price",
            rule="bracket_directional_coherence",
            message=(
                f"long {rec.underlying} ({rec.recommendation_id}): target {target} must be above "
                f"the entry reference {base:.2f} ({rec.underlying} reference price {live:.2f}); "
                "a long profits as price rises"
            ),
        )

    if stop is None:
        return
    if is_short and stop <= base:
        yield ValidationError(
            field_path=f"{field_prefix}.invalidation_legs",
            rule="bracket_directional_coherence",
            message=(
                f"short {rec.underlying} ({rec.recommendation_id}): protective stop {stop} must "
                f"be above the entry reference {base:.2f} ({rec.underlying} reference price "
                f"{live:.2f}); a short is stopped out as price rises"
            ),
        )
    elif not is_short and stop >= base:
        yield ValidationError(
            field_path=f"{field_prefix}.invalidation_legs",
            rule="bracket_directional_coherence",
            message=(
                f"long {rec.underlying} ({rec.recommendation_id}): protective stop {stop} must "
                f"be below the entry reference {base:.2f} ({rec.underlying} reference price "
                f"{live:.2f}); a long is stopped out as price falls"
            ),
        )


def _entry_anchor_at_live(rec: Recommendation) -> float | None:
    """The per-share price the analyst sized against, for entries expected to
    fill at the live quote — or ``None`` when the entry intentionally rests away
    from it.

    * ``market`` — the implied anchor is ``dollar_value / quantity`` (a market
      entry fills now, so its sizing price should track the latest close).
    * ``limit`` with no ``entry_window`` — an "enter-now" limit priced through
      the touch (ALP-738); its ``limit_price`` should track the latest close.
    * ``limit`` with an ``entry_window`` (patient retest) and ``stop_limit``
      (breakout) — the analyst deliberately anchors away from the live quote,
      so staleness does not apply; returns ``None``.
    """
    entry = rec.entry_order
    if entry.type == "stop_limit":
        return None
    if entry.type == "limit":
        if rec.entry_window is not None:
            return None
        return float(entry.limit_price) if entry.limit_price is not None else None
    qty = rec.position_size.quantity
    if qty <= 0:
        return None
    return float(rec.position_size.dollar_value) / qty


def _check_reference_price_staleness(
    rec: Recommendation,
    *,
    field_prefix: str,
    underlying_prices: Mapping[str, float],
    tolerance_pct: float,
) -> Iterable[ValidationError]:
    """(k) The analyst's entry anchor must be within tolerance of the reference price.

    Catches calibration drift the directional check can miss: a bracket whose
    target/stop happen to straddle the reference price can still have been *sized*
    against a stale reference, mis-stating notional and risk. Drift beyond
    ``tolerance_pct`` of the fill-collection reference price (a live batch-quote mid when
    available, else the freshest ``ohlcv_bars`` close) is flagged for redraft
    (the recurring 3+-cycle pattern the PM narrative named on 2026-05-29).

    Scoped to equity entries that fill at the live quote (see
    :func:`_entry_anchor_at_live`); patient-retest and breakout entries are
    exempt. A ticker absent from ``underlying_prices`` is skipped.
    """
    live = _live_equity_close(rec, underlying_prices)
    if live is None:
        return
    anchor = _entry_anchor_at_live(rec)
    if anchor is None or anchor <= 0:
        return
    drift_pct = abs(anchor - live) / live * 100.0
    if drift_pct > tolerance_pct:
        yield ValidationError(
            field_path=f"{field_prefix}.position_size",
            rule="reference_price_staleness",
            message=(
                f"{rec.underlying} ({rec.recommendation_id}): {rec.underlying} reference price "
                f"= {live:.2f}; your entry anchor {anchor:.2f} is {drift_pct:.1f}% off (tolerance "
                f"{tolerance_pct}%) — the bracket was sized against a different price. Re-anchor "
                f"entry/target/stop for {rec.underlying} to {live:.2f}."
            ),
        )


# ---------------------------------------------------------------------------
# Layer-3 referential integrity
# ---------------------------------------------------------------------------


def _check_narrative_references(
    narrative: str,
    *,
    field_path: str,
    retrieval_store: RetrievalStore,
) -> Iterable[ValidationError]:
    """Resolve every canonical-prefix ``[XX-N]`` token in ``narrative``.

    Skips non-canonical prefixes (e.g., ``[INV-1]``, ``[ND-M1]``) — those
    don't live in the synthesizer's reference taxonomy and don't belong in
    the retrieval store.

    Also surfaces bare-prefix citations (ALP-521): bracketed tokens whose
    body matches a known ``ReferencePrefix`` value but carries no ``-N``
    index. The retrieval store is keyed by ``<prefix>-<index>`` so a bare
    prefix can never resolve; surfacing it as a ``bare_prefix_citation``
    error lets the corrective-retry path run.
    """
    for match in REF_ID_RE.finditer(narrative):
        ref_id = match.group(1)
        if parse_reference_id(ref_id) is None:
            continue
        if retrieval_store.lookup(ref_id) is None:
            yield ValidationError(
                field_path=field_path,
                rule="unknown_reference",
                message=(
                    f"reference {ref_id!r} cited in {field_path} does not resolve in the "
                    "synthesizer's per-invocation retrieval store"
                ),
            )
    for prefix in find_bare_prefix_citations(narrative):
        yield ValidationError(
            field_path=field_path,
            rule="bare_prefix_citation",
            message=(
                f"bare-prefix citation [{prefix}] in {field_path} carries no index; "
                "the retrieval store is keyed by <prefix>-<index> and cannot resolve "
                f"bare prefixes. Cite a specific brief section like [{prefix}-1]."
            ),
        )


def _check_recommendation_references(
    rec: Recommendation,
    *,
    field_prefix: str,
    retrieval_store: RetrievalStore,
) -> Iterable[ValidationError]:
    """Layer-3 referential resolution for every narrative field on ``rec``."""
    yield from _check_narrative_references(
        rec.thesis_narrative,
        field_path=f"{field_prefix}.thesis_narrative",
        retrieval_store=retrieval_store,
    )
    yield from _check_narrative_references(
        rec.target_rationale,
        field_path=f"{field_prefix}.target_rationale",
        retrieval_store=retrieval_store,
    )
    for j, rationale in enumerate(rec.invalidation_rationale):
        yield from _check_narrative_references(
            rationale.rationale,
            field_path=f"{field_prefix}.invalidation_rationale[{j}].rationale",
            retrieval_store=retrieval_store,
        )
    yield from _check_narrative_references(
        rec.position_size_rationale,
        field_path=f"{field_prefix}.position_size_rationale",
        retrieval_store=retrieval_store,
    )
    if rec.entry_window_rationale is not None:
        yield from _check_narrative_references(
            rec.entry_window_rationale,
            field_path=f"{field_prefix}.entry_window_rationale",
            retrieval_store=retrieval_store,
        )
    yield from _check_narrative_references(
        rec.counterarguments_acknowledged,
        field_path=f"{field_prefix}.counterarguments_acknowledged",
        retrieval_store=retrieval_store,
    )


# ---------------------------------------------------------------------------
# Watchlist-mode checks
# ---------------------------------------------------------------------------


def _check_watchlist_entry(
    entry: WatchlistEntry,
    *,
    field_prefix: str,
    active_sectors: frozenset[str],
    retrieval_store: RetrievalStore,
) -> Iterable[ValidationError]:
    """Layer-2/3 checks for a single :class:`WatchlistEntry`.

    Watchlist entries are recommendation-shape-lite: the validator runs
    only the sector-in-active-set and source-reference resolution checks.
    """
    if entry.sector not in active_sectors:
        yield ValidationError(
            field_path=f"{field_prefix}.sector",
            rule="sector_not_active",
            message=(
                f"watchlist sector {entry.sector!r} is not in active_sectors "
                f"{sorted(active_sectors)!r} for ticker {entry.ticker}"
            ),
        )
    for k, ref_id in enumerate(entry.source_references or ()):
        if parse_reference_id(ref_id) is None:
            continue
        if retrieval_store.lookup(ref_id) is None:
            yield ValidationError(
                field_path=f"{field_prefix}.source_references[{k}]",
                rule="unknown_reference",
                message=(
                    f"watchlist source_reference {ref_id!r} does not resolve in the "
                    "synthesizer's per-invocation retrieval store"
                ),
            )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def validate_analyst_output(
    output: AnalystOutput,
    *,
    retrieval_store: RetrievalStore,
    active_sectors: frozenset[str],
    conviction_bands: dict[int, tuple[float, float]] | None = None,
    underlying_prices: Mapping[str, float] | None = None,
    price_staleness_tolerance_pct: float | None = None,
) -> ValidationResult:
    """Run Layer-2 + Layer-3 checks on *output*.

    Parameters
    ----------
    output:
        The parsed analyst output to validate.
    retrieval_store:
        The per-invocation :class:`RetrievalStore` assembled by the
        synthesizer. Layer 3 resolves every ``[XX-N]`` reference embedded in
        narrative fields against this store.
    active_sectors:
        The active portfolio profile's ``active_sectors`` set (subset of
        ``{"tech", "semis", "financials", "energy"}``). Recommendations
        whose ``sector`` is outside this set are an error.
    conviction_bands:
        Mapping ``conviction_level`` → ``(lower_pct, upper_pct)`` for the
        soft sizing-band warning. Defaults to
        :data:`DEFAULT_CONVICTION_BANDS`.
    underlying_prices:
        Per-ticker freshest recorded ``ohlcv_bars`` close of any timeframe (the
        guardrail library's ``MarketInputs.underlying_prices``; ALP-747 freshens
        it from the lagging daily close to the freshest intraday bar), used by
        the ALP-742 bracket price-coherence and reference-price-staleness checks.
        ``None`` (or a ticker absent from the map) skips those checks —
        production always threads the live close map through from the harness.
    price_staleness_tolerance_pct:
        Maximum tolerated drift between the analyst's implied entry anchor and
        the live close before the recommendation is flagged for redraft.
        Defaults to :data:`DEFAULT_PRICE_STALENESS_TOLERANCE_PCT`.

    Returns
    -------
    ValidationResult
        ``is_valid=True`` iff no errors. Warnings never disqualify.
    """
    bands = conviction_bands if conviction_bands is not None else DEFAULT_CONVICTION_BANDS
    tolerance = (
        price_staleness_tolerance_pct
        if price_staleness_tolerance_pct is not None
        else DEFAULT_PRICE_STALENESS_TOLERANCE_PCT
    )
    errors: list[ValidationError] = []
    warnings: list[ValidationWarning] = []
    if output.mode == "normal" and output.recommendations is not None:
        errors.extend(_check_recommendation_ids_unique(output.recommendations))
        for i, rec in enumerate(output.recommendations):
            field_prefix = f"recommendations[{i}]"
            errors.extend(_check_invalidation_leg_id_pairing(rec, field_prefix=field_prefix))
            errors.extend(_check_underlying_matches_instrument(rec, field_prefix=field_prefix))
            errors.extend(_check_at_least_one_hard_leg(rec, field_prefix=field_prefix))
            errors.extend(
                _check_sector_active(rec, field_prefix=field_prefix, active_sectors=active_sectors)
            )
            errors.extend(_check_asset_type_feature_disabled(rec, field_prefix=field_prefix))
            errors.extend(
                _check_entry_window_not_expired(
                    rec, field_prefix=field_prefix, reference_clock=output.timestamp
                )
            )
            warnings.extend(
                _check_time_horizon_consistency(
                    rec, field_prefix=field_prefix, reference_clock=output.timestamp
                )
            )
            warnings.extend(
                _check_conviction_band_deviation(rec, field_prefix=field_prefix, bands=bands)
            )
            errors.extend(
                _check_recommendation_references(
                    rec, field_prefix=field_prefix, retrieval_store=retrieval_store
                )
            )
            if underlying_prices is not None:
                errors.extend(
                    _check_bracket_directional_coherence(
                        rec, field_prefix=field_prefix, underlying_prices=underlying_prices
                    )
                )
                errors.extend(
                    _check_reference_price_staleness(
                        rec,
                        field_prefix=field_prefix,
                        underlying_prices=underlying_prices,
                        tolerance_pct=tolerance,
                    )
                )
    elif output.mode == "watchlist" and output.watchlist is not None:
        for k, entry in enumerate(output.watchlist):
            errors.extend(
                _check_watchlist_entry(
                    entry,
                    field_prefix=f"watchlist[{k}]",
                    active_sectors=active_sectors,
                    retrieval_store=retrieval_store,
                )
            )
    return ValidationResult(
        errors=tuple(errors),
        warnings=tuple(warnings),
    )
