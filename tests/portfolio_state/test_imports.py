from pathlib import Path

import alphamind.portfolio_state
import alphamind.portfolio_state.computations
import alphamind.portfolio_state.consumers
import alphamind.portfolio_state.records


def test_portfolio_state_importable() -> None:
    assert alphamind.portfolio_state is not None


def test_records_subpackage_importable() -> None:
    assert alphamind.portfolio_state.records is not None


def test_computations_subpackage_importable() -> None:
    assert alphamind.portfolio_state.computations is not None


def test_consumers_subpackage_importable() -> None:
    assert alphamind.portfolio_state.consumers is not None


# ---------------------------------------------------------------------------
# ALP-468 sync-strip invariant — Protocol + Stub layers carry zero ``async def``
# ---------------------------------------------------------------------------
#
# Guards against a re-introduction of ``async def`` in the fake-async stub
# layer the audit flagged (L10 part 1). The SQL repository implementation
# (``execution.state_persistence.repository.sql_repository``) is also sync,
# but isn't in scope of this assertion because it is verified end-to-end by
# the snapshot-assembler integration tests in ``test_sql_repository.py``.


_PORTFOLIO_STATE_SYNC_SURFACES = (
    "src/alphamind/portfolio_state/repository.py",
    "src/alphamind/portfolio_state/consumers/synthesizer.py",
    "src/alphamind/portfolio_state/pricing.py",
)


def _repo_root() -> Path:
    """Return the repository root by walking up from this file's location."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    msg = "Could not locate repo root (pyproject.toml) from test file"
    raise RuntimeError(msg)


def test_portfolio_state_sync_surfaces_have_zero_async_def() -> None:
    """The three files in scope of ALP-468 contain no ``async def`` lines.

    The Stub adapters, Protocol declarations, and snapshot-assembler
    machinery in these files are synchronous per ALP-454 Pre-resolved
    decision (C). A regression that re-adds ``async def`` here would
    silently break sync callers; this grep-based check catches that.
    """
    root = _repo_root()
    for rel in _PORTFOLIO_STATE_SYNC_SURFACES:
        text = (root / rel).read_text(encoding="utf-8")
        assert "async def" not in text, f"{rel} contains an unexpected `async def`"
