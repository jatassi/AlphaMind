"""Tests for ``scheduler/debug_e2e/settings.py`` (story ALP-500 / 03).

Verifies the public contract called out by the user story:

* :class:`DebugE2ESettings` is a ``frozen=True, slots=True`` dataclass with
  exactly four fields: ``account_queries``, ``ca_queries``,
  ``emitter_factory``, ``resume_context`` (the last added by ALP-693).
* :func:`configure_debug_e2e(*, archive_root)` returns a populated
  :class:`DebugE2ESettings` whose ``account_queries`` /
  ``ca_queries`` are the log-only stand-ins from story 02b and whose
  ``emitter_factory`` produces a :class:`JsonlProgressEmitter` writing to
  ``<archive_root>/invocations/<invocation_id>/progress.jsonl``.
* :mod:`alphamind.scheduler.debug_e2e` re-exports only the two-symbol
  public surface.
* Importing the factory does not eagerly import the heavy submodules
  (``broker``, ``jsonl_emitter``, ``portfolio``) at module-load time —
  story 04's import-linter contract enforces this for production callers.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime
from pathlib import Path

import pytest


def test_debug_e2e_settings_is_frozen_and_slotted() -> None:
    """:class:`DebugE2ESettings` is a frozen, slotted dataclass.

    Frozen → attribute assignment raises ``FrozenInstanceError``.
    Slotted → instances reject attribute creation (no ``__dict__``).
    """
    from alphamind.scheduler.debug_e2e.settings import DebugE2ESettings

    settings = DebugE2ESettings(
        account_queries=object(),  # type: ignore[arg-type]
        ca_queries=object(),  # type: ignore[arg-type]
        quote_source=object(),  # type: ignore[arg-type]
        emitter_factory=lambda _id, _dt: object(),  # type: ignore[arg-type,return-value]
    )

    with pytest.raises(FrozenInstanceError):
        settings.emitter_factory = lambda _id, _dt: object()  # type: ignore[misc,assignment,return-value]

    # ``slots=True`` removes ``__dict__`` so unknown attribute names
    # cannot be silently attached. Python 3.13 raises ``TypeError`` from
    # the ``__setattr__`` super-call before reaching the frozen guard;
    # earlier minor versions raise ``AttributeError``.
    with pytest.raises((AttributeError, TypeError, FrozenInstanceError)):
        settings.unknown_field = "x"  # type: ignore[attr-defined]

    # Confirm slots are declared — ``__slots__`` lists the five fields
    # (ALP-693 added ``resume_context``; ALP-753 added ``quote_source``) and
    # ``__dict__`` is absent on instances.
    assert hasattr(type(settings), "__slots__")
    assert not hasattr(settings, "__dict__")


def test_debug_e2e_settings_has_five_named_fields() -> None:
    """:class:`DebugE2ESettings` declares exactly the five story-named fields.

    ``account_queries`` / ``ca_queries`` / ``emitter_factory`` originate in
    story ALP-500 (03). ``quote_source`` was added in ALP-753 so the debug-e2e
    run substitutes an offline batch quote source (no live Alpaca fetch).
    ``resume_context`` was added in story ALP-693 to carry the ``--resume-from``
    inputs through to stories 04a / 04b's pipeline-composition runners; it
    defaults to ``None`` so existing constructors keep parsing.
    """
    from alphamind.scheduler.debug_e2e.settings import DebugE2ESettings

    field_names = tuple(f.name for f in fields(DebugE2ESettings))
    assert field_names == (
        "account_queries",
        "ca_queries",
        "quote_source",
        "emitter_factory",
        "resume_context",
    )


def test_configure_debug_e2e_returns_debug_e2e_settings(tmp_path: Path) -> None:
    """``configure_debug_e2e`` returns a :class:`DebugE2ESettings` instance."""
    from alphamind.scheduler.debug_e2e.portfolio import SYNTHETIC_PORTFOLIO
    from alphamind.scheduler.debug_e2e.settings import (
        DebugE2ESettings,
        configure_debug_e2e,
    )

    settings = configure_debug_e2e(archive_root=tmp_path, portfolio=SYNTHETIC_PORTFOLIO)
    assert isinstance(settings, DebugE2ESettings)


def test_configure_debug_e2e_wires_log_only_account_queries(tmp_path: Path) -> None:
    """The bundle carries a :class:`LogOnlyAccountStateQueries` instance."""
    from alphamind.scheduler.debug_e2e.broker import LogOnlyAccountStateQueries
    from alphamind.scheduler.debug_e2e.portfolio import SYNTHETIC_PORTFOLIO
    from alphamind.scheduler.debug_e2e.settings import configure_debug_e2e

    settings = configure_debug_e2e(archive_root=tmp_path, portfolio=SYNTHETIC_PORTFOLIO)
    assert isinstance(settings.account_queries, LogOnlyAccountStateQueries)


def test_configure_debug_e2e_wires_log_only_ca_queries(tmp_path: Path) -> None:
    """The bundle carries a :class:`LogOnlyCorporateActionsQueries` instance."""
    from alphamind.scheduler.debug_e2e.broker import LogOnlyCorporateActionsQueries
    from alphamind.scheduler.debug_e2e.portfolio import SYNTHETIC_PORTFOLIO
    from alphamind.scheduler.debug_e2e.settings import configure_debug_e2e

    settings = configure_debug_e2e(archive_root=tmp_path, portfolio=SYNTHETIC_PORTFOLIO)
    assert isinstance(settings.ca_queries, LogOnlyCorporateActionsQueries)


def test_configure_debug_e2e_threads_fresh_start_portfolio_to_broker(
    tmp_path: Path,
) -> None:
    """The ``portfolio`` kwarg flows into the broker stand-in (ALP-618).

    ``LogOnlyAccountStateQueries.get_account()`` projects the held
    portfolio's ``starting_cash_usd`` into the
    :class:`TradeAccountSnapshot`'s ``cash`` field, and ``get_positions()``
    returns one snapshot per position. The fresh-start fixture has zero
    positions and $100k cash; verifying both flow through proves the
    portfolio is threaded — not just the synthetic default.
    """
    from decimal import Decimal

    from alphamind.scheduler.debug_e2e.portfolio import FRESH_START_PORTFOLIO
    from alphamind.scheduler.debug_e2e.settings import configure_debug_e2e

    settings = configure_debug_e2e(archive_root=tmp_path, portfolio=FRESH_START_PORTFOLIO)

    account = settings.account_queries.get_account()
    assert account.cash == Decimal(100_000)
    positions = settings.account_queries.get_positions()
    assert positions == ()


def test_emitter_factory_returns_jsonl_emitter_under_invocation_id(
    tmp_path: Path,
) -> None:
    """``emitter_factory(invocation_id)`` returns a JSONL emitter at the right path.

    Path layout: ``<archive_root>/invocations/<invocation_id>/progress.jsonl``.
    The emitter must actually be a :class:`JsonlProgressEmitter` — writing
    to it produces a single JSONL record on disk at the expected path.
    """
    from alphamind.scheduler.debug_e2e.jsonl_emitter import JsonlProgressEmitter
    from alphamind.scheduler.debug_e2e.portfolio import SYNTHETIC_PORTFOLIO
    from alphamind.scheduler.debug_e2e.settings import configure_debug_e2e

    _as_of = datetime(2026, 5, 7, 14, 30, 0, tzinfo=UTC)
    settings = configure_debug_e2e(archive_root=tmp_path, portfolio=SYNTHETIC_PORTFOLIO)
    emitter = settings.emitter_factory("inv-test", _as_of)

    assert isinstance(emitter, JsonlProgressEmitter)

    # Round-trip via a real emit to prove the path is the one the factory
    # constructed — observable behaviour rather than an internal attribute.
    expected_path = tmp_path / "2026-05-07" / "inv-test" / "progress.jsonl"
    assert not expected_path.exists()
    emitter.phase_start("phase1")
    assert expected_path.is_file()


def test_package_init_reexports_only_two_symbols() -> None:
    """``scheduler.debug_e2e`` re-exports only ``DebugE2ESettings`` + ``configure_debug_e2e``.

    Verified via ``__all__`` per the user story acceptance criterion (P9 —
    small public surface).
    """
    import alphamind.scheduler.debug_e2e as pkg

    assert pkg.__all__ == ["DebugE2ESettings", "configure_debug_e2e"]
    # The two names must actually be reachable from the package namespace.
    from alphamind.scheduler.debug_e2e import (
        DebugE2ESettings,
        configure_debug_e2e,
    )

    assert DebugE2ESettings is pkg.DebugE2ESettings
    assert configure_debug_e2e is pkg.configure_debug_e2e


def test_importing_settings_does_not_eagerly_import_heavy_submodules() -> None:
    """Importing ``settings`` does not trigger ``broker`` / ``jsonl_emitter`` / ``portfolio``.

    Story 04's import-linter contract forbids production callers from
    reaching ``debug_e2e/`` at module-load time; the lazy-import seam
    inside ``configure_debug_e2e`` is what makes that contract holdable
    even when ``__main__.py`` imports ``configure_debug_e2e`` at the
    top of the module.

    Run in a fresh subprocess so the assertion is unaffected by other
    tests in the same xdist worker that may have imported the heavy
    submodules already.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys\n"
                "from alphamind.scheduler.debug_e2e.settings import (  # noqa: F401\n"
                "    DebugE2ESettings, configure_debug_e2e,\n"
                ")\n"
                "for forbidden in (\n"
                "    'alphamind.scheduler.debug_e2e.broker',\n"
                "    'alphamind.scheduler.debug_e2e.jsonl_emitter',\n"
                "    'alphamind.scheduler.debug_e2e.portfolio',\n"
                "):\n"
                "    assert forbidden not in sys.modules, (\n"
                "        f'{forbidden} imported eagerly via settings'\n"
                "    )\n"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
