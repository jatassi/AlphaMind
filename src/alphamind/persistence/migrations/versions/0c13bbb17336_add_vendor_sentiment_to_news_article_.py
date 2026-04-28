"""add_vendor_sentiment_to_news_article_tickers

Revision ID: 0c13bbb17336
Revises: a171d048d6b8
Create Date: 2026-04-27 17:52:40.556798

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0c13bbb17336"
down_revision: str | Sequence[str] | None = "a171d048d6b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add vendor_sentiment_score and vendor_sentiment_label to news_article_tickers."""
    op.add_column(
        "news_article_tickers",
        sa.Column("vendor_sentiment_score", sa.Float(), nullable=True),
    )
    op.add_column(
        "news_article_tickers",
        sa.Column("vendor_sentiment_label", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Remove vendor_sentiment_score and vendor_sentiment_label from news_article_tickers."""
    op.drop_column("news_article_tickers", "vendor_sentiment_label")
    op.drop_column("news_article_tickers", "vendor_sentiment_score")
