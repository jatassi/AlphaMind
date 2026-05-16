"""Q3 options-flow indicators and cross-ticker signals — story 02-distillation/08b.

The implementation is split across this sub-package so each concern lives in
its own module. The top-level entry point
:mod:`alphamind.distillation.q3_options` re-exports the public API from each
sub-module so call sites do not need to know the internal layout.

Structure:

- :mod:`alphamind.distillation.q3.anomalies` — low-OI volume anomaly and
  sector-wide sweep detection.
- :mod:`alphamind.distillation.q3.flow_classification` — BTO/STO
  classification, protective vs. speculative tagging, and index-vs-sector
  flow classification.
- :mod:`alphamind.distillation.q3.pair_trade` — pair-trade signature
  detection.
- :mod:`alphamind.distillation.q3.etf_iv_divergence` — ETF IV vs.
  single-name IV divergence detection.
- :mod:`alphamind.distillation.q3.atm_iv_baseline` — ATM-IV trailing
  baseline and IV-rank computation.
- :mod:`alphamind.distillation.q3.assemble` — per-block-type assemblers and
  the top-level :func:`assemble_q3_blocks` entry point.
"""

from __future__ import annotations

from alphamind.distillation.q3._loaders import Q3Inputs, load_q3_inputs
from alphamind.distillation.q3.anomalies import (
    LowOiVolumeAnomaly,
    SectorWideSweep,
    SweepDirection,
    detect_low_oi_volume_anomalies,
    detect_sector_wide_sweeps,
)
from alphamind.distillation.q3.assemble import (
    FlowClassificationInputs,
    assemble_q3_blocks,
    assemble_q3_blocks_from_inputs,
    assemble_q3_etf_iv_divergence_blocks,
    assemble_q3_flow_classification_blocks,
    assemble_q3_index_vs_sector_block,
    assemble_q3_iv_rank_blocks,
    assemble_q3_pair_trade_blocks,
    assemble_q3_sector_wide_sweep_blocks,
)
from alphamind.distillation.q3.atm_iv_baseline import (
    ATM_IV_BASELINE_KIND,
    refresh_atm_iv_baselines,
)
from alphamind.distillation.q3.etf_iv_divergence import (
    EtfIvDivergence,
    EtfIvDivergenceDirection,
    compute_etf_iv_divergences,
)
from alphamind.distillation.q3.flow_classification import (
    SNAPSHOT_OI_DELTA_ATTRIBUTION,
    IndexVsSectorClassification,
    IndexVsSectorLabel,
    PutFlowIntent,
    TickerOptionsFlow,
    classify_index_vs_sector_flow,
    classify_options_flow,
    classify_put_flow_intent,
)
from alphamind.distillation.q3.pair_trade import (
    FlowZScore,
    PairTradeSignature,
    detect_pair_trade_signatures,
)

__all__ = [
    "ATM_IV_BASELINE_KIND",
    "SNAPSHOT_OI_DELTA_ATTRIBUTION",
    "EtfIvDivergence",
    "EtfIvDivergenceDirection",
    "FlowClassificationInputs",
    "FlowZScore",
    "IndexVsSectorClassification",
    "IndexVsSectorLabel",
    "LowOiVolumeAnomaly",
    "PairTradeSignature",
    "PutFlowIntent",
    "Q3Inputs",
    "SectorWideSweep",
    "SweepDirection",
    "TickerOptionsFlow",
    "assemble_q3_blocks",
    "assemble_q3_blocks_from_inputs",
    "assemble_q3_etf_iv_divergence_blocks",
    "assemble_q3_flow_classification_blocks",
    "assemble_q3_index_vs_sector_block",
    "assemble_q3_iv_rank_blocks",
    "assemble_q3_pair_trade_blocks",
    "assemble_q3_sector_wide_sweep_blocks",
    "classify_index_vs_sector_flow",
    "classify_options_flow",
    "classify_put_flow_intent",
    "compute_etf_iv_divergences",
    "detect_low_oi_volume_anomalies",
    "detect_pair_trade_signatures",
    "detect_sector_wide_sweeps",
    "load_q3_inputs",
    "refresh_atm_iv_baselines",
]
