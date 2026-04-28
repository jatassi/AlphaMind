"""Unit tests for ``scripts/verify_regime_transition.py`` (story 13).

The regime-transition verifier reads ``distillation_regime_state`` for
the last ``--lookback-days N`` and asserts the four state-machine
invariants from story 09 hold. Coverage:

- All-pass over a clean lookback → exit 0.
- A label change with empty ``prior_label`` → invariant-1 violation.
- A ``confirmed`` row whose preceding row has a different label →
  invariant-2 violation.
- An ``early-strong`` row with ``indicator_agreement_count < 3`` →
  invariant-3 violation.
- An ``early-weak`` row with ``indicator_agreement_count >= 3`` →
  invariant-4 violation.
- Empty lookback window → reports exit 0 with an "(empty)" note (no
  invariants to violate).
- Renderer surfaces the trajectory and the transition events.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from alphamind.persistence.models import DistillationRegimeState
from alphamind.scripts.verify_regime_transition import (
    RegimeTransitionReport,
    compute_regime_transition_report,
    format_regime_transition_report,
)

_NOW = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)


def _add_regime_row(
    session: Session,
    *,
    minutes_before_now: int,
    regime_label: str,
    transition_state: str,
    prior_label: str,
    indicator_agreement_count: int = 4,
    invocations_held: int = 1,
) -> None:
    ts = _NOW - timedelta(minutes=minutes_before_now)
    iso = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    session.add(
        DistillationRegimeState(
            as_of=iso,
            regime_label=regime_label,
            vix_level=18.0,
            term_structure_basis=0.5,
            vvix_percentile=50.0,
            realized_vol=12.0,
            indicator_agreement_count=indicator_agreement_count,
            invocations_held=invocations_held,
            transition_state=transition_state,
            prior_label=prior_label,
            ingested_at=iso,
        )
    )


def test_clean_run_passes(session: Session) -> None:
    """A trajectory with valid invariants returns ``passed=True``."""
    # Three same-label rows with the second crossing the confirmed threshold.
    _add_regime_row(
        session,
        minutes_before_now=180,
        regime_label="vol_normalization",
        transition_state="stable",
        prior_label="vol_normalization",
        invocations_held=1,
    )
    _add_regime_row(
        session,
        minutes_before_now=120,
        regime_label="vol_normalization",
        transition_state="confirmed",
        prior_label="vol_normalization",
        invocations_held=2,
    )
    _add_regime_row(
        session,
        minutes_before_now=60,
        regime_label="vol_normalization",
        transition_state="stable",
        prior_label="vol_normalization",
        invocations_held=3,
    )
    session.commit()

    report = compute_regime_transition_report(session=session, now=_NOW, lookback_days=7)

    assert isinstance(report, RegimeTransitionReport)
    assert report.passed is True
    assert report.failures == ()
    assert len(report.rows) == 3


def test_label_change_without_prior_label_fails(session: Session) -> None:
    """A row whose label differs from its predecessor must record ``prior_label``."""
    _add_regime_row(
        session,
        minutes_before_now=120,
        regime_label="vol_normalization",
        transition_state="stable",
        prior_label="vol_normalization",
    )
    # Empty prior_label on a label change → invariant-1 violation.
    _add_regime_row(
        session,
        minutes_before_now=60,
        regime_label="vol_expansion",
        transition_state="early-strong",
        prior_label="",
        indicator_agreement_count=4,
    )
    session.commit()

    report = compute_regime_transition_report(session=session, now=_NOW, lookback_days=7)

    assert report.passed is False
    codes = {f.code for f in report.failures}
    assert "missing-prior-label-on-change" in codes


def test_confirmed_without_two_prior_invocations_fails(session: Session) -> None:
    """A ``confirmed`` row's predecessor must share its new label."""
    _add_regime_row(
        session,
        minutes_before_now=120,
        regime_label="vol_normalization",
        transition_state="stable",
        prior_label="vol_normalization",
    )
    # confirmed at vol_expansion immediately after vol_normalization is invalid.
    _add_regime_row(
        session,
        minutes_before_now=60,
        regime_label="vol_expansion",
        transition_state="confirmed",
        prior_label="vol_normalization",
        invocations_held=1,
    )
    session.commit()

    report = compute_regime_transition_report(session=session, now=_NOW, lookback_days=7)

    assert report.passed is False
    codes = {f.code for f in report.failures}
    assert "confirmed-without-prior-runlength" in codes


def test_early_strong_with_low_agreement_fails(session: Session) -> None:
    """An ``early-strong`` row must have ``indicator_agreement_count >= 3``."""
    _add_regime_row(
        session,
        minutes_before_now=120,
        regime_label="vol_normalization",
        transition_state="stable",
        prior_label="vol_normalization",
    )
    _add_regime_row(
        session,
        minutes_before_now=60,
        regime_label="vol_expansion",
        transition_state="early-strong",
        prior_label="vol_normalization",
        indicator_agreement_count=2,  # below threshold
    )
    session.commit()

    report = compute_regime_transition_report(session=session, now=_NOW, lookback_days=7)

    assert report.passed is False
    codes = {f.code for f in report.failures}
    assert "early-strong-low-agreement" in codes


def test_early_weak_with_high_agreement_fails(session: Session) -> None:
    """An ``early-weak`` row must have ``indicator_agreement_count < 3``."""
    _add_regime_row(
        session,
        minutes_before_now=120,
        regime_label="vol_normalization",
        transition_state="stable",
        prior_label="vol_normalization",
    )
    _add_regime_row(
        session,
        minutes_before_now=60,
        regime_label="vol_expansion",
        transition_state="early-weak",
        prior_label="vol_normalization",
        indicator_agreement_count=4,  # above threshold
    )
    session.commit()

    report = compute_regime_transition_report(session=session, now=_NOW, lookback_days=7)

    assert report.passed is False
    codes = {f.code for f in report.failures}
    assert "early-weak-high-agreement" in codes


def test_empty_lookback_window_passes(session: Session) -> None:
    """No rows in the lookback window → no invariants to violate → pass."""
    report = compute_regime_transition_report(session=session, now=_NOW, lookback_days=7)

    assert report.passed is True
    assert report.rows == ()


def test_format_renders_trajectory_and_events(session: Session) -> None:
    """Rendered output mentions the trajectory and a transition event."""
    _add_regime_row(
        session,
        minutes_before_now=120,
        regime_label="vol_normalization",
        transition_state="stable",
        prior_label="vol_normalization",
    )
    _add_regime_row(
        session,
        minutes_before_now=60,
        regime_label="vol_expansion",
        transition_state="early-strong",
        prior_label="vol_normalization",
        indicator_agreement_count=4,
    )
    session.commit()

    report = compute_regime_transition_report(session=session, now=_NOW, lookback_days=7)
    rendered = format_regime_transition_report(report)

    assert "Regime Transition Verification" in rendered
    assert "vol_normalization" in rendered
    assert "vol_expansion" in rendered
    assert "early-strong" in rendered
    assert "RESULT: PASS" in rendered
