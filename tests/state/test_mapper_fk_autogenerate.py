"""Tests that the SQLAlchemy mapper FK declarations match the migrated schema (ALP-369).

The 12 deferred FKs declared on the mappers in
``src/alphamind/execution/state_persistence/tables/`` must round-trip cleanly
through ``alembic revision --autogenerate``: a fresh DB at ``head`` compared
against ``Base.metadata`` must produce no FK-shaped diff. Otherwise a future
autogenerate run would emit a destructive migration that drops the FKs the
``e9d2c4f7b3a1_tighten_state_persistence_fks`` migration installed.

Also asserts that ``Base.metadata.create_all`` produces a schema where every
expected FK is enforced — the unit-test-time path now matches the production
migrated path.
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy.engine.interfaces import ReflectedForeignKeyConstraint

from alphamind.persistence.models import Base
from alphamind.persistence.session import make_engine

# Expected FK shape per table: {column: target_table.target_column}.
_EXPECTED_FKS: dict[str, dict[str, str]] = {
    "positions": {
        "thesis_id": "theses.thesis_id",
        "bracket_id": "brackets.bracket_id",
        "parent_position_id": "positions.position_id",
    },
    "theses": {"position_id": "positions.position_id"},
    "orders": {
        "position_id": "positions.position_id",
        "bracket_id": "brackets.bracket_id",
    },
    "brackets": {
        "position_id": "positions.position_id",
        "entry_order_id": "orders.order_id",
    },
    "bracket_legs": {
        "order_id": "orders.order_id",
        # Pre-existing non-deferrable FK from the initial schema.
        "bracket_id": "brackets.bracket_id",
    },
    "activity_log": {
        "position_id": "positions.position_id",
        "order_id": "orders.order_id",
        "thesis_id": "theses.thesis_id",
        # Pre-existing non-deferrable FK from the initial schema.
        "invocation_id": "invocations.invocation_id",
    },
}


def _alembic_config(db_path: Path) -> Config:
    repo_root = Path(__file__).parents[2]
    return Config(
        repo_root / "alembic.ini",
        cmd_opts=Namespace(x=[f"db={db_path}"]),
    )


def _column_to_target(fk: ReflectedForeignKeyConstraint) -> tuple[str, str]:
    """Render a SQLAlchemy inspector FK record as ``(column, "table.column")``."""
    column = fk["constrained_columns"][0]
    target = f"{fk['referred_table']}.{fk['referred_columns'][0]}"
    return column, target


class TestMapperFkShape:
    """Base.metadata.create_all produces the FK-enforced schema."""

    def test_create_all_produces_expected_fks(self, tmp_path: Path) -> None:
        # Reach in to import the tables module so Base.metadata is populated.
        import alphamind.state.tables  # noqa: F401

        db_path = tmp_path / "create_all.db"
        eng = make_engine(str(db_path))
        try:
            Base.metadata.create_all(eng)
            insp = inspect(eng)
            for table, expected in _EXPECTED_FKS.items():
                actual = dict(_column_to_target(fk) for fk in insp.get_foreign_keys(table))
                assert actual == expected, (
                    f"FK mismatch on {table}: expected {expected}, got {actual}"
                )
        finally:
            eng.dispose()


class TestAutogenerateNoDrift:
    """alembic autogenerate against a fresh head DB produces no FK diff.

    Alembic emits a SAWarning ("Cannot correctly sort tables; there are
    unresolvable cycles between tables ...") on the cyclic positions ↔ theses
    ↔ brackets ↔ orders cluster and skips FK comparison for those four tables.
    Coverage of the 8 cyclic FKs lives on ``TestMapperFkShape`` (which
    inspects FK shape directly via ``Inspector.get_foreign_keys``); this test
    is the autogenerate-shaped guard for the remaining 4 FKs on
    ``bracket_legs`` and ``activity_log``.
    """

    def test_no_diff_between_metadata_and_head_schema(self, tmp_path: Path) -> None:
        # Reach in to import the tables module so Base.metadata is populated.
        import alphamind.state.tables  # noqa: F401

        db_path = tmp_path / "autogen.db"
        command.upgrade(_alembic_config(db_path), "head")

        eng = make_engine(str(db_path))
        try:
            with eng.connect() as conn:
                ctx = MigrationContext.configure(
                    connection=conn,
                    opts={"compare_type": True, "target_metadata": Base.metadata},
                )
                diffs = compare_metadata(ctx, Base.metadata)

            fk_diffs = [d for d in diffs if _is_fk_op(d)]
            assert fk_diffs == [], (
                "autogenerate would emit FK operations against a fresh head DB; "
                f"the mappers and the migrated schema have drifted: {fk_diffs}"
            )
        finally:
            eng.dispose()


def _is_fk_op(diff: object) -> bool:
    """Return True iff *diff* is an autogenerate op that touches a FK constraint.

    Alembic emits ``add_foreign_key`` / ``remove_foreign_key`` for FK ops; the
    substring match catches both directly. Table-scoped diffs nest as lists.
    """
    if isinstance(diff, list):
        return any(_is_fk_op(d) for d in diff)
    if not isinstance(diff, tuple) or not diff:
        return False
    op_name = diff[0]
    if not isinstance(op_name, str):
        return False
    return "foreign_key" in op_name
