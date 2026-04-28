"""Q1 price/volume indicator and anomaly computations — story 02-distillation/08a.

The package implements the deterministic price/volume computations from
``docs/design/02-distillation-layer/external.md`` § 2 From price and volume
(quant 1) plus the price/volume anomaly detections from
``docs/design/02-distillation-layer/external.md`` § 3 Anomaly detection.

Submodules group by indicator family rather than per-bar computation so the
public surface exposed by :mod:`alphamind.distillation.q1_price_volume`
stays small.
"""
