"""Repo-wide pytest conftest.

The only job here is registering every state-persistence table class on
``alphamind.persistence.models.Base.metadata`` before any test fixture
calls ``Base.metadata.create_all(engine)``. The persistence module
declares string-based foreign keys from ``briefs.invocation_id`` (and
several other tables) to ``invocations.invocation_id``; SQLAlchemy
resolves those strings by walking the shared metadata, so the target
table classes must already be imported. The ``alphamind.state.tables``
package's ``__init__.py`` imports every state-layer ``Row`` class, which
is enough to register them.

Without this, narrow scoped pytest runs (e.g. ``pytest tests/data_sources``
or ``pytest tests/risk_guardrails/regime_adaptation``) fail at fixture
setup with::

    NoReferencedTableError: Foreign key associated with column
    'briefs.invocation_id' could not find table 'invocations' ...

Individual test files used to work around this with module-level
``import alphamind.state.tables  # noqa: F401`` lines; this conftest
makes that workaround unnecessary for every test in the repo.
"""

from __future__ import annotations

import alphamind.state.tables  # noqa: F401  — register state tables on Base.metadata
