"""Volatility regime classification — story 02-distillation-layer/09.

Implements the composite four-tier volatility regime label, the
four-state transition machine, the regime-skip emergency trigger, the
universal-broadcast ``OutputBlock`` contract, and the
``current_regime_label`` accessor downstream code uses to read the
current label without re-deriving it.

Reference docs:

- ``docs/design/02-distillation-layer/external.md`` § 4 Persistent state
  and composites — the four-tier ladder semantics and the universal
  context broadcast.
- ``docs/design/02-distillation-layer/threshold-calibration.md``
  § Regime classification boundaries — VIX boundaries, term-structure
  backwardation, VVIX percentile cutoffs.
- ``docs/design/02-distillation-layer/threshold-calibration.md``
  § Regime transition confidence — confirmed-invocations,
  indicator-agreement-min, regime-skip emergency trigger.
- ``docs/design/06-risk-guardrails/regime-adaptation.md`` — how the
  four-tier label maps to guardrail multipliers; ``early-strong`` vs.
  ``early-weak`` vs. ``confirmed`` drive tightening/loosening.
- ``docs/design/06-risk-guardrails/breach-behavior.md`` § Emergency
  invocation trigger — the ``regime_skip_emergency`` consumer.

The module is the single source of truth for the regime label. Every
threshold reaches the classifier as a keyword argument from the loaded
:class:`alphamind.config.models.distillation.DistillationConfig`; no
literals are encoded here. Per the no-magic-numbers audit
(``tests/distillation/test_no_magic_numbers.py``) the module must be
clean of YAML-matching literal values.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.distillation.baselines import _refresh_transaction
from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.output import OutputAudience, OutputBlock
from alphamind.persistence.models import (
    _REGIME_LABELS,
    _TRANSITION_STATES,
    DistillationRegimeState,
)


class RegimeLabel(StrEnum):
    """The four regime labels.

    Each member's value matches an entry of :data:`_REGIME_LABELS` in
    :mod:`alphamind.persistence.models` — the schema CHECK constraint
    accepts exactly these four strings.
    """

    LOW_VOL_COMPRESSION = "low_vol_compression"
    VOL_EXPANSION = "vol_expansion"
    CRISIS_SPIKE = "crisis_spike"
    VOL_NORMALIZATION = "vol_normalization"


# Compile-time guard that the StrEnum and the schema vocabulary stay in
# sync. The schema imports its CHECK strings from this module via
# :data:`_REGIME_LABELS`; this assertion catches drift the other way
# (a label added to the enum without being added to the schema vocabulary).
assert {member.value for member in RegimeLabel} == set(_REGIME_LABELS), (
    "RegimeLabel and persistence._REGIME_LABELS must list the same labels"
)


# ---------------------------------------------------------------------------
# Threshold bundles — mirror the YAML config groups
# ---------------------------------------------------------------------------
#
# The classifier needs all nine ``regime_classification`` thresholds plus
# the two ``regime_transition`` thresholds. Bundling each YAML group into
# a frozen dataclass keeps argument counts manageable and matches the
# Pydantic config shape one-for-one — the orchestrator (story 12) pulls
# the values from :class:`alphamind.config.models.distillation.RegimeClassification`
# and :class:`alphamind.config.models.distillation.RegimeTransition` into
# these dataclasses without re-typing.


@dataclass(frozen=True, slots=True)
class RegimeClassificationThresholds:
    """The nine ``regime_classification`` thresholds.

    Field names mirror :class:`alphamind.config.models.distillation.RegimeClassification`
    one-for-one so the orchestrator can transcribe them directly.
    """

    low_vol_vix_max: float
    normal_vix_min: float
    normal_vix_max: float
    elevated_vix_min: float
    elevated_vix_max: float
    crisis_vix_min: float
    term_structure_backwardation_threshold: float
    vvix_high_percentile: float
    vvix_low_percentile: float


@dataclass(frozen=True, slots=True)
class RegimeTransitionThresholds:
    """The two ``regime_transition`` integer thresholds."""

    confirmed_invocations: int
    indicator_agreement_min: int


@dataclass(frozen=True, slots=True)
class RegimeSnapshot:
    """One invocation's view of the supporting volatility indicators.

    Field summary:

    - ``vix_level`` — current VIX spot level (FRED ``VIXCLS``).
    - ``vx1_minus_vix`` — front-month VIX future minus VIX spot. Positive
      values mean contango; negative values mean backwardation.
    - ``vvix_percentile`` — VVIX percentile rank (0..100) against trailing
      one-year history. Caller computes the rank against
      ``macro_observations``.
    - ``realized_vol_5d`` / ``realized_vol_20d`` — trailing 5-day and
      20-day realized volatility. The 5d-vs-20d comparison is the
      "realized vol direction" indicator: 5d < 20d means declining; 5d >
      20d means rising.

    The dataclass is the contract a caller fulfills before invoking
    :func:`classify_regime`; the classifier itself is pure (no I/O).
    """

    vix_level: float
    vx1_minus_vix: float
    vvix_percentile: float
    realized_vol_5d: float
    realized_vol_20d: float
    vix_trailing_20d_mean: float | None = None
    prior_term_structure_backwardation: bool | None = None


# ---------------------------------------------------------------------------
# VIX-band classification — used for skip-detection per the story Notes
# ---------------------------------------------------------------------------
#
# The four underlying VIX bands per ``threshold-calibration.md`` § Regime
# classification boundaries. Skip detection runs against this band
# classification rather than the four-label ladder per the story's Notes
# section: ``threshold-calibration.md`` examples list VIX-band skips
# ("low-vol → elevated", "normal → crisis"), not four-label skips, and
# the four labels do not map cleanly to VIX bands (``vol_expansion``
# covers both ``normal`` and ``elevated`` VIX bands). Documenting the
# choice here per the story dispatch instruction.


class VixBand(StrEnum):
    """Underlying VIX band used for regime-skip emergency detection."""

    LOW_VOL = "low_vol"
    NORMAL = "normal"
    ELEVATED = "elevated"
    CRISIS = "crisis"


# Ordering of the bands in increasing-volatility direction. Skip
# detection measures the absolute index distance between current and
# prior band in this ladder.
_VIX_BAND_LADDER: tuple[VixBand, ...] = (
    VixBand.LOW_VOL,
    VixBand.NORMAL,
    VixBand.ELEVATED,
    VixBand.CRISIS,
)


# Per the spec ("skips a level" = distance ≥ 2 in the band ladder; an
# adjacent transition (distance == 1) is not a skip). Encoded as a
# named constant rather than a literal so the policy is searchable.
_REGIME_SKIP_BAND_DISTANCE_THRESHOLD: int = 2


def classify_vix_band(
    *,
    vix_level: float,
    low_vol_vix_max: float,
    normal_vix_max: float,
    elevated_vix_max: float,
) -> VixBand:
    """Classify a VIX level into one of the four underlying bands.

    Boundaries are inclusive at the upper end of each band, matching
    the YAML invariant ``regime_low_vol_vix_max == regime_normal_vix_min``
    (and so on). The crisis band has no upper bound.
    """
    if vix_level <= low_vol_vix_max:
        return VixBand.LOW_VOL
    if vix_level <= normal_vix_max:
        return VixBand.NORMAL
    if vix_level <= elevated_vix_max:
        return VixBand.ELEVATED
    return VixBand.CRISIS


def detect_regime_skip_emergency(
    *,
    new_vix_band: VixBand,
    prior_vix_band: VixBand | None,
) -> bool:
    """Return ``True`` when the VIX band has skipped ≥ 2 bands.

    A skip is a band-distance ≥
    :data:`_REGIME_SKIP_BAND_DISTANCE_THRESHOLD` in the ladder
    LOW_VOL → NORMAL → ELEVATED → CRISIS, per
    ``threshold-calibration.md`` § Regime transition confidence
    ("skips a level"). Returns ``False`` on bootstrap (no prior band
    means no transition to evaluate).

    The detector measures absolute distance, so a downward
    crisis → normal jump triggers the same emergency flag as the
    upward low_vol → elevated case — both are large regime
    discontinuities the continuous monitor needs to react to.
    """
    if prior_vix_band is None:
        return False
    new_index = _VIX_BAND_LADDER.index(new_vix_band)
    prior_index = _VIX_BAND_LADDER.index(prior_vix_band)
    return abs(new_index - prior_index) >= _REGIME_SKIP_BAND_DISTANCE_THRESHOLD


# ---------------------------------------------------------------------------
# Definitional cutoffs for the four-tier classifier
# ---------------------------------------------------------------------------
#
# Per the story scope (line 36), the ``vol_normalization`` rule fires when
# the current VIX is below the trailing 20-day mean by at least this many
# points. The cutoff is definitional rather than tunable: per
# ``threshold-calibration.md`` § Where each threshold lives, VIX-point
# offsets that anchor a rule shape live with the rule, not in YAML —
# tightening risks producing a label flicker the analysts and PM aren't
# trained to read. Encoded as a named constant so a reader can verify it
# against the story spec without leaving the source.

# regime classification cutoff per external.md § 4 / story 09 scope —
# VIX point decline below the trailing 20-day mean that qualifies as
# "declining from elevated levels" for the vol_normalization rule.
_VOL_NORMALIZATION_VIX_POINT_DECLINE: float = 5.0


# ---------------------------------------------------------------------------
# Four-tier label classifier
# ---------------------------------------------------------------------------


def classify_regime(
    *,
    snapshot: RegimeSnapshot,
    thresholds: RegimeClassificationThresholds,
) -> RegimeLabel:
    """Classify the current regime into one of the four labels.

    The rule table per ``external.md`` § 4 Persistent state and composites:

    - ``low_vol_compression``: VIX ≤ ``low_vol_vix_max`` AND term
      structure in steep contango (``vx1_minus_vix`` strictly positive)
      AND VVIX percentile ≤ ``vvix_low_percentile`` AND realized vol
      declining (``realized_vol_5d`` < ``realized_vol_20d``).
    - ``crisis_spike``: VIX ≥ ``crisis_vix_min`` AND term structure in
      backwardation (``vx1_minus_vix`` ≤ ``term_structure_backwardation_threshold``)
      AND VVIX percentile ≥ ``vvix_high_percentile``.
    - ``vol_normalization``: VIX declining from elevated levels (current
      < ``vix_trailing_20d_mean`` minus
      :data:`_VOL_NORMALIZATION_VIX_POINT_DECLINE` points) AND term
      structure has returned to contango after recent backwardation AND
      realized vol declining.
    - ``vol_expansion``: VIX in ``[normal_vix_min, elevated_vix_max]``
      AND realized vol rising.
    - Fallback when no rule fires definitively: classify by the
      underlying VIX band alone — see
      :func:`_fallback_label_from_vix_band`.
    """
    if (
        snapshot.vix_level <= thresholds.low_vol_vix_max
        and snapshot.vx1_minus_vix > 0.0
        and snapshot.vvix_percentile <= thresholds.vvix_low_percentile
        and snapshot.realized_vol_5d < snapshot.realized_vol_20d
    ):
        return RegimeLabel.LOW_VOL_COMPRESSION
    if (
        snapshot.vix_level >= thresholds.crisis_vix_min
        and snapshot.vx1_minus_vix <= thresholds.term_structure_backwardation_threshold
        and snapshot.vvix_percentile >= thresholds.vvix_high_percentile
    ):
        return RegimeLabel.CRISIS_SPIKE
    if (
        snapshot.vix_trailing_20d_mean is not None
        and snapshot.prior_term_structure_backwardation
        and snapshot.vix_level
        <= snapshot.vix_trailing_20d_mean - _VOL_NORMALIZATION_VIX_POINT_DECLINE
        and snapshot.vx1_minus_vix > thresholds.term_structure_backwardation_threshold
        and snapshot.realized_vol_5d < snapshot.realized_vol_20d
    ):
        return RegimeLabel.VOL_NORMALIZATION
    if (
        thresholds.normal_vix_min <= snapshot.vix_level <= thresholds.elevated_vix_max
        and snapshot.realized_vol_5d > snapshot.realized_vol_20d
    ):
        return RegimeLabel.VOL_EXPANSION
    return _fallback_label_from_vix_band(
        vix_level=snapshot.vix_level,
        thresholds=thresholds,
    )


# ---------------------------------------------------------------------------
# Fallback path — when no rule fires, classify by the underlying VIX band
# ---------------------------------------------------------------------------
#
# Per the story scope: "when no rule definitively fires, default to whichever
# regime the VIX-band classification alone implies (the VIX boundary segments
# from ``regime_classification`` config)." Documented here so a reader can
# verify it against the spec without leaving the source.


def _fallback_label_from_vix_band(
    *,
    vix_level: float,
    thresholds: RegimeClassificationThresholds,
) -> RegimeLabel:
    """Return the four-tier label implied by ``vix_level`` alone.

    The two mid-volatility VIX bands (``normal`` and ``elevated``)
    collapse onto :attr:`RegimeLabel.VOL_EXPANSION` because the
    four-tier ladder has no separate ``elevated`` label — see
    ``regime-adaptation.md`` § Regime classification mapping (the four
    labels the four-tier ladder names plus the two VIX bands are an
    intentional asymmetry).

    The fallback never produces :attr:`RegimeLabel.VOL_NORMALIZATION` —
    that label requires the trailing-mean and prior-backwardation
    context the snapshot may not carry; if those structural conditions
    were present the primary rule would already have fired.
    """
    if vix_level <= thresholds.low_vol_vix_max:
        return RegimeLabel.LOW_VOL_COMPRESSION
    if vix_level <= thresholds.elevated_vix_max:
        return RegimeLabel.VOL_EXPANSION
    return RegimeLabel.CRISIS_SPIKE


# ---------------------------------------------------------------------------
# Indicator agreement count
# ---------------------------------------------------------------------------


def compute_indicator_agreement_count(
    *,
    snapshot: RegimeSnapshot,
    label: RegimeLabel,
    thresholds: RegimeClassificationThresholds,
) -> int:
    """Count how many of the four indicators agree with ``label``.

    The four indicators are VIX band, term structure shape, VVIX
    percentile, and realized vol direction (5d vs. 20d). Each casts a
    0/1 vote — the count drives the transition state machine
    (≥ ``regime_transition_indicator_agreement_min`` makes a label
    change ``early-strong``; below makes it ``early-weak``).
    """
    realized_declining = snapshot.realized_vol_5d < snapshot.realized_vol_20d
    realized_rising = snapshot.realized_vol_5d > snapshot.realized_vol_20d
    in_low_vol_band = snapshot.vix_level <= thresholds.low_vol_vix_max
    in_mid_band = not in_low_vol_band and snapshot.vix_level <= thresholds.elevated_vix_max
    in_crisis_band = snapshot.vix_level > thresholds.elevated_vix_max
    in_indeterminate_vvix_band = (
        thresholds.vvix_low_percentile < snapshot.vvix_percentile < thresholds.vvix_high_percentile
    )
    backwardation_threshold = thresholds.term_structure_backwardation_threshold

    # Each indicator votes once; ``sum(...)`` over a tuple of booleans
    # counts the agreements because ``bool`` is a subclass of ``int``.
    if label is RegimeLabel.LOW_VOL_COMPRESSION:
        votes = (
            in_low_vol_band,
            snapshot.vx1_minus_vix > backwardation_threshold,
            snapshot.vvix_percentile <= thresholds.vvix_low_percentile,
            realized_declining,
        )
    elif label is RegimeLabel.CRISIS_SPIKE:
        votes = (
            in_crisis_band,
            snapshot.vx1_minus_vix <= backwardation_threshold,
            snapshot.vvix_percentile >= thresholds.vvix_high_percentile,
            realized_rising,
        )
    elif label is RegimeLabel.VOL_EXPANSION:
        votes = (
            in_mid_band,
            snapshot.vx1_minus_vix >= backwardation_threshold,
            in_indeterminate_vvix_band,
            realized_rising,
        )
    else:  # vol_normalization
        votes = (
            in_mid_band,
            snapshot.vx1_minus_vix > backwardation_threshold,
            in_indeterminate_vvix_band,
            realized_declining,
        )
    return sum(votes)


# ---------------------------------------------------------------------------
# Transition state machine
# ---------------------------------------------------------------------------


class TransitionState(StrEnum):
    """The four transition states.

    Each member's value matches an entry of
    :data:`alphamind.persistence.models._TRANSITION_STATES` so the
    schema CHECK constraint accepts the persisted strings.
    """

    STABLE = "stable"
    EARLY_WEAK = "early-weak"
    EARLY_STRONG = "early-strong"
    CONFIRMED = "confirmed"


# Compile-time guard that the StrEnum and the schema vocabulary stay in sync.
assert {member.value for member in TransitionState} == set(_TRANSITION_STATES), (
    "TransitionState and persistence._TRANSITION_STATES must list the same states"
)


def compute_transition_state(
    *,
    new_label: RegimeLabel,
    prior_label: RegimeLabel | None,
    prior_invocations_held: int,
    indicator_agreement_count: int,
    transition_thresholds: RegimeTransitionThresholds,
) -> tuple[TransitionState, int]:
    """Resolve ``(transition_state, invocations_held)`` for a new invocation.

    Per ``threshold-calibration.md`` § Regime transition confidence:

    - ``prior_label is None`` (bootstrap) → ``stable``, ``held = 1``.
    - ``new_label == prior_label`` → ``held = prior_held + 1``. The
      state is ``confirmed`` exactly when the run length first reaches
      ``confirmed_invocations`` (this happens once, on the invocation
      that crosses the threshold), and ``stable`` otherwise —
      including subsequent same-label invocations.
    - ``new_label != prior_label`` → ``held = 1`` (first invocation at
      the new label). The state is ``early-strong`` when
      ``indicator_agreement_count`` ≥ ``indicator_agreement_min``,
      ``early-weak`` otherwise.

    The "confirmed exactly once" semantic is what
    ``regime-adaptation.md`` § Transition mechanics expects: the
    confirmation is an event guardrails react to, not a steady state
    that needs to keep firing on every subsequent invocation.
    """
    if prior_label is None:
        return TransitionState.STABLE, 1
    if new_label is prior_label:
        new_held = prior_invocations_held + 1
        if new_held == transition_thresholds.confirmed_invocations:
            return TransitionState.CONFIRMED, new_held
        return TransitionState.STABLE, new_held
    # Label changed — first invocation at the new label.
    if indicator_agreement_count >= transition_thresholds.indicator_agreement_min:
        return TransitionState.EARLY_STRONG, 1
    return TransitionState.EARLY_WEAK, 1


# ---------------------------------------------------------------------------
# Persistence — refresh the distillation_regime_state row per invocation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RegimeRefreshResult:
    """Outcome of one :func:`refresh_regime_state` call.

    The caller — the orchestrator (story 12) and the universal-broadcast
    assembler — uses this result to build the ``regime.label``
    ``OutputBlock``. ``prior_label`` is ``None`` exclusively in the
    bootstrap case (no prior row) per the story scope; the persisted
    row carries the schema-compatible non-null sentinel (``regime_label``
    itself) but the in-memory contract surfaces ``None`` so consumers
    can distinguish "no transition" from "no-op stable transition".
    """

    regime_label: RegimeLabel
    transition_state: TransitionState
    prior_label: RegimeLabel | None
    invocations_held: int
    indicator_agreement_count: int
    regime_skip_emergency: bool
    snapshot: RegimeSnapshot
    calibration_state: CalibrationState
    bootstrap_reason: str | None


def _select_most_recent_regime_row(
    session: Session,
) -> DistillationRegimeState | None:
    """Return the most recently persisted ``distillation_regime_state`` row.

    The schema indexes ``as_of`` and the table is forward-only append per
    story 03; ordering by ``as_of`` descending and limiting to one row is
    the canonical read pattern.
    """
    return session.execute(
        select(DistillationRegimeState).order_by(DistillationRegimeState.as_of.desc()).limit(1)
    ).scalar_one_or_none()


def current_regime_label(session: Session) -> str:
    """Return the most-recently-persisted regime label.

    Canonical accessor the rest of the codebase uses to read the current
    label without re-deriving it from the underlying indicators. The
    indirection lets future implementations cache the value, switch
    storage layouts, or read from a snapshot view without touching
    callers.

    Raises ``LookupError`` when no row has been persisted yet — the
    universal-broadcast block is the layer's "always emitted" output and
    every downstream consumer (analysts, synthesizer, strategist)
    expects a non-empty label, so a missing row is a genuine error
    rather than a quietly-returned default.
    """
    row = _select_most_recent_regime_row(session)
    if row is None:
        raise LookupError(
            "no distillation_regime_state row exists; "
            "call refresh_regime_state at least once before reading the label"
        )
    return str(row.regime_label)


def refresh_regime_state(
    session: Session,
    *,
    as_of: str,
    snapshot: RegimeSnapshot,
    classification_thresholds: RegimeClassificationThresholds,
    transition_thresholds: RegimeTransitionThresholds,
    calibration_state: CalibrationState = CalibrationState.CALIBRATED,
    bootstrap_reason: str | None = None,
) -> RegimeRefreshResult:
    """Compute the regime label, transition state, and skip flag; append a row.

    Per the story scope this is the orchestration entry point that
    glues :func:`classify_regime`, :func:`compute_indicator_agreement_count`,
    :func:`compute_transition_state`, and :func:`detect_regime_skip_emergency`
    into one Class-B refresh. The new row is appended to
    ``distillation_regime_state`` (forward-only) and the result is
    returned for the universal-broadcast assembler.

    Bootstrap (no prior row): ``prior_label`` is ``None`` in the
    returned result, ``invocations_held = 1``, ``transition_state =
    'stable'``. The persisted row stores the current label as its
    ``prior_label`` (schema requires non-null) — this is a write-side
    sentinel, not a transition signal.

    The ``calibration_state`` argument lets the orchestrator surface a
    degraded read (e.g., VX1 / VVIX series unavailable) without blocking
    the regime block. Per the story dispatch instruction: "If VX1
    (front-month VIX future) is unavailable in ``macro_observations``,
    document the gap and emit ``regime.label`` with degraded
    calibration state — DO NOT block on the missing series."
    """
    label = classify_regime(
        snapshot=snapshot,
        thresholds=classification_thresholds,
    )
    agreement = compute_indicator_agreement_count(
        snapshot=snapshot,
        label=label,
        thresholds=classification_thresholds,
    )

    prior_row = _select_most_recent_regime_row(session)
    prior_label_enum: RegimeLabel | None
    prior_invocations_held = 0
    if prior_row is None:
        prior_label_enum = None
    else:
        prior_label_enum = RegimeLabel(prior_row.regime_label)
        prior_invocations_held = int(prior_row.invocations_held)

    transition_state, invocations_held = compute_transition_state(
        new_label=label,
        prior_label=prior_label_enum,
        prior_invocations_held=prior_invocations_held,
        indicator_agreement_count=agreement,
        transition_thresholds=transition_thresholds,
    )

    if prior_row is None:
        skip_emergency = False
    else:
        new_band = classify_vix_band(
            vix_level=snapshot.vix_level,
            low_vol_vix_max=classification_thresholds.low_vol_vix_max,
            normal_vix_max=classification_thresholds.normal_vix_max,
            elevated_vix_max=classification_thresholds.elevated_vix_max,
        )
        prior_band = classify_vix_band(
            vix_level=float(prior_row.vix_level),
            low_vol_vix_max=classification_thresholds.low_vol_vix_max,
            normal_vix_max=classification_thresholds.normal_vix_max,
            elevated_vix_max=classification_thresholds.elevated_vix_max,
        )
        skip_emergency = detect_regime_skip_emergency(
            new_vix_band=new_band,
            prior_vix_band=prior_band,
        )

    # The schema requires a non-null ``prior_label`` — see story 03's
    # Alembic migration. For bootstrap we persist the current label as
    # the prior so the column is always populated; the in-memory result
    # surfaces ``None`` so callers can distinguish bootstrap from a
    # genuine same-label held state.
    persisted_prior_label = prior_label_enum.value if prior_label_enum is not None else label.value
    ingested_at = datetime.now(UTC).isoformat()
    # Wrap the row write in the framework's fail-closed transaction wrapper
    # so any error during the add/commit rolls back rather than leaving the
    # session in a partially-mutated state.
    with _refresh_transaction(session):
        session.add(
            DistillationRegimeState(
                as_of=as_of,
                regime_label=label.value,
                vix_level=float(snapshot.vix_level),
                term_structure_basis=float(snapshot.vx1_minus_vix),
                vvix_percentile=float(snapshot.vvix_percentile),
                realized_vol=float(snapshot.realized_vol_5d),
                indicator_agreement_count=int(agreement),
                invocations_held=int(invocations_held),
                transition_state=transition_state.value,
                prior_label=persisted_prior_label,
                ingested_at=ingested_at,
            )
        )

    return RegimeRefreshResult(
        regime_label=label,
        transition_state=transition_state,
        prior_label=prior_label_enum,
        invocations_held=invocations_held,
        indicator_agreement_count=agreement,
        regime_skip_emergency=skip_emergency,
        snapshot=snapshot,
        calibration_state=calibration_state,
        bootstrap_reason=bootstrap_reason,
    )


# ---------------------------------------------------------------------------
# Universal-broadcast OutputBlock — ``regime.label``
# ---------------------------------------------------------------------------
#
# Per ``external.md`` § Output format ("Volatility regime classification
# label as universal context — delivered to every agent") and the
# story scope, the regime block carries
# ``audience = OutputAudience.UNIVERSAL_BROADCAST``. The block_id is the
# canonical pin downstream consumers reference.

REGIME_BLOCK_ID = "regime.label"
"""Canonical block_id for the universal-broadcast regime block.

