"""Q3 ATM-IV trailing baseline — thin orchestration shim (story ALP-484).

The pure compute lives in :mod:`.atm_iv_baseline_compute`; the IO shell
(history reads + baseline upserts via ``Session``) lives in
:mod:`.atm_iv_baseline_loaders`. This module preserves the legacy
session-accepting public API plus exports the canonical
:data:`ATM_IV_BASELINE_KIND` constant.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.orm import Session

from alphamind.distillation._calibration_core import CalibratedValue
from alphamind.distillation.q3.atm_iv_baseline_compute import ATM_IV_BASELINE_KIND
from alphamind.distillation.q3.atm_iv_baseline_loaders import load_and_refresh_atm_iv_baselines


def refresh_atm_iv_baselines(
    session: Session,
    *,
    ticker_scope: Sequence[str],
    as_of: str,
    window_days: int,
    min_observations: int,
) -> dict[str, CalibratedValue]:
    """Session-accepting shim that delegates to the loader composition.

    Existing call sites (q3 assembly, the external test suite) keep their
    signature; the shim threads to
    :func:`alphamind.distillation.q3.atm_iv_baseline_loaders.load_and_refresh_atm_iv_baselines`.
    """
    return load_and_refresh_atm_iv_baselines(
        session,
        ticker_scope=ticker_scope,
        as_of=as_of,
        window_days=window_days,
        min_observations=min_observations,
    )


__all__ = [
    "ATM_IV_BASELINE_KIND",
    "refresh_atm_iv_baselines",
]
