"""add_news_article_clusters

Adds the ``news_article_clusters`` table the news-digest two-pass clustering
pipeline writes — story ALP-245. Also adds the
``news_articles.cross_ticker_cluster_id`` foreign-key column the per-article
linkage uses, plus the index the renderer's join reads.

The clustering algorithm specification lives at
``docs/design/03-analysis-layer/qualitative-research.md`` § Headline
clustering. Schema-level shape (the documentation reference) is
``docs/design/01-data-layer/schema/news_sentiment.py`` § ``HeadlineCluster``.

Revision ID: c1f7d2e8a9b3
Revises: ed4b7069d668
Create Date: 2026-05-02

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1f7d2e8a9b3"
down_revision: str | Sequence[str] | None = "ed4b7069d668"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``news_article_clusters`` and the per-article linkage column."""
    op.create_table(
        "news_article_clusters",
        sa.Column("cluster_id", sa.Text(), nullable=False),
        sa.Column("primary_theme", sa.Text(), nullable=False),
        sa.Column("headline_count", sa.Integer(), nullable=False),
        sa.Column("first_seen_at", sa.Text(), nullable=False),
        sa.Column("last_seen_at", sa.Text(), nullable=False),
        sa.Column("tickers_involved", sa.Text(), nullable=False),
        sa.Column("sealed_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("cluster_id"),
    )
    op.create_index(
        "ix_news_article_clusters_first_seen_at",
        "news_article_clusters",
        ["first_seen_at"],
    )

    # ``cross_ticker_cluster_id`` carries the cluster's ``cluster_id`` for
    # every member; null for unclustered headlines. SQLite cannot ALTER
    # an existing table to add a FK constraint, so the column is added
    # plain and the ORM model declares the relationship for typing and
    # Postgres-portability. The renderer's join reads against the index
    # below regardless of FK enforcement.
    op.add_column(
        "news_articles",
        sa.Column("cross_ticker_cluster_id", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_news_articles_cross_ticker_cluster_id",
        "news_articles",
        ["cross_ticker_cluster_id"],
    )


def downgrade() -> None:
    """Drop the per-article linkage and the cluster table."""
    op.drop_index(
        "ix_news_articles_cross_ticker_cluster_id",
        table_name="news_articles",
    )
    op.drop_column("news_articles", "cross_ticker_cluster_id")
    op.drop_index(
        "ix_news_article_clusters_first_seen_at",
        table_name="news_article_clusters",
    )
    op.drop_table("news_article_clusters")
