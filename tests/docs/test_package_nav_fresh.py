"""Freshness guard for the generated module-navigation tables.

The ``Modules`` table in each nested ``CLAUDE.md`` is generated from module docstrings by
``scripts/gen_package_nav.py``. This asserts the committed tables match what the generator
would produce now — so a renamed/added/removed module, or an edited docstring, that wasn't
regenerated fails CI instead of silently drifting.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_GEN = REPO_ROOT / "scripts" / "gen_package_nav.py"

_spec = importlib.util.spec_from_file_location("gen_package_nav", _GEN)
assert _spec and _spec.loader
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)


def test_module_tables_are_fresh() -> None:
    stale = [
        str(p.relative_to(REPO_ROOT))
        for p in gen.marked_files()
        if gen.apply(p, write=False)
    ]
    assert not stale, (
        "Stale module tables — run `uv run python scripts/gen_package_nav.py`:\n"
        + "\n".join(stale)
    )
