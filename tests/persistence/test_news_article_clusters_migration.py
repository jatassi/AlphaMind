"""Tests for the news_article_clusters Alembic migration — story ALP-245."""

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


class TestNewsArticleClustersMigration:
    def test_upgrade_head_creates_table_with_columns(self, tmp_path: Path) -> None:
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")

        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            cols = {c["name"]: c for c in insp.get_columns("news_article_clusters")}
            for required in (
                "cluster_id",
                "primary_theme",
                "headline_count",
                "first_seen_at",
                "last_seen_at",
                "tickers_involved",
                "sealed_at",
            ):
                assert required in cols, (
                    f"news_article_clusters missing column {required!r}; got {sorted(cols)}"
                )
            # cluster_id is the primary key.
            pk_cols = insp.get_pk_constraint("news_article_clusters")["constrained_columns"]
            assert pk_cols == ["cluster_id"]
            # An index on first_seen_at exists for the renderer's
            # invocation-windowed reads.
            index_columns = {
                tuple(idx["column_names"]) for idx in insp.get_indexes("news_article_clusters")
            }
            assert ("first_seen_at",) in index_columns
        finally:
            eng.dispose()

    def test_upgrade_head_adds_cross_ticker_cluster_id_column(self, tmp_path: Path) -> None:
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")

        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            cols = {c["name"]: c for c in insp.get_columns("news_articles")}
            assert "cross_ticker_cluster_id" in cols, (
                f"news_articles missing column cross_ticker_cluster_id; got {sorted(cols)}"
            )
            # Indexed for the renderer's join.
            indexed_columns = {
                tuple(idx["column_names"]) for idx in insp.get_indexes("news_articles")
            }
            assert ("cross_ticker_cluster_id",) in indexed_columns
        finally:
            eng.dispose()

    def test_downgrade_one_drops_table_and_column(self, tmp_path: Path) -> None:
        """Targets the news-article-clusters revision explicitly so future
        migrations on top don't shift the test's downgrade target."""
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, "c1f7d2e8a9b3")
        command.downgrade(cfg, "-1")

        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            assert "news_article_clusters" not in insp.get_table_names(), (
                "downgrade did not drop news_article_clusters table"
            )
            cols = {c["name"] for c in insp.get_columns("news_articles")}
            assert "cross_ticker_cluster_id" not in cols, (
                "downgrade did not drop news_articles.cross_ticker_cluster_id column"
            )
            # The rest of the news_articles table is intact.
            assert "article_id" in cols
            assert "headline_text" in cols
        finally:
            eng.dispose()