Centralized here so the analyst-facing tests, the synthesizer's
correlation/regime brief assembler (story 11b), and the strategist
input bundle reference the same string.
"""

_UNIVERSAL_BROADCAST_AUDIENCE: frozenset[OutputAudience] = frozenset(
    {OutputAudience.UNIVERSAL_BROADCAST}
)


def assemble_regime_block(
    *,
    result: RegimeRefreshResult,
    freshness_ts: datetime,
) -> OutputBlock:
    """Pack a :class:`RegimeRefreshResult` into the universal-broadcast block.

    The payload includes every field the story scope enumerates:

    - ``regime_label`` (one of the four labels)
    - ``transition_state`` (``stable`` / ``early-weak`` / ``early-strong``
      / ``confirmed``)
    - ``prior_label`` (``None`` on bootstrap; the prior label otherwise)
    - ``invocations_held``
    - ``regime_skip_emergency`` boolean
    - ``indicator_agreement_count``
    - the supporting indicator snapshot (``vix_level``,
      ``term_structure_basis``, ``vvix_percentile``,
      ``realized_vol_5d``, plus ``realized_vol_20d`` so the
      direction-of-change input the count uses is visible to consumers)

    Calibration state on the block is whatever the refresh result
    carries — typically ``CALIBRATED``, but ``BOOTSTRAP`` when an
    underlying series (e.g. VX1) was unavailable per the story
    dispatch instruction.

    The block carries no anomaly flags and no ``regime_context`` — the
    block IS the regime context for every other block, so duplicating
    the label into its own annotation would be circular.
    """
    payload: dict[str, object] = {
        "regime_label": result.regime_label.value,
        "transition_state": result.transition_state.value,
        "prior_label": (result.prior_label.value if result.prior_label is not None else None),
        "invocations_held": result.invocations_held,
        "indicator_agreement_count": result.indicator_agreement_count,
        "regime_skip_emergency": result.regime_skip_emergency,
        "vix_level": float(result.snapshot.vix_level),
        "term_structure_basis": float(result.snapshot.vx1_minus_vix),
        "vvix_percentile": float(result.snapshot.vvix_percentile),
        "realized_vol_5d": float(result.snapshot.realized_vol_5d),
        "realized_vol_20d": float(result.snapshot.realized_vol_20d),
    }
    return OutputBlock(
        block_id=REGIME_BLOCK_ID,
        audience=_UNIVERSAL_BROADCAST_AUDIENCE,
        freshness_ts=freshness_ts,
        calibration_state=result.calibration_state,
        bootstrap_reason=result.bootstrap_reason,
        payload=payload,
        anomaly_flags=(),
        regime_context=None,
    )
