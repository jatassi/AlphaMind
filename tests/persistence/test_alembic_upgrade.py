"""Unit test for the shared alembic-upgrade util (ALP-930 (D)).

``persistence.alembic_upgrade.upgrade_to_head`` is the one shared body the
feedback-loop verify script and the replay-harness engine both call to land the
full schema on a fresh scratch DB. The two callers sit at different depths and
each resolves its own ``repo_root``, so the util takes it as a parameter; this
test pins that the util materialises the head schema against a throwaway DB.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect

from alphamind.persistence.alembic_upgrade import upgrade_to_head
from alphamind.persistence.session import make_engine

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_upgrade_to_head_materialises_schema(tmp_path: Path) -> None:
    """Running the util against a fresh DB lands the head schema (a head-only table
    present confirms the migrations ran end-to-end)."""
    db_path = tmp_path / "upgrade_util.db"
    upgrade_to_head(str(db_path), repo_root=_REPO_ROOT)

    engine = make_engine(str(db_path))
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert "counterfactual_replays" in tables
    assert "theses" in tables
