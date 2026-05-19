"""Path-resolution tests for ``alphamind.persistence.session``.

The dev-machine bug these tests guard against: ``config/main.yaml``
stores production paths with Windows-style ``%USERPROFILE%`` env-var
expansions, and a previous fallback returned the literal substring on
POSIX, which made SQLAlchemy create a junk file named verbatim
``%USERPROFILE%\\AlphaMind\\data\\alphamind.db`` in the test cwd.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import alphamind.persistence.session as session_module
from alphamind.persistence.session import _resolve_path


def test_explicit_path_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_PATH", raising=False)
    assert _resolve_path("/tmp/explicit.db") == "/tmp/explicit.db"


def test_database_path_env_var_used_when_no_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_PATH", "/tmp/from-env.db")
    assert _resolve_path(None) == "/tmp/from-env.db"


def test_userprofile_falls_back_to_home_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``USERPROFILE`` is unset (POSIX), the YAML default expands to
    :func:`Path.home`, matching the convention every other module uses."""
    monkeypatch.delenv("DATABASE_PATH", raising=False)
    monkeypatch.delenv("USERPROFILE", raising=False)
    resolved = _resolve_path(None)
    home = str(Path.home())
    assert resolved.startswith(home), (
        f"expected resolved path to start with {home!r}; got {resolved!r}"
    )
    assert "%USERPROFILE%" not in resolved


def test_userprofile_env_var_honoured_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_PATH", raising=False)
    monkeypatch.setenv("USERPROFILE", "/custom/profile")
    resolved = _resolve_path(None)
    assert resolved.startswith("/custom/profile"), f"got {resolved!r}"


def test_unresolved_percent_var_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any ``%VAR%`` that survives substitution raises a clear RuntimeError
    rather than letting SQLAlchemy write a literal-named junk file."""
    monkeypatch.delenv("DATABASE_PATH", raising=False)
    monkeypatch.delenv("MYSTERY_VAR", raising=False)

    def fake_safe_load(_handle: Any) -> dict[str, dict[str, str]]:
        return {"paths": {"database": "%MYSTERY_VAR%/alphamind.db"}}

    import yaml

    monkeypatch.setattr(yaml, "safe_load", fake_safe_load)
    with pytest.raises(RuntimeError, match=r"unexpanded variables.*MYSTERY_VAR"):
        _resolve_path(None)


def test_no_config_raises_clear_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When neither explicit path, env var, nor YAML resolves, raise."""
    monkeypatch.delenv("DATABASE_PATH", raising=False)
    fake_session_path = tmp_path / "fake" / "src" / "alphamind" / "persistence" / "session.py"
    fake_session_path.parent.mkdir(parents=True)
    fake_session_path.touch()
    monkeypatch.setattr(session_module, "__file__", str(fake_session_path))
    with pytest.raises(RuntimeError, match="not configured"):
        _resolve_path(None)
