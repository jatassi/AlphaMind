"""Tests for ``scripts/backfill_ctra_merger.py`` (ALP-584).

The script records the CTRA→DVN merger that Polygon's corporate-actions feed
never carried, then runs the reconciler so ``asset_universe.CTRA`` flips to
``is_active=0``.
"""

from __future__ import annotations

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from alphamind.persistence.models import AssetUniverse, CorporateActions
from alphamind.persistence.session import make_session_factory
from alphamind.scripts.backfill_ctra_merger import seed_ctra_merger


def _seed_universe(engine: Engine) -> sessionmaker[Session]:
    """Seed CTRA and its acquirer DVN as active universe tickers."""
    factory = make_session_factory(engine)
    with factory() as sess:
        for ticker in ("CTRA", "DVN"):
            sess.merge(
                AssetUniverse(
                    asset_id=ticker,
                    ticker=ticker,
                    full_name=ticker,
                    asset_class="equity",
                    asset_role="universe",
                    exchange="NYSE",
                    is_active=1,
                    added_date="2020-01-01",
                    last_updated="2020-01-01T00:00:00Z",
                )
            )
        sess.commit()
    return factory


def test_seed_ctra_merger_deactivates_ctra(engine: Engine) -> None:
    factory = _seed_universe(engine)

    seed_ctra_merger(factory)

    with factory() as sess:
        action = sess.query(CorporateActions).filter_by(ticker="CTRA").one()
        ctra = sess.query(AssetUniverse).filter_by(ticker="CTRA").one()
        dvn = sess.query(AssetUniverse).filter_by(ticker="DVN").one()

    assert action.action_type == "merger"
    assert action.acquirer_ticker == "DVN"
    assert action.ex_date == "2026-05-07"
    assert ctra.is_active == 0
    assert ctra.removed_date == "2026-05-07"
    assert "DVN" in (ctra.removal_reason or "")
    # The acquirer itself is untouched.
    assert dvn.is_active == 1


def test_seed_ctra_merger_idempotent(engine: Engine) -> None:
    factory = _seed_universe(engine)

    seed_ctra_merger(factory)
    seed_ctra_merger(factory)

    with factory() as sess:
        assert sess.query(CorporateActions).filter_by(ticker="CTRA").count() == 1
        ctra = sess.query(AssetUniverse).filter_by(ticker="CTRA").one()
    assert ctra.is_active == 0
