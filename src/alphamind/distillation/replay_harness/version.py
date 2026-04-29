"""Replay harness version constant.

Lives in its own module so the report header (story 08) can read the version
without importing the rest of the package.

Advances when harness algorithm changes — a new aggregation, a new
fixture-loading rule, or a Class B baseline computation switching to a new
code path. See `docs/design/02-distillation-layer/replay-harness.md`
§ Versioning.
"""

from __future__ import annotations

HARNESS_VERSION: str = "0.1.0"
