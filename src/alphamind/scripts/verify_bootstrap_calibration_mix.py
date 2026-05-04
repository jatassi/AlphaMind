"""Post-bootstrap calibration-mix verifier (story 13).

After ``python -m alphamind.collector bootstrap`` completes, run the
distillation orchestrator once and then this script. The script reads
``distillation_ticker_baseline`` and ``distillation_pair_lag``,
computes the calibration-state distribution per ``baseline_kind``, and
asserts the distribution roughly matches
``docs/design/02-distillation-layer/threshold-calibration.md``
§ Warm-up duration estimate:

- Volume / ATR / spread: ``calibrated`` after 20 trading days. After
  the standard 252-day bootstrap window most rows should be calibrated.
- Sentiment: starts mostly ``bootstrap`` (vendor sentiment backfill
  varies), trends to ``calibrated`` over the first 1-2 invocations.
- Lead-lag pairs: starts mostly ``bootstrap`` (event-driven; meaningful
  after ~10 cycles per pair, typically 1-2 months).

Bands are deliberately broad — the warm-up estimate is qualitative, not
a numeric SLA. ``≥ 80%`` calibrated for high-frequency baselines and
``≤ 70%`` calibrated for sentiment/lead-lag are the structural
"calibrated mostly works / event-driven is mostly bootstrapping"
checks the spec asks for.

Cold-start exemption: on a freshly-migrated DB the first orchestrator
invocation tags every per-ticker baseline ``bootstrap`` (the rolling
window has not filled yet), so the high-frequency lower bound trivially
fails on day one. When every row carries the cold-start signature —
all ``bootstrap``, every ``n_observations < window_days``, and the
ticker count covers the configured universe — the verifier reports the
band as ``DEFERRED`` instead of failing, mirroring the ``deferred=True``
state-table pattern in ``verify_distillation``.

Exit codes:
- 0 — every kind's distribution is within its expected band, or any
  out-of-band high-frequency band is in the cold-start state
- 1 — at least one kind is out of band, or the table is empty
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from alphamind.distillation.calibration import CalibrationState
from alphamind.persistence.models import (
    DistillationPairLag,
    DistillationTickerBaseline,
)
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.scripts._common import AssertionFailure, load_universe_scope

# ---------------------------------------------------------------------------
# Expected calibrated-share bands per kind
# ---------------------------------------------------------------------------
#
# Per story 13's note: "Don't pin tight numeric thresholds; verify
# 'qualitatively correct' with broad bands (e.g., ≥ 80% of volume
# baselines are calibrated; ≥ 50% of lead-lag pairs are bootstrap)."
# The bands below encode the qualitative bands so the assertion checks
# the documented direction without over-fitting.

CALIBRATED_DOMINANT_LOWER_BOUND = 0.80
"""High-frequency kinds (volume / ATR / spread) — at least 80% calibrated.

Per ``threshold-calibration.md`` § Warm-up duration estimate: high-
frequency baselines are universally ``calibrated`` after one month of
operation. The threshold is the qualitative "mostly calibrated" band.
"""

BOOTSTRAP_DOMINANT_UPPER_BOUND = 0.50
"""Event-driven kinds (sentiment / lead-lag) — calibrated share ≤ 50%.

