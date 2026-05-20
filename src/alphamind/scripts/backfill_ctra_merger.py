"""One-off backfill: record CTRA's merger into Devon Energy and deactivate it.

Coterra Energy (CTRA) was acquired by Devon Energy (DVN) and ceased trading
after the 2026-05-06 close. Polygon's corporate-actions feed never carried the
merger, so ``asset_universe.CTRA`` stayed ``is_active=1`` and the equity
collector kept polling a delisted ticker every cycle (ALP-584).

This script inserts the ``merger`` row the feed missed, then runs
:func:`reconcile_delisted_tickers` so ``asset_universe.CTRA`` flips to
``is_active=0``. It is idempotent — the ``corporate_actions`` row is keyed on a
stable ``action_id`` and the reconciler skips already-inactive tickers, so
re-runs are no-ops.

Run against a database::

    uv run python -m alphamind.scripts.backfill_ctra_merger --db-path /path/to/alphamind.db
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from typing import Any

from alphamind.data_sources.polygon.corporate_actions import reconcile_delisted_tickers
from alphamind.persistence.models import AssetUniverse, Base, CorporateActions
from alphamind.persistence.session import make_engine, make_session_factory

_CTRA = "CTRA"
_ACQUIRER = "DVN"
_EX_DATE = "2026-05-07"
_DESCRIPTION = "Devon Energy and Coterra Energy complete merger"
# Stable, readable primary key so a re-run merges rather than duplicates.
_ACTION_ID = "manual-CTRA-merger-2026-05-07"


def seed_ctra_merger(session_factory: Any) -> None:
    """Insert the CTRA→DVN merger row and reconcile ``asset_universe``."""
    action = CorporateActions(
        action_id=_ACTION_ID,
        ticker=_CTRA,
        action_type="merger",
        ex_date=_EX_DATE,
        acquirer_ticker=_ACQUIRER,
        description=_DESCRIPTION,
        source="manual",
        ingested_at=datetime.now(UTC).isoformat(),
    )
    with session_factory() as sess:
        sess.merge(action)
        sess.commit()
    reconcile_delisted_tickers(session_factory)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill the CTRA→DVN merger (ALP-584).")
    parser.add_argument(
        "--db-path",
        default=None,
        help="SQLite database path. Defaults to the configured AlphaMind database.",
    )
    args = parser.parse_args()

    engine = make_engine(args.db_path)
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)

    seed_ctra_merger(session_factory)

    with session_factory() as sess:
        row = sess.query(AssetUniverse).filter_by(ticker=_CTRA).first()
    if row is None:
        print(f"{_CTRA}: not present in asset_universe — nothing to deactivate")
    else:
        print(
            f"{_CTRA}: is_active={row.is_active} "
            f"removed_date={row.removed_date} removal_reason={row.removal_reason!r}"
        )


if __name__ == "__main__":
    main()
