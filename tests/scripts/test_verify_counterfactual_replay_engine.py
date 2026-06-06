"""Unit tests for the counterfactual-replay-engine verify script (ALP-566, story 10).

The script's *seed builder* (``build_test_seed``) and its pure *assertion
helpers* are the unit-testable surface; the full end-to-end run is exercised by
invoking ``scripts/verify_counterfactual_replay_engine.py`` manually (it is the
verify-of-the-verify, so embedding the whole standalone run in pytest would just
duplicate the script). These tests pin:

* the seed builder inserts exactly the eight documented replayable proposals the
  engine queue yields; and
* each pure ``assert_*`` helper returns ``None`` on a conforming input and a
  failure message naming the rule on a mismatch.

The database is the sanctioned mock boundary; here it is a real in-memory SQLite
created by the shared ``session`` fixture in ``tests/scripts/conftest.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from alphamind._kernel.ids import EnvelopeId
from alphamind._kernel.money import signed_money
from alphamind.execution.counterfactual_replay_engine.engine import (
    ReplayBatchResult,
    replay_pending_proposals,
)
from alphamind.execution.counterfactual_replay_engine.enums import UnevaluableReason
from alphamind.execution.counterfactual_replay_engine.queue import iter_pending_replay_proposals
from alphamind.execution.counterfactual_replay_engine.records import CounterfactualReplayRecord
from alphamind.scripts import verify_counterfactual_replay_engine as verify
from alphamind.state.repository.counterfactual_replays import (
    load_counterfactual_replays_for_envelope,
)
from alphamind.scripts.verify_counterfactual_replay_engine import (
    ReplaySeed,
    assert_batch_counts,
    assert_corporate_action_in_window,
    assert_data_missing,
    assert_idempotent_second_run,
    assert_ineligible_strategy,
    assert_modification_record_written,
    assert_option_target_hit,
    assert_per_envelope_outcomes,
    assert_strategist_close,
    assert_target_hit_pl,
    build_test_seed,
)


class TestSeedBuilder:
    def test_seeds_eight_replayable_proposals(self, session: Session) -> None:
        """The queue yields exactly the eight documented replayable proposals."""
        build_test_seed(session)
        session.flush()

        yielded = list(iter_pending_replay_proposals(session))

        assert len(yielded) == 8


@pytest.fixture()
def replayed(
    session: Session,
) -> Iterator[tuple[dict[str, CounterfactualReplayRecord], ReplaySeed, ReplayBatchResult]]:
    """Seed, run the engine once, and return ``(records_by_envelope, seed, first_result)``.

    The database is the sanctioned mock boundary; this exercises the real engine
    against the controlled seed so the ``assert_*`` helpers can be checked on a
    genuine record set rather than hand-built imitations.
    """
    seed = build_test_seed(session)
    session.commit()
    first = replay_pending_proposals(
        session,
        config=verify._ENGINE_CONFIG,
        paper_harness_config=verify._PAPER_HARNESS,
        risk_free_rate=verify._RISK_FREE_RATE,
        as_of=seed.as_of,
    )
    session.commit()
    records: dict[str, CounterfactualReplayRecord] = {}
    for proposal in seed.proposals:
        for record in load_counterfactual_replays_for_envelope(
            session, EnvelopeId(proposal.envelope_id)
        ):
            records[str(record.pm_decision_envelope_id)] = record
    yield records, seed, first


class TestAssertionHelpersOnRealRecords:
    """Every ``assert_*`` helper passes (returns ``None``) on the real replay set."""

    def test_batch_counts_pass(
        self, replayed: tuple[dict[str, CounterfactualReplayRecord], ReplaySeed, ReplayBatchResult]
    ) -> None:
        _records, seed, first = replayed
        assert assert_batch_counts(first, seed) is None

    def test_per_envelope_outcomes_pass(
        self, replayed: tuple[dict[str, CounterfactualReplayRecord], ReplaySeed, ReplayBatchResult]
    ) -> None:
        records, seed, _ = replayed
        assert assert_per_envelope_outcomes(records, seed) is None

    def test_equity_and_close_pl_pass(
        self, replayed: tuple[dict[str, CounterfactualReplayRecord], ReplaySeed, ReplayBatchResult]
    ) -> None:
        records, seed, _ = replayed
        assert assert_target_hit_pl(records, seed) is None
        assert assert_strategist_close(records, seed) is None

    def test_option_target_and_modification_pass(
        self, replayed: tuple[dict[str, CounterfactualReplayRecord], ReplaySeed, ReplayBatchResult]
    ) -> None:
        records, _, _ = replayed
        assert assert_option_target_hit(records) is None
        assert assert_modification_record_written(records) is None

    def test_unevaluable_reasons_pass(
        self, replayed: tuple[dict[str, CounterfactualReplayRecord], ReplaySeed, ReplayBatchResult]
    ) -> None:
        records, _, _ = replayed
        assert assert_ineligible_strategy(records) is None
        assert assert_data_missing(records) is None
        assert assert_corporate_action_in_window(records) is None


class TestAssertionHelpersCatchRegressions:
    """Each helper returns a failure message when the outcome diverges from the seed.

    A verify that cannot fail is worthless, so these pin the *failure-detection*
    behavior — tampering one record / count and confirming the matching helper
    reports it.
    """

    def test_batch_counts_detects_wrong_evaluated_count(
        self, replayed: tuple[dict[str, CounterfactualReplayRecord], ReplaySeed, ReplayBatchResult]
    ) -> None:
        _, seed, first = replayed
        tampered = replace(first, evaluated=first.evaluated + 1)
        failure = assert_batch_counts(tampered, seed)
        assert failure is not None
        assert "evaluated" in failure

    def test_per_envelope_detects_wrong_status(
        self, replayed: tuple[dict[str, CounterfactualReplayRecord], ReplaySeed, ReplayBatchResult]
    ) -> None:
        records, seed, _ = replayed
        # Drop a record so the count no longer matches the eight expected.
        broken = dict(records)
        broken.pop(verify._ENV_EQUITY_TARGET)
        failure = assert_per_envelope_outcomes(broken, seed)
        assert failure is not None

    def test_target_hit_pl_detects_out_of_tolerance(
        self, replayed: tuple[dict[str, CounterfactualReplayRecord], ReplaySeed, ReplayBatchResult]
    ) -> None:
        records, seed, _ = replayed
        record = records[verify._ENV_EQUITY_TARGET]
        assert record.realized_pl is not None
        tampered = dict(records)
        tampered[verify._ENV_EQUITY_TARGET] = replace(
            record, realized_pl=signed_money(Decimal(record.realized_pl) + Decimal(50))
        )
        failure = assert_target_hit_pl(tampered, seed)
        assert failure is not None
        assert "tolerance" in failure or "within" in failure

    def test_option_target_detects_non_evaluated(
        self, replayed: tuple[dict[str, CounterfactualReplayRecord], ReplaySeed, ReplayBatchResult]
    ) -> None:
        records, _, _ = replayed
        broken = dict(records)
        broken.pop(verify._ENV_OPTION_TARGET)
        failure = assert_option_target_hit(broken)
        assert failure is not None

    def test_data_missing_detects_wrong_reason(
        self, replayed: tuple[dict[str, CounterfactualReplayRecord], ReplaySeed, ReplayBatchResult]
    ) -> None:
        records, _, _ = replayed
        record = records[verify._ENV_DATA_MISSING]
        tampered = dict(records)
        tampered[verify._ENV_DATA_MISSING] = replace(
            record, unevaluable_reason=UnevaluableReason.UNSUPPORTED_INSTRUMENT
        )
        failure = assert_data_missing(tampered)
        assert failure is not None
        assert UnevaluableReason.DATA_MISSING.value in failure

    def test_idempotency_detects_new_records_on_second_run(
        self, replayed: tuple[dict[str, CounterfactualReplayRecord], ReplaySeed, ReplayBatchResult]
    ) -> None:
        # A "second run" that re-evaluated a proposal (rather than skipping it)
        # is the regression the idempotency check must catch.
        not_idempotent = ReplayBatchResult(
            evaluated=1,
            unevaluable_by_reason={},
            skipped_idempotent=7,
            skipped_not_due=0,
            error_count=0,
            first_error=None,
        )
        failure = assert_idempotent_second_run(not_idempotent, first_total=8)
        assert failure is not None

    def test_idempotency_passes_when_all_skipped(self) -> None:
        idempotent = ReplayBatchResult(
            evaluated=0,
            unevaluable_by_reason={},
            skipped_idempotent=8,
            skipped_not_due=0,
            error_count=0,
            first_error=None,
        )
        assert assert_idempotent_second_run(idempotent, first_total=8) is None
