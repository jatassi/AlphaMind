"""Cross-CHECK / enum lockstep regression guard for ``activity_log`` (ALP-933).

The ``activity_log`` table carries three closed-set CHECK constraints whose
vocabularies the model derives from live enums — ``ck_activity_log_event_type``
from ``EventType``, ``ck_activity_log_event_group`` from ``EventGroup``,
``ck_activity_log_source`` from ``EventSource`` (see
``state/tables/activity_log.py``). Each time an enum gains a member, a companion
Alembic migration must widen the matching CHECK, or an alembic-managed DB created
before the member existed silently rejects the new value at insert time. ALP-933
is exactly that failure: ALP-877 added ``EventGroup.DISTILLATION_ANOMALY`` but the
companion migration widened only the *event_type* and *source* CHECKs, leaving the
*group* CHECK narrow on prod.

This guard asserts model↔migration vocabulary lockstep by a **deterministic
persisted-DDL string check** — the assertion the FK-only autogenerate test
(``test_mapper_fk_autogenerate.py``) deliberately cannot make, because Alembic's
SQLite reflection cannot reliably diff CHECK constraints (called out at
``test_feedback_loop_migration.py``). It materialises the head schema, reads the
persisted ``activity_log`` DDL from ``sqlite_master``, and asserts every value in
the model's ``_EVENT_TYPES`` / ``_EVENT_GROUPS`` / ``_EVENT_SOURCES`` appears as a
quoted literal in that DDL.

**On the metadata-driven baseline this is a forward tripwire, not a same-run
red→green.** The genesis baseline (``a000000000aa``) is metadata-driven
(``Base.metadata.create_all`` against the live models), so a fresh ``upgrade
head`` builds all three CHECKs from the *current* enums and this guard is
tautologically green — both before and after the ALP-933 migration. The original
prod drift arose because prod materialised the baseline when the model's enum was
still narrow and then carried that table forward through migrations that forgot
the *group* widen; a fresh test DB cannot reproduce that, since it always
materialises against the current (wide) model. The guard's value is therefore a
tripwire that fires the moment the head-materialised DDL ever loses a current
enum member — e.g. if the baseline is later re-frozen as static SQL (a real
possibility when baselines are re-squashed) and a member is missed, or if a model
ever stops deriving a CHECK from its enum. Its catch logic is verified by
inspection of its red→green transition against a temporarily-narrowed CHECK
(per ALP-933 AC); the live red→green for the *group* member is exercised by the
insert-level round-trip in
``test_activity_log_event_group_distillation_anomaly_migration.py``.
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from alphamind.persistence.session import make_engine
from alphamind.state.tables.activity_log import (
    _EVENT_GROUPS,
    _EVENT_SOURCES,
    _EVENT_TYPES,
)


def _alembic_config(db_path: Path) -> Config:
    repo_root = Path(__file__).parents[2]
    return Config(
        repo_root / "alembic.ini",
        cmd_opts=Namespace(x=[f"db={db_path}"]),
    )


def _activity_log_ddl(db_path: Path) -> str:
    """Return the persisted ``CREATE TABLE activity_log`` DDL from sqlite_master."""
    eng = make_engine(str(db_path))
    try:
        with eng.connect() as conn:
            ddl: str = conn.execute(
                text("SELECT sql FROM sqlite_master WHERE type='table' AND name='activity_log'"),
            ).scalar_one()
            return ddl
    finally:
        eng.dispose()


class TestActivityLogCheckEnumParity:
    def test_head_ddl_lists_every_model_check_vocabulary_member(self, tmp_path: Path) -> None:
        """Every model enum member appears as a quoted literal in the migrated
        head ``activity_log`` DDL — across all three CHECK constraints.

        Fails the next time an enum gains a member that a migration forgets to
        widen into ``ck_activity_log_event_type`` / ``_event_group`` / ``_source``
        on a non-metadata-built head DDL.
        """
        db_path = tmp_path / "parity.db"
        command.upgrade(_alembic_config(db_path), "head")
        ddl = _activity_log_ddl(db_path)

        for member in _EVENT_TYPES:
            assert repr(member) in ddl, f"event_type CHECK missing {member!r}"
        for member in _EVENT_GROUPS:
            assert repr(member) in ddl, f"event_group CHECK missing {member!r}"
        for member in _EVENT_SOURCES:
            assert repr(member) in ddl, f"source CHECK missing {member!r}"
