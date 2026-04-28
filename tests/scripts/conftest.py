"""Shared fixtures for the scripts test suite.

The verification scripts each need an in-memory SQLite engine with the
distillation tables created and pragmas applied; pulling the fixture
into a shared conftest avoids three near-identical copies of the same
boilerplate. ``StaticPool`` + ``check_same_thread=False`` mirrors the
distillation orchestrator's own test scaffolding so the fixture can be
shared with anything that runs the orchestrator under
``asyncio.to_thread``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from alphamind.persistence.models import Base


@pytest.fixture()
def engine() -> Iterator[Engine]:
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(eng, "connect")
    def _set_sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as sess:
        yield sess
