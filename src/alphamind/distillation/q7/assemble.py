"""Q7 top-level assembly entry point — refactored for the compute/load split (ALP-486).

ALP-486 split this module along the compute/load boundary:

* :func:`assemble_q7_blocks_from_inputs` — pure compute over a frozen
  :class:`alphamind.distillation.q7._loaders.Q7Inputs`. The orchestrator's
  Phase 2 calls this under ``asyncio.TaskGroup`` + ``asyncio.to_thread`` in
  parallel with q1 / q3 / q6 / qualitative.
* :func:`assemble_q7_blocks` — thin session-accepting shim that wraps
  :func:`load_q7_inputs` then delegates to the pure compute.

The orchestrator-facing :func:`compute_pair_correlations` helper lives in
:mod:`._loaders` and is re-exported at the package root.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from sqlalchemy.orm import Session

from alphamind.distillation._config_domain import DistillationDomainConfig
from alphamind.distillation.output import OutputBlock
from alphamind.distillation.q7._loaders import Q7Inputs, load_q7_inputs


def assemble_q7_blocks_from_inputs(inputs: Q7Inputs) -> list[OutputBlock]:
    """Pure-compute assembly of every Q7 :class:`OutputBlock`.

    Operates entirely on the pre-loaded :class:`Q7Inputs`; no DB access.
    This is the function the orchestrator's Phase 2 calls under
    ``asyncio.TaskGroup`` + ``asyncio.to_thread`` parallel with q1 / q3 /
    q6 / qualitative.

    The loader has already invoked the per-sub pure computes and persisted
    the intra-sector divergence events; this function concatenates the
    pre-built blocks in the deterministic order the upstream pipeline
    expects.
    """
    if not inputs.ticker_scope:
        return []
    blocks: list[OutputBlock] = []
    blocks.extend(inputs.intra_sector_blocks)
    blocks.extend(inputs.cross_sector_blocks)
    blocks.extend(inputs.breadth_blocks)
    blocks.extend(inputs.intermarket_blocks)
    blocks.extend(inputs.lead_lag_blocks)
    blocks.extend(inputs.correlation_regime_change_blocks)
    return blocks


def assemble_q7_blocks(
    session: Session,
    *,
    config: DistillationDomainConfig,
    as_of: datetime,
    ticker_scope: Sequence[str] | None = None,
    pair_correlations: Mapping[tuple[str, str], float] | None = None,
) -> list[OutputBlock]:
    """Session-accepting shim that delegates to the pure assembly path.

    Existing call sites pass a ``Session`` directly; the shim invokes
    :func:`load_q7_inputs` (which performs the intra-sector
    divergence-event writes synchronously under the shared session) and
    then runs the pure compute. The orchestrator calls the pure compute
    directly under its TaskGroup; this shim is preserved for the external
    test suite and any consumer not yet routed through the orchestrator.
    """
    inputs = load_q7_inputs(
        session,
        config=config,
        as_of=as_of,
        ticker_scope=ticker_scope,
        pair_correlations=pair_correlations,
    )
    return assemble_q7_blocks_from_inputs(inputs)


__all__ = [
    "Q7Inputs",
    "assemble_q7_blocks",
    "assemble_q7_blocks_from_inputs",
    "load_q7_inputs",
]
