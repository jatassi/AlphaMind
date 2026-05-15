"""Pure-compute test for the q3 flow_classification propagation proof (ALP-467).

The flow classification's compute core is exercised over a hand-built
``FlowClassificationInputs`` so no SQLite session is needed. This file
lives under tests/distillation/q1/ since it shares the q1 conftest and
proves the propagation of the pilot pattern to a second category.
"""

from __future__ import annotations

from alphamind._kernel.ids import Symbol
from alphamind.distillation._repository import (
    OptionsContractRow,
    OptionsContractSnapshotRow,
)
from alphamind.distillation.q3.flow_classification_compute import (
    FlowClassificationInputs,
    PerContractSnapshotPair,
    compute_options_flow,
)


def test_options_flow_classifies_bto_when_oi_rises() -> None:
    """Today's volume on a contract whose OI rose since prior snapshot = BTO."""
    contract = OptionsContractRow(
        contract_ticker="AAPL240419C150",
        underlying_ticker=Symbol("AAPL"),
        contract_type="call",
    )
    today = OptionsContractSnapshotRow(
        contract_ticker=contract.contract_ticker,
        snapshot_ts="2026-04-25T00:00:00Z",
        volume_today=100,
        open_interest=500,
    )
    prior = OptionsContractSnapshotRow(
        contract_ticker=contract.contract_ticker,
        snapshot_ts="2026-04-24T00:00:00Z",
        volume_today=50,
        open_interest=400,
    )
    inputs = FlowClassificationInputs(
        per_ticker_pairs={
            "AAPL": (
                PerContractSnapshotPair(
                    contract=contract,
                    today_snapshot=today,
                    prior_snapshot=prior,
                ),
            )
        }
    )
    flow = compute_options_flow(inputs)["AAPL"]
    assert flow.call_bto_volume == 100
    assert flow.call_sto_volume == 0


def test_options_flow_classifies_sto_when_oi_flat_or_falls() -> None:
    """Today's volume on a contract whose OI is flat-or-falling = STO."""
    contract = OptionsContractRow(
        contract_ticker="AAPL240419P150",
        underlying_ticker=Symbol("AAPL"),
        contract_type="put",
    )
    today = OptionsContractSnapshotRow(
        contract_ticker=contract.contract_ticker,
        snapshot_ts="2026-04-25T00:00:00Z",
        volume_today=75,
        open_interest=300,
    )
    prior = OptionsContractSnapshotRow(
        contract_ticker=contract.contract_ticker,
        snapshot_ts="2026-04-24T00:00:00Z",
        volume_today=20,
        open_interest=400,
    )
    inputs = FlowClassificationInputs(
        per_ticker_pairs={
            "AAPL": (
                PerContractSnapshotPair(
                    contract=contract,
                    today_snapshot=today,
                    prior_snapshot=prior,
                ),
            )
        }
    )
    flow = compute_options_flow(inputs)["AAPL"]
    assert flow.put_sto_volume == 75
    assert flow.put_bto_volume == 0


def test_options_flow_skips_zero_volume_contracts() -> None:
    """Contracts with zero today-volume contribute nothing to the aggregate."""
    contract = OptionsContractRow(
        contract_ticker="AAPL240419C150",
        underlying_ticker=Symbol("AAPL"),
        contract_type="call",
    )
    today = OptionsContractSnapshotRow(
        contract_ticker=contract.contract_ticker,
        snapshot_ts="2026-04-25T00:00:00Z",
        volume_today=0,
        open_interest=500,
    )
    prior = OptionsContractSnapshotRow(
        contract_ticker=contract.contract_ticker,
        snapshot_ts="2026-04-24T00:00:00Z",
        volume_today=0,
        open_interest=500,
    )
    inputs = FlowClassificationInputs(
        per_ticker_pairs={
            "AAPL": (
                PerContractSnapshotPair(
                    contract=contract,
                    today_snapshot=today,
                    prior_snapshot=prior,
                ),
            )
        }
    )
    flow = compute_options_flow(inputs)["AAPL"]
    assert flow.call_bto_volume == 0
    assert flow.call_sto_volume == 0