Sentiment is mostly ``bootstrap`` initially (vendor backfill depth
varies); lead-lag pairs are mostly ``bootstrap`` for the first 1-2
months. The qualitative band keeps the assertion permissive while
catching a regression that flipped the dominance the wrong way.
"""


@dataclass(frozen=True)
class _KindBand:
    """Expected band for one baseline kind."""

    kind: str
    table_name: str
    description: str
    calibrated_share_lower: float | None
    calibrated_share_upper: float | None


_KIND_BANDS: tuple[_KindBand, ...] = (
    _KindBand(
        kind="volume",
        table_name="distillation_ticker_baseline",
        description="Per-ticker volume baseline (high-frequency, ≥ 80% calibrated expected).",
        calibrated_share_lower=CALIBRATED_DOMINANT_LOWER_BOUND,
        calibrated_share_upper=None,
    ),
    _KindBand(
        kind="atr",
        table_name="distillation_ticker_baseline",
        description="Per-ticker ATR baseline (high-frequency, ≥ 80% calibrated expected).",
        calibrated_share_lower=CALIBRATED_DOMINANT_LOWER_BOUND,
        calibrated_share_upper=None,
    ),
    _KindBand(
        kind="spread",
        table_name="distillation_ticker_baseline",
        description="Per-ticker spread baseline (high-frequency, ≥ 80% calibrated expected).",
        calibrated_share_lower=CALIBRATED_DOMINANT_LOWER_BOUND,
        calibrated_share_upper=None,
    ),
    _KindBand(
        kind="sentiment",
        table_name="distillation_ticker_baseline",
        description=(
            "Per-ticker sentiment baseline (vendor backfill varies, ≤ 50% "
            "calibrated expected immediately post-bootstrap)."
        ),
        calibrated_share_lower=None,
        calibrated_share_upper=BOOTSTRAP_DOMINANT_UPPER_BOUND,
    ),
    _KindBand(
        kind="lead_lag",
        table_name="distillation_pair_lag",
        description=(
            "Lead-lag pair estimates (event-driven, ≤ 50% calibrated expected "
            "immediately post-bootstrap)."
        ),
        calibrated_share_lower=None,
        calibrated_share_upper=BOOTSTRAP_DOMINANT_UPPER_BOUND,
    ),
)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KindDistribution:
    """Per-kind calibration distribution snapshot.

    ``calibrated_share`` is ``calibrated / total`` in [0, 1]. ``None``
    when ``total == 0`` (empty table). Two deferral flags exist for
    the cases where the qualitative band is structurally inapplicable:

    - ``cold_start_deferred`` — fresh-DB first invocation: all rows are
      ``bootstrap`` with ``n_observations < window_days``. Applies to
      lower-bound bands (volume / atr / spread) only — the upper-bound
      bands trivially pass on cold start.
    - ``post_bootstrap_deferred`` — system has matured past the warm-up
      window: every row is ``calibrated``. Applies to upper-bound bands
      (sentiment / lead_lag) only — once those have fully calibrated by
      design, the "≤ X% calibrated" assertion has nothing left to catch.

    Either flag turns a would-be band failure into a DEFERRED row, in the
    same spirit as ``verify_distillation``'s ``deferred=True`` state-table
    probes.
    """

    kind: str
    table_name: str
    total: int
    calibrated: int
    bootstrap: int
    unavailable: int
    expected_calibrated_share_lower: float | None
    expected_calibrated_share_upper: float | None
    in_band: bool
    cold_start_deferred: bool = False
    post_bootstrap_deferred: bool = False

    @property
    def calibrated_share(self) -> float:
        if self.total == 0:
            return 0.0
        return self.calibrated / self.total

    @property
    def deferred(self) -> bool:
        return self.cold_start_deferred or self.post_bootstrap_deferred


@dataclass(frozen=True)
class CalibrationMixReport:
    """Aggregate report — passes when every kind is in band."""

    passed: bool
    failures: tuple[AssertionFailure, ...]
    distributions: tuple[KindDistribution, ...]


# ---------------------------------------------------------------------------
# Pure assertion logic
# ---------------------------------------------------------------------------


def _count_calibration_states(session: Session, *, band: _KindBand) -> tuple[int, int, int, int]:
    """Return ``(total, calibrated, bootstrap, unavailable)`` for ``band``.

    The table-name dispatch is explicit (rather than reflective) so the
    schema is part of the verifier's contract — the assertion fails
    loudly if a model is renamed without updating the verifier.
    """
    if band.table_name == "distillation_ticker_baseline":
        rows = session.execute(
            select(
                DistillationTickerBaseline.calibration_state,
                func.count(),
            )
            .where(DistillationTickerBaseline.baseline_kind == band.kind)
            .group_by(DistillationTickerBaseline.calibration_state)
        ).all()
    elif band.table_name == "distillation_pair_lag":
        rows = session.execute(
            select(
                DistillationPairLag.calibration_state,
                func.count(),
            ).group_by(DistillationPairLag.calibration_state)
        ).all()
    else:
        raise ValueError(f"unknown table_name {band.table_name!r}")

    counts = {state: int(n) for state, n in rows}
    calibrated = counts.get("calibrated", 0)
    bootstrap = counts.get("bootstrap", 0)
    unavailable = counts.get("unavailable", 0)
    total = calibrated + bootstrap + unavailable
    return total, calibrated, bootstrap, unavailable


def _band_holds(
    *,
    calibrated_share: float,
    lower: float | None,
    upper: float | None,
) -> bool:
    """Return True when ``calibrated_share`` falls within ``[lower, upper]``.

    Either bound may be ``None`` (open-ended). Both ``None`` is unused
    because every entry in :data:`_KIND_BANDS` carries at least one
    bound — a kind without an expected direction would not warrant a
    band assertion in the first place.
    """
    below_lower = lower is not None and calibrated_share < lower
    above_upper = upper is not None and calibrated_share > upper
    return not (below_lower or above_upper)


def _cold_start_signature_holds(
    session: Session,
    *,
    band: _KindBand,
    ticker_universe_size: int | None,
) -> bool:
    """Return True when ``band``'s rows match the fresh-DB cold-start signature.

    Three conditions, all required:

    1. Every row's ``calibration_state`` is ``bootstrap``.
    2. Every row's ``n_observations`` is strictly less than ``window_days``
       (the rolling window has not yet filled).
    3. When ``ticker_universe_size`` is known, the per-ticker row count
       matches it — the orchestrator processed the full universe (otherwise
       a stale partial state could be misread as cold-start).

    Only meaningful for ticker-baseline kinds with a ``calibrated_share_lower``
    band. ``sentiment`` and ``lead_lag`` only carry an upper bound, so a
    100%-bootstrap distribution already passes their band; the deferred-on-
    cold-start path doesn't apply and the function returns False early.
    """
    if band.table_name != "distillation_ticker_baseline" or band.calibrated_share_lower is None:
        return False
    rows = session.execute(
        select(
            DistillationTickerBaseline.ticker,
            DistillationTickerBaseline.calibration_state,
            DistillationTickerBaseline.n_observations,
            DistillationTickerBaseline.window_days,
        ).where(DistillationTickerBaseline.baseline_kind == band.kind)
    ).all()
    if not rows:
        return False
    bootstrap_value = CalibrationState.BOOTSTRAP.value
    for _ticker, state, n_obs, win_days in rows:
        if state != bootstrap_value or n_obs >= win_days:
            return False
    return (
        ticker_universe_size is None
        or len({ticker for ticker, _, _, _ in rows}) >= ticker_universe_size
    )


def _post_bootstrap_signature_holds(session: Session, *, band: _KindBand) -> bool:
    """Return True when every row in ``band``'s table is ``calibrated``.

    Mirror of :func:`_cold_start_signature_holds` for the upper-bound case:
    once a kind has fully matured past the warm-up window, the "≤ X%
    calibrated" band has nothing left to catch — the signature it was
    written against (sentiment vendor backfill / lead-lag event accumulation)
    has resolved by design. The check is gated on ``calibrated_share_upper``
    so it only fires for sentiment / lead_lag; lower-bound kinds use the
    cold-start path instead.

    A row's ``calibration_state == 'calibrated'`` already implies its
    underlying threshold (``n_observations >= min_observations`` for tickers,
    ``n_pair_events >= min_events`` for pairs) has been crossed, so no
    additional row-level threshold check is needed here.
    """
    if band.calibrated_share_upper is None:
        return False
    if band.table_name == "distillation_ticker_baseline":
        rows = session.execute(
            select(DistillationTickerBaseline.calibration_state).where(
                DistillationTickerBaseline.baseline_kind == band.kind
            )
        ).all()
    elif band.table_name == "distillation_pair_lag":
        rows = session.execute(select(DistillationPairLag.calibration_state)).all()
    else:
        return False
    if not rows:
        return False
    calibrated_value = CalibrationState.CALIBRATED.value
    return all(state == calibrated_value for (state,) in rows)


def compute_calibration_mix_report(
    *,
    session: Session,
    ticker_universe_size: int | None = None,
) -> CalibrationMixReport:
    """Build the per-kind distribution and the aggregate pass/fail report."""
    failures: list[AssertionFailure] = []
    distributions: list[KindDistribution] = []
    total_rows_seen = 0
    for band in _KIND_BANDS:
        total, calibrated, bootstrap, unavailable = _count_calibration_states(session, band=band)
        total_rows_seen += total
        share = calibrated / total if total > 0 else 0.0
        in_band = total > 0 and _band_holds(
            calibrated_share=share,
            lower=band.calibrated_share_lower,
            upper=band.calibrated_share_upper,
        )
        cold_start_deferred = (
            total > 0
            and not in_band
            and _cold_start_signature_holds(
                session, band=band, ticker_universe_size=ticker_universe_size
            )
        )
        post_bootstrap_deferred = (
            total > 0
            and not in_band
            and not cold_start_deferred
            and _post_bootstrap_signature_holds(session, band=band)
        )
        distributions.append(
            KindDistribution(
                kind=band.kind,
                table_name=band.table_name,
                total=total,
                calibrated=calibrated,
                bootstrap=bootstrap,
                unavailable=unavailable,
                expected_calibrated_share_lower=band.calibrated_share_lower,
                expected_calibrated_share_upper=band.calibrated_share_upper,
                in_band=in_band,
                cold_start_deferred=cold_start_deferred,
                post_bootstrap_deferred=post_bootstrap_deferred,
            )
        )
        if total > 0 and not in_band and not cold_start_deferred and not post_bootstrap_deferred:
            failures.append(
                AssertionFailure(
                    code="calibration-distribution-out-of-band",
                    message=(
                        f"kind={band.kind!r} calibrated_share={share:.2f} is "
                        f"outside expected band "
                        f"[{band.calibrated_share_lower}, {band.calibrated_share_upper}]"
                    ),
                )
            )
    if total_rows_seen == 0:
        failures.append(
            AssertionFailure(
                code="no-baseline-rows",
                message=(
                    "distillation_ticker_baseline and distillation_pair_lag "
                    "are both empty — run the orchestrator at least once after bootstrap."
                ),
            )
        )
    return CalibrationMixReport(
        passed=not failures,
        failures=tuple(failures),
        distributions=tuple(distributions),
    )


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


def format_calibration_mix_report(report: CalibrationMixReport) -> str:
    """Human-readable summary."""
    lines: list[str] = []
    banner = "=" * 70
    lines.append(banner)
    lines.append("AlphaMind Distillation — Bootstrap Calibration Mix")
    lines.append(banner)

    lines.append("")
    lines.append("[ Calibration Mix ]")
    lines.append(
        f"  {'kind':<12} {'total':>6} {'calibrated':>11} {'bootstrap':>10} "
        f"{'unavailable':>12} {'share':>7}  band"
    )
    for dist in report.distributions:
        share_str = f"{dist.calibrated_share * 100:>6.1f}%" if dist.total > 0 else "  (n/a)"
        lower = dist.expected_calibrated_share_lower
        upper = dist.expected_calibrated_share_upper
        band_lower = "_" if lower is None else f"{lower * 100:.0f}%"
        band_upper = "_" if upper is None else f"{upper * 100:.0f}%"
        if dist.in_band:
            status = "OK"
        elif dist.total == 0:
            status = "EMPTY"
        elif dist.deferred:
            status = "DEFERRED"
        else:
            status = "OUT"
        lines.append(
            f"  {dist.kind:<12} {dist.total:>6} {dist.calibrated:>11} "
            f"{dist.bootstrap:>10} {dist.unavailable:>12} {share_str}  "
            f"[{band_lower}..{band_upper}] {status}"
        )

    if report.failures:
        lines.append("")
        lines.append("[ Failures ]")
        for failure in report.failures:
            lines.append(f"  {failure.code}: {failure.message}")

    lines.append("")
    lines.append(banner)
    if report.passed:
        lines.append("RESULT: PASS — calibration distribution within expected bands.")
    else:
        lines.append(
            f"RESULT: FAIL — {len(report.failures)} band(s) out of tolerance or no baseline rows."
        )
    lines.append(banner)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify the post-bootstrap calibration-state mix against the "
            "warm-up estimate (story 13)."
        )
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="Path to the AlphaMind SQLite database.",
    )
    args = parser.parse_args(argv)

    universe = load_universe_scope()

    engine = make_engine(args.db_path)
    factory = make_session_factory(engine)
    with factory() as session:
        report = compute_calibration_mix_report(session=session, ticker_universe_size=len(universe))
    print(format_calibration_mix_report(report))
    return 0 if report.passed else 1


__all__ = [
    "BOOTSTRAP_DOMINANT_UPPER_BOUND",
    "CALIBRATED_DOMINANT_LOWER_BOUND",
    "AssertionFailure",
    "CalibrationMixReport",
    "KindDistribution",
    "compute_calibration_mix_report",
    "format_calibration_mix_report",
    "main",
]


if __name__ == "__main__":
    sys.exit(main())
