"""Shared engine/session fixtures for the distillation test subtree (ALP-821).

Hoisted from the ~19 sibling test files that each carried a byte-identical copy
of this in-memory-SQLite ``engine``/``session`` pair. ALP-783 wave-2 deduplicated
fixtures *within* each directory but left this copy duplicated *across* the
``q1/``, ``q7/``, ``baselines/``, ``threshold_calibration/`` and root dirs; any
test under ``tests/distillation/`` now inherits these fixtures from here.

``tests/distillation/external/conftest.py`` deliberately *overrides* both
fixtures with a ``StaticPool`` + ``check_same_thread=False`` variant: its
orchestrator/q3 tests drive synchronous DB calls through ``asyncio.to_thread``
and must share one underlying connection across worker threads. That variant is
not interchangeable with this plain one, so it stays local to the external
subtree (and shadows these for tests collected under it).

The FK target tables (``invocations``/``process_lifetimes``) are registered on
``Base.metadata`` globally by ``tests/conftest.py``, so no local
``import alphamind.state.tables`` is needed here.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind.persistence.models import Base
from alphamind.persistence.session import make_engine, make_session_factory


@pytest.fixture()
def engine() -> Iterator[Engine]:
    eng = make_engine(":memory:")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    factory = make_session_factory(engine)
    with factory() as sess:
        yield sess
