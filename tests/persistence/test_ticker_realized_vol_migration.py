"""Tests for the ticker_realized_vol Alembic migration — story ALP-530."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from alphamind.persistence.session import make_engine


def _alembic_config(db_path: Path) -> Config:
    repo_root = Path(__file__).parents[2]
    return Config(
        repo_root / "alembic.ini",
        cmd_opts=Namespace(x=[f"db={db_path}"]),
    )


class TestTickerRealizedVolMigration:
    def test_upgrade_head_creates_table_with_columns(self, tmp_path: Path) -> None:
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")

        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            cols = {c["name"]: c for c in insp.get_columns("ticker_realized_vol")}
            for required in (
                "ticker",
                "as_of_date",
                "trailing_30d_realized_vol",
                "invocation_id",
                "computed_at",
            ):
                assert required in cols, (
                    f"ticker_realized_vol missing column {required!r}; got {sorted(cols)}"
                )
            # Composite primary key (ticker, as_of_date) per the design.
            pk_cols = insp.get_pk_constraint("ticker_realized_vol")["constrained_columns"]
            assert set(pk_cols) == {"ticker", "as_of_date"}
            # Index on as_of_date for the "latest per ticker" read path.
            index_columns = {
                tuple(idx["column_names"]) for idx in insp.get_indexes("ticker_realized_vol")
            }
            assert ("as_of_date",) in index_columns
        finally:
            eng.dispose()

    def test_downgrade_one_drops_table(self, tmp_path: Path) -> None:
        """Targets the ticker_realized_vol revision explicitly so future
        migrations on top don't shift the downgrade target."""
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, "b3e6f8a2c4d7")
        command.downgrade(cfg, "-1")

        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            assert "ticker_realized_vol" not in insp.get_table_names(), (
                "downgrade did not drop ticker_realized_vol table"
            )
        finally:
            eng.dispose()


class TestTickerRealizedVolRowImportable:
    def test_orm_class_is_importable_from_persistence_models(self) -> None:
        """``TickerRealizedVolRow`` must live alongside the other
        distillation tables in ``alphamind.persistence.models``."""
        from alphamind.persistence.models import TickerRealizedVolRow

        assert TickerRealizedVolRow.__tablename__ == "ticker_realized_vol"
