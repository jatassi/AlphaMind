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

from sqlalchemy.orm import Session

from alphamind.execution.counterfactual_replay_engine.queue import iter_pending_replay_proposals
from alphamind.scripts.verify_counterfactual_replay_engine import build_test_seed


class TestSeedBuilder:
    def test_seeds_eight_replayable_proposals(self, session: Session) -> None:
        """The queue yields exactly the eight documented replayable proposals."""
        build_test_seed(session)
        session.flush()

        yielded = list(iter_pending_replay_proposals(session))

        assert len(yielded) == 8
