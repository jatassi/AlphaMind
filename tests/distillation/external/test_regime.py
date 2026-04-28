"""Tests for the volatility regime classifier — story 02-distillation-layer/09.

Covers the four-tier label classifier (low_vol_compression / vol_expansion /
crisis_spike / vol_normalization), the four-state transition machine
(stable / early-weak / early-strong / confirmed), the indicator-agreement
count, the regime-skip emergency trigger, the universal-broadcast
``OutputBlock`` contract, and the ``current_regime_label`` accessor.

The tests interleave pure-function checks (no DB) with end-to-end checks
that exercise persistence via an in-memory SQLite engine, mirroring the
test layout in ``test_q6_macro.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.output import OutputAudience
from alphamind.distillation.regime import (
    RegimeClassificationThresholds,
    RegimeLabel,
    RegimeRefreshResult,
    RegimeSnapshot,
    RegimeTransitionThresholds,
    TransitionState,
    VixBand,
    assemble_regime_block,
    classify_regime,
    classify_vix_band,
    compute_indicator_agreement_count,
    compute_transition_state,
    current_regime_label,
    detect_regime_skip_emergency,
    refresh_regime_state,
)
from alphamind.persistence.models import Base
from alphamind.persistence.session import make_engine, make_session_factory

# ---------------------------------------------------------------------------
# Shared fixtures and constants
# ---------------------------------------------------------------------------

# Test-side constants mirror the YAML defaults the orchestrator would pass in
# production. The no-magic-numbers audit only scans ``src/alphamind/distillation/``
# so naming them here is fine.
LOW_VOL_VIX_MAX = 14.0
NORMAL_VIX_MIN = 14.0
NORMAL_VIX_MAX = 22.0
ELEVATED_VIX_MIN = 22.0
ELEVATED_VIX_MAX = 35.0
CRISIS_VIX_MIN = 35.0
TERM_STRUCTURE_BACKWARDATION_THRESHOLD = 0.0
VVIX_HIGH_PERCENTILE = 80.0
VVIX_LOW_PERCENTILE = 30.0
TRANSITION_CONFIRMED_INVOCATIONS = 2
TRANSITION_INDICATOR_AGREEMENT_MIN = 3


@pytest.fixture()
def engine() -> Iterator[Engine]:
    """Per-test in-memory SQLite engine with the full distillation schema."""
    eng = make_engine(":memory:")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    factory = make_session_factory(engine)
    with factory() as sess:
        yield sess


AS_OF = "2026-04-25T14:30:00Z"
FRESHNESS_TS = datetime(2026, 4, 25, 14, 30, tzinfo=UTC)


def _classification_thresholds() -> RegimeClassificationThresholds:
    """Default thresholds for ``classify_regime`` — keeps test bodies focused."""
    return RegimeClassificationThresholds(
        low_vol_vix_max=LOW_VOL_VIX_MAX,
        normal_vix_min=NORMAL_VIX_MIN,
        normal_vix_max=NORMAL_VIX_MAX,
        elevated_vix_min=ELEVATED_VIX_MIN,
        elevated_vix_max=ELEVATED_VIX_MAX,
        crisis_vix_min=CRISIS_VIX_MIN,
        term_structure_backwardation_threshold=(TERM_STRUCTURE_BACKWARDATION_THRESHOLD),
        vvix_high_percentile=VVIX_HIGH_PERCENTILE,
        vvix_low_percentile=VVIX_LOW_PERCENTILE,
    )


def _transition_thresholds() -> RegimeTransitionThresholds:
    return RegimeTransitionThresholds(
        confirmed_invocations=TRANSITION_CONFIRMED_INVOCATIONS,
        indicator_agreement_min=TRANSITION_INDICATOR_AGREEMENT_MIN,
    )


# ---------------------------------------------------------------------------
# Label classification — the four labels under their canonical conditions
# ---------------------------------------------------------------------------


class TestRegimeLabelClassification:
    """``classify_regime`` produces one of the four regime labels.

    Inputs are the supporting-indicator snapshot plus the Class A
    thresholds; the four labels are ``low_vol_compression``,
    ``vol_expansion``, ``crisis_spike``, ``vol_normalization``.
    """

    def test_low_vol_compression_under_canonical_conditions(self) -> None:
        # VIX low + steep contango + low VVIX + declining realized vol.
        snapshot = RegimeSnapshot(
            vix_level=12.0,
            vx1_minus_vix=1.5,  # steep contango
            vvix_percentile=20.0,  # below low cutoff
            realized_vol_5d=8.0,
            realized_vol_20d=10.0,  # declining
        )
        assert (
            classify_regime(snapshot=snapshot, thresholds=_classification_thresholds())
            is RegimeLabel.LOW_VOL_COMPRESSION
        )

    def test_crisis_spike_under_canonical_conditions(self) -> None:
        # VIX > crisis floor + backwardation + VVIX percentile high.
        snapshot = RegimeSnapshot(
            vix_level=42.0,  # > 35 crisis floor
            vx1_minus_vix=-0.5,  # backwardation
            vvix_percentile=90.0,  # above high cutoff
            realized_vol_5d=30.0,
            realized_vol_20d=20.0,  # rising
        )
        assert (
            classify_regime(snapshot=snapshot, thresholds=_classification_thresholds())
            is RegimeLabel.CRISIS_SPIKE
        )

    def test_vol_expansion_when_vix_in_normal_or_elevated_band_and_realized_rising(
        self,
    ) -> None:
        # VIX inside the normal-or-elevated VIX band; term-structure
        # flattening (small positive basis); realized vol rising.
        snapshot = RegimeSnapshot(
            vix_level=18.0,  # in [14, 22] band
            vx1_minus_vix=0.2,  # flattening (slightly positive)
            vvix_percentile=55.0,  # mid-range
            realized_vol_5d=12.0,
            realized_vol_20d=10.0,  # rising
        )
        assert (
            classify_regime(snapshot=snapshot, thresholds=_classification_thresholds())
            is RegimeLabel.VOL_EXPANSION
        )

    def test_vol_normalization_when_vix_declining_from_elevated_with_contango_return(
        self,
    ) -> None:
        # VIX declining from elevated levels (current 24 < trailing 20d
        # mean 30 by ≥ 5); term structure returned to contango after
        # recent backwardation; realized vol declining.
        snapshot = RegimeSnapshot(
            vix_level=24.0,
            vx1_minus_vix=0.4,  # contango
            vvix_percentile=55.0,
            realized_vol_5d=12.0,
            realized_vol_20d=18.0,  # declining
            vix_trailing_20d_mean=30.0,
            prior_term_structure_backwardation=True,
        )
        assert (
            classify_regime(snapshot=snapshot, thresholds=_classification_thresholds())
            is RegimeLabel.VOL_NORMALIZATION
        )

    def test_fallback_classifies_by_vix_band_when_no_rule_fires(self) -> None:
        # Mid VIX 18 (vol_expansion band) but realized vol flat — neither
        # vol_expansion (needs rising) nor vol_normalization (needs prior
        # backwardation) fires. Fallback should return vol_expansion via
        # the underlying VIX-band classification.
        snapshot = RegimeSnapshot(
            vix_level=18.0,
            vx1_minus_vix=0.5,  # contango
            vvix_percentile=55.0,
            realized_vol_5d=10.0,
            realized_vol_20d=10.0,  # flat — neither rising nor declining
        )
        assert (
            classify_regime(snapshot=snapshot, thresholds=_classification_thresholds())
            is RegimeLabel.VOL_EXPANSION
        )


# ---------------------------------------------------------------------------
# Indicator agreement count — votes per indicator
# ---------------------------------------------------------------------------


class TestIndicatorAgreementCount:
    """``compute_indicator_agreement_count`` returns 0..4.

    Each of the four indicators (VIX band, term structure shape, VVIX
    percentile, realized vol direction) votes 0 or 1 based on whether
    its value is consistent with the resolved label.
    """

    def test_count_is_four_when_all_indicators_agree_with_low_vol(self) -> None:
        # Canonical low-vol-compression snapshot — all four indicators
        # consistent with the label.
        snapshot = RegimeSnapshot(
            vix_level=12.0,
            vx1_minus_vix=1.5,
            vvix_percentile=20.0,
            realized_vol_5d=8.0,
            realized_vol_20d=10.0,
        )
        count = compute_indicator_agreement_count(
            snapshot=snapshot,
            label=RegimeLabel.LOW_VOL_COMPRESSION,
            thresholds=_classification_thresholds(),
        )
        assert count == 4

    def test_count_is_two_when_only_two_indicators_agree_with_low_vol(self) -> None:
        # Low-vol label nominally chosen, but VVIX is high (disagrees) and
        # realized vol is rising (disagrees). Only VIX band and term
        # structure remain consistent — count is 2.
        snapshot = RegimeSnapshot(
            vix_level=12.0,  # agree
            vx1_minus_vix=1.5,  # agree (contango)
            vvix_percentile=85.0,  # disagree — above high cutoff
            realized_vol_5d=11.0,
            realized_vol_20d=10.0,  # disagree — rising
        )
        count = compute_indicator_agreement_count(
            snapshot=snapshot,
            label=RegimeLabel.LOW_VOL_COMPRESSION,
            thresholds=_classification_thresholds(),
        )
        assert count == 2

    def test_count_for_crisis_label_uses_crisis_consistency(self) -> None:
        # Crisis-spike snapshot — all four indicators consistent.
        snapshot = RegimeSnapshot(
            vix_level=42.0,
            vx1_minus_vix=-0.5,
            vvix_percentile=90.0,
            realized_vol_5d=30.0,
            realized_vol_20d=20.0,
        )
        count = compute_indicator_agreement_count(
            snapshot=snapshot,
            label=RegimeLabel.CRISIS_SPIKE,
            thresholds=_classification_thresholds(),
        )
        assert count == 4

    def test_count_for_vol_expansion_uses_mid_vix_band_consistency(self) -> None:
        # Vol-expansion snapshot — VIX in normal band, term structure
        # flattening (≥ 0), VVIX mid-range, realized vol rising.
        snapshot = RegimeSnapshot(
            vix_level=18.0,
            vx1_minus_vix=0.2,
            vvix_percentile=55.0,
            realized_vol_5d=12.0,
            realized_vol_20d=10.0,
        )
        count = compute_indicator_agreement_count(
            snapshot=snapshot,
            label=RegimeLabel.VOL_EXPANSION,
            thresholds=_classification_thresholds(),
        )
        assert count == 4

    def test_count_for_vol_normalization_uses_declining_consistency(self) -> None:
        # Vol-normalization snapshot — VIX in elevated band, contango,
        # mid VVIX, declining realized vol.
        snapshot = RegimeSnapshot(
            vix_level=24.0,
            vx1_minus_vix=0.4,
            vvix_percentile=55.0,
            realized_vol_5d=12.0,
            realized_vol_20d=18.0,
        )
        count = compute_indicator_agreement_count(
            snapshot=snapshot,
            label=RegimeLabel.VOL_NORMALIZATION,
            thresholds=_classification_thresholds(),
        )
        assert count == 4


# ---------------------------------------------------------------------------
# Transition state machine
# ---------------------------------------------------------------------------


class TestTransitionStateMachine:
    """``compute_transition_state`` resolves ``(transition_state, invocations_held)``.

    Per ``threshold-calibration.md`` § Regime transition confidence:

    - Same label as prior → ``stable``, increment ``invocations_held``.
    - Label change with ``indicator_agreement_count >= 3`` →
      ``early-strong``, ``invocations_held = 1``.
    - Label change with ``indicator_agreement_count < 3`` →
      ``early-weak``, ``invocations_held = 1``.
    - Held for ``regime_transition_confirmed_invocations`` consecutive
      invocations at the new label → ``confirmed``.
    - Bootstrap (``prior_label`` is ``None``) → ``stable``,
      ``invocations_held = 1``.
    """

    def test_stable_when_label_unchanged(self) -> None:
        state, held = compute_transition_state(
            new_label=RegimeLabel.LOW_VOL_COMPRESSION,
            prior_label=RegimeLabel.LOW_VOL_COMPRESSION,
            prior_invocations_held=5,
            indicator_agreement_count=4,
            transition_thresholds=_transition_thresholds(),
        )
        assert state is TransitionState.STABLE
        assert held == 6

    def test_early_strong_when_label_changes_with_high_agreement(self) -> None:
        state, held = compute_transition_state(
            new_label=RegimeLabel.VOL_EXPANSION,
            prior_label=RegimeLabel.LOW_VOL_COMPRESSION,
            prior_invocations_held=10,
            indicator_agreement_count=3,
            transition_thresholds=_transition_thresholds(),
        )
        assert state is TransitionState.EARLY_STRONG
        assert held == 1

    def test_early_weak_when_label_changes_with_low_agreement(self) -> None:
        state, held = compute_transition_state(
            new_label=RegimeLabel.VOL_EXPANSION,
            prior_label=RegimeLabel.LOW_VOL_COMPRESSION,
            prior_invocations_held=10,
            indicator_agreement_count=2,
            transition_thresholds=_transition_thresholds(),
        )
        assert state is TransitionState.EARLY_WEAK
        assert held == 1

    def test_confirmed_after_held_for_required_invocations(self) -> None:
        # The new label has held for 1 invocation; the next invocation at
        # the same label increments ``invocations_held`` to 2 — the
        # configured ``regime_transition_confirmed_invocations`` — so the
        # transition state confirms.
        state, held = compute_transition_state(
            new_label=RegimeLabel.VOL_EXPANSION,
            prior_label=RegimeLabel.VOL_EXPANSION,
            prior_invocations_held=1,  # this invocation makes it 2
            indicator_agreement_count=4,
            transition_thresholds=_transition_thresholds(),
        )
        assert state is TransitionState.CONFIRMED
        assert held == 2

    def test_stable_after_confirmation_invocation(self) -> None:
        # After the confirmed invocation, subsequent same-label
        # invocations report ``stable`` (per the spec: "transition_state
        # = 'stable'" when label is unchanged) but keep incrementing
        # ``invocations_held``.
        state, held = compute_transition_state(
            new_label=RegimeLabel.VOL_EXPANSION,
            prior_label=RegimeLabel.VOL_EXPANSION,
            prior_invocations_held=2,
            indicator_agreement_count=4,
            transition_thresholds=_transition_thresholds(),
        )
        assert state is TransitionState.STABLE
        assert held == 3

    def test_bootstrap_first_invocation_returns_stable_with_held_one(
        self,
    ) -> None:
        # No prior row: this is the bootstrap case. Per the spec:
        # ``prior_label = None`` ⇒ ``transition_state = 'stable'`` and
        # ``invocations_held = 1``.
        state, held = compute_transition_state(
            new_label=RegimeLabel.LOW_VOL_COMPRESSION,
            prior_label=None,
            prior_invocations_held=0,
            indicator_agreement_count=4,
            transition_thresholds=_transition_thresholds(),
        )
        assert state is TransitionState.STABLE
        assert held == 1


# ---------------------------------------------------------------------------
# VIX-band classifier and regime-skip emergency trigger
# ---------------------------------------------------------------------------


def _vix_band_kwargs() -> dict[str, float]:
    return {
        "low_vol_vix_max": LOW_VOL_VIX_MAX,
        "normal_vix_max": NORMAL_VIX_MAX,
        "elevated_vix_max": ELEVATED_VIX_MAX,
    }


class TestVixBandClassifier:
    """``classify_vix_band`` partitions VIX levels into the four bands."""

    def test_low_vol_band_at_or_below_low_vol_vix_max(self) -> None:
        assert classify_vix_band(vix_level=12.0, **_vix_band_kwargs()) is VixBand.LOW_VOL

    def test_normal_band_between_low_vol_and_normal_max(self) -> None:
        assert classify_vix_band(vix_level=18.0, **_vix_band_kwargs()) is VixBand.NORMAL

    def test_elevated_band_between_normal_max_and_elevated_max(self) -> None:
        assert classify_vix_band(vix_level=28.0, **_vix_band_kwargs()) is VixBand.ELEVATED

    def test_crisis_band_above_elevated_max(self) -> None:
        assert classify_vix_band(vix_level=42.0, **_vix_band_kwargs()) is VixBand.CRISIS


class TestRegimeSkipEmergencyDetection:
    """``detect_regime_skip_emergency`` flags VIX-band skips of ≥ 2 positions.

    Skip detection runs against the VIX-band classification rather than
    the four-label ladder per the story's Notes section: the four labels
    don't map cleanly to VIX bands, while the spec's examples in
    ``threshold-calibration.md`` ("low-vol → elevated", "normal → crisis")
    do work directly against the bands.
    """

    def test_skip_fires_on_two_band_jump(self) -> None:
        # low_vol → elevated jumps two band positions.
        assert (
            detect_regime_skip_emergency(
                new_vix_band=VixBand.ELEVATED,
                prior_vix_band=VixBand.LOW_VOL,
            )
            is True
        )

    def test_skip_fires_on_three_band_jump(self) -> None:
        # low_vol → crisis jumps three band positions.
        assert (
            detect_regime_skip_emergency(
                new_vix_band=VixBand.CRISIS,
                prior_vix_band=VixBand.LOW_VOL,
            )
            is True
        )

    def test_skip_does_not_fire_on_adjacent_band_transition(self) -> None:
        # low_vol → normal is a single-band step — not a skip.
        assert (
            detect_regime_skip_emergency(
                new_vix_band=VixBand.NORMAL,
                prior_vix_band=VixBand.LOW_VOL,
            )
            is False
        )

    def test_skip_does_not_fire_when_band_unchanged(self) -> None:
        assert (
            detect_regime_skip_emergency(
                new_vix_band=VixBand.NORMAL,
                prior_vix_band=VixBand.NORMAL,
            )
            is False
        )

    def test_skip_does_not_fire_when_no_prior_band(self) -> None:
        # Bootstrap: no prior band means no transition to evaluate.
        assert (
            detect_regime_skip_emergency(
                new_vix_band=VixBand.CRISIS,
                prior_vix_band=None,
            )
            is False
        )

    def test_skip_fires_on_downward_two_band_jump(self) -> None:
        # crisis → normal is also a two-band jump (in the loosening
        # direction). The skip detector measures absolute distance.
        assert (
            detect_regime_skip_emergency(
                new_vix_band=VixBand.NORMAL,
                prior_vix_band=VixBand.CRISIS,
            )
            is True
        )


# ---------------------------------------------------------------------------
# Persistence — refresh_regime_state and current_regime_label
# ---------------------------------------------------------------------------


# Test bodies pass the threshold bundles explicitly via
# ``classification_thresholds=_classification_thresholds()`` and
# ``transition_thresholds=_transition_thresholds()`` rather than spreading
# a dict — mypy can't infer the per-key type from a heterogeneous dict
# spread, so the explicit form keeps the type checker happy.


class TestRefreshRegimeStateBootstrap:
    """First invocation has no prior row — bootstrap path.

    Per the story scope: "Bootstrap case: first invocation with no prior
    ``distillation_regime_state`` row → ``prior_label = None``,
    ``invocations_held = 1``, ``transition_state = 'stable'``. No
    transition without a prior."
    """

    def test_first_invocation_writes_stable_with_held_one_and_no_prior(
        self, session: Session
    ) -> None:
        snapshot = RegimeSnapshot(
            vix_level=12.0,
            vx1_minus_vix=1.5,
            vvix_percentile=20.0,
            realized_vol_5d=8.0,
            realized_vol_20d=10.0,
        )
        result = refresh_regime_state(
            session,
            as_of=AS_OF,
            snapshot=snapshot,
            classification_thresholds=_classification_thresholds(),
            transition_thresholds=_transition_thresholds(),
        )
        assert result.regime_label is RegimeLabel.LOW_VOL_COMPRESSION
        assert result.transition_state is TransitionState.STABLE
        assert result.prior_label is None
        assert result.invocations_held == 1
        assert result.regime_skip_emergency is False

        # current_regime_label sees the freshly written row.
        assert current_regime_label(session) == "low_vol_compression"


class TestRefreshRegimeStateConsecutive:
    """Multi-invocation persistence behavior — runs and label changes."""

    def test_invocations_held_increments_across_same_label_invocations(
        self, session: Session
    ) -> None:
        snapshot = RegimeSnapshot(
            vix_level=12.0,
            vx1_minus_vix=1.5,
            vvix_percentile=20.0,
            realized_vol_5d=8.0,
            realized_vol_20d=10.0,
        )
        # First invocation — bootstrap.
        first = refresh_regime_state(
            session,
            as_of="2026-04-25T13:30:00Z",
            snapshot=snapshot,
            classification_thresholds=_classification_thresholds(),
            transition_thresholds=_transition_thresholds(),
        )
        assert first.invocations_held == 1

        # Second invocation at same label — confirmed (held == 2 hits the
        # threshold).
        second = refresh_regime_state(
            session,
            as_of="2026-04-25T17:30:00Z",
            snapshot=snapshot,
            classification_thresholds=_classification_thresholds(),
            transition_thresholds=_transition_thresholds(),
        )
        assert second.regime_label is RegimeLabel.LOW_VOL_COMPRESSION
        assert second.invocations_held == 2
        assert second.transition_state is TransitionState.CONFIRMED
        assert second.prior_label is RegimeLabel.LOW_VOL_COMPRESSION

        # Third invocation at same label — stable; held increments to 3.
        third = refresh_regime_state(
            session,
            as_of="2026-04-25T21:30:00Z",
            snapshot=snapshot,
            classification_thresholds=_classification_thresholds(),
            transition_thresholds=_transition_thresholds(),
        )
        assert third.invocations_held == 3
        assert third.transition_state is TransitionState.STABLE

    def test_label_change_resets_invocations_held_and_emits_early_strong(
        self, session: Session
    ) -> None:
        # Start in low_vol; flip to crisis.
        low_vol = RegimeSnapshot(
            vix_level=12.0,
            vx1_minus_vix=1.5,
            vvix_percentile=20.0,
            realized_vol_5d=8.0,
            realized_vol_20d=10.0,
        )
        crisis = RegimeSnapshot(
            vix_level=42.0,
            vx1_minus_vix=-0.5,
            vvix_percentile=90.0,
            realized_vol_5d=30.0,
            realized_vol_20d=20.0,
        )
        refresh_regime_state(
            session,
            as_of="2026-04-25T13:30:00Z",
            snapshot=low_vol,
            classification_thresholds=_classification_thresholds(),
            transition_thresholds=_transition_thresholds(),
        )
        result = refresh_regime_state(
            session,
            as_of="2026-04-25T17:30:00Z",
            snapshot=crisis,
            classification_thresholds=_classification_thresholds(),
            transition_thresholds=_transition_thresholds(),
        )
        assert result.regime_label is RegimeLabel.CRISIS_SPIKE
        assert result.prior_label is RegimeLabel.LOW_VOL_COMPRESSION
        assert result.invocations_held == 1
        assert result.transition_state is TransitionState.EARLY_STRONG

    def test_regime_skip_emergency_fires_on_two_band_jump(self, session: Session) -> None:
        # low_vol → elevated jumps two band positions → skip emergency.
        low_vol = RegimeSnapshot(
            vix_level=12.0,
            vx1_minus_vix=1.5,
            vvix_percentile=20.0,
            realized_vol_5d=8.0,
            realized_vol_20d=10.0,
        )
        elevated = RegimeSnapshot(
            vix_level=28.0,
            vx1_minus_vix=0.1,
            vvix_percentile=70.0,
            realized_vol_5d=18.0,
            realized_vol_20d=12.0,
        )
        refresh_regime_state(
            session,
            as_of="2026-04-25T13:30:00Z",
            snapshot=low_vol,
            classification_thresholds=_classification_thresholds(),
            transition_thresholds=_transition_thresholds(),
        )
        result = refresh_regime_state(
            session,
            as_of="2026-04-25T17:30:00Z",
            snapshot=elevated,
            classification_thresholds=_classification_thresholds(),
            transition_thresholds=_transition_thresholds(),
        )
        assert result.regime_skip_emergency is True

    def test_no_skip_emergency_on_adjacent_band_transition(self, session: Session) -> None:
        # low_vol → normal is one band — not a skip.
        low_vol = RegimeSnapshot(
            vix_level=12.0,
            vx1_minus_vix=1.5,
            vvix_percentile=20.0,
            realized_vol_5d=8.0,
            realized_vol_20d=10.0,
        )
        normal = RegimeSnapshot(
            vix_level=18.0,
            vx1_minus_vix=0.2,
            vvix_percentile=55.0,
            realized_vol_5d=12.0,
            realized_vol_20d=10.0,
        )
        refresh_regime_state(
            session,
            as_of="2026-04-25T13:30:00Z",
            snapshot=low_vol,
            classification_thresholds=_classification_thresholds(),
            transition_thresholds=_transition_thresholds(),
        )
        result = refresh_regime_state(
            session,
            as_of="2026-04-25T17:30:00Z",
            snapshot=normal,
            classification_thresholds=_classification_thresholds(),
            transition_thresholds=_transition_thresholds(),
        )
        assert result.regime_skip_emergency is False

    def test_current_regime_label_returns_most_recent_label(self, session: Session) -> None:
        low_vol = RegimeSnapshot(
            vix_level=12.0,
            vx1_minus_vix=1.5,
            vvix_percentile=20.0,
            realized_vol_5d=8.0,
            realized_vol_20d=10.0,
        )
        crisis = RegimeSnapshot(
            vix_level=42.0,
            vx1_minus_vix=-0.5,
            vvix_percentile=90.0,
            realized_vol_5d=30.0,
            realized_vol_20d=20.0,
        )
        refresh_regime_state(
            session,
            as_of="2026-04-25T13:30:00Z",
            snapshot=low_vol,
            classification_thresholds=_classification_thresholds(),
            transition_thresholds=_transition_thresholds(),
        )
        refresh_regime_state(
            session,
            as_of="2026-04-25T17:30:00Z",
            snapshot=crisis,
            classification_thresholds=_classification_thresholds(),
            transition_thresholds=_transition_thresholds(),
        )
        assert current_regime_label(session) == "crisis_spike"

    def test_current_regime_label_raises_when_no_row_exists(self, session: Session) -> None:
        with pytest.raises(LookupError):
            current_regime_label(session)


# ---------------------------------------------------------------------------
# Universal broadcast OutputBlock
# ---------------------------------------------------------------------------


def _result_for_block(
    *,
    label: RegimeLabel = RegimeLabel.LOW_VOL_COMPRESSION,
    transition_state: TransitionState = TransitionState.STABLE,
    prior_label: RegimeLabel | None = None,
    invocations_held: int = 1,
    indicator_agreement_count: int = 4,
    regime_skip_emergency: bool = False,
    calibration_state: CalibrationState = CalibrationState.CALIBRATED,
    bootstrap_reason: str | None = None,
) -> RegimeRefreshResult:
    snapshot = RegimeSnapshot(
        vix_level=12.0,
        vx1_minus_vix=1.5,
        vvix_percentile=20.0,
        realized_vol_5d=8.0,
        realized_vol_20d=10.0,
    )
    return RegimeRefreshResult(
        regime_label=label,
        transition_state=transition_state,
        prior_label=prior_label,
        invocations_held=invocations_held,
        indicator_agreement_count=indicator_agreement_count,
        regime_skip_emergency=regime_skip_emergency,
        snapshot=snapshot,
        calibration_state=calibration_state,
        bootstrap_reason=bootstrap_reason,
    )


class TestRegimeOutputBlock:
    """``assemble_regime_block`` produces the universal-broadcast block.

    Per the story scope: ``block_id = "regime.label"``, ``audience =
    OutputAudience.UNIVERSAL_BROADCAST``, payload includes
    ``regime_label``, ``transition_state``, ``prior_label``,
    ``invocations_held``, ``regime_skip_emergency``,
    ``indicator_agreement_count``, and the supporting indicator
    snapshot.
    """

    def test_block_id_is_regime_dot_label(self) -> None:
        block = assemble_regime_block(
            result=_result_for_block(),
            freshness_ts=FRESHNESS_TS,
        )
        assert block.block_id == "regime.label"

    def test_block_audience_is_universal_broadcast(self) -> None:
        block = assemble_regime_block(
            result=_result_for_block(),
            freshness_ts=FRESHNESS_TS,
        )
        assert block.audience == frozenset({OutputAudience.UNIVERSAL_BROADCAST})

    def test_payload_carries_documented_fields(self) -> None:
        block = assemble_regime_block(
            result=_result_for_block(
                label=RegimeLabel.VOL_EXPANSION,
                transition_state=TransitionState.EARLY_STRONG,
                prior_label=RegimeLabel.LOW_VOL_COMPRESSION,
                invocations_held=1,
                indicator_agreement_count=3,
                regime_skip_emergency=True,
            ),
            freshness_ts=FRESHNESS_TS,
        )
        assert block.payload["regime_label"] == "vol_expansion"
        assert block.payload["transition_state"] == "early-strong"
        assert block.payload["prior_label"] == "low_vol_compression"
        assert block.payload["invocations_held"] == 1
        assert block.payload["indicator_agreement_count"] == 3
        assert block.payload["regime_skip_emergency"] is True
        # Supporting indicator snapshot is delivered alongside.
        assert "vix_level" in block.payload
        assert "term_structure_basis" in block.payload
        assert "vvix_percentile" in block.payload
        assert "realized_vol_5d" in block.payload

    def test_payload_prior_label_is_none_on_bootstrap(self) -> None:
        block = assemble_regime_block(
            result=_result_for_block(prior_label=None),
            freshness_ts=FRESHNESS_TS,
        )
        assert block.payload["prior_label"] is None

    def test_block_carries_calibration_state_through(self) -> None:
        block = assemble_regime_block(
            result=_result_for_block(
                calibration_state=CalibrationState.BOOTSTRAP,
                bootstrap_reason="vx1_unavailable: VX1 series not in macro_observations",
            ),
            freshness_ts=FRESHNESS_TS,
        )
        assert block.calibration_state is CalibrationState.BOOTSTRAP
        assert block.bootstrap_reason == ("vx1_unavailable: VX1 series not in macro_observations")
