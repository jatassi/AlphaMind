"""Tests for the import-linter scaffolding installed in ALP-455.

These tests verify the externally observable behavior of the import-linter
configuration without locking onto the specific layer names or contract
internals — story ALP-459 will tighten those, and rewriting these tests when
it does would defeat the point of testing behavior over structure.

Acceptance criteria covered:

- AC1: ``import-linter`` is installed and the ``lint-imports`` CLI works.
- AC2: an import-linter configuration with at least one ``Layered`` contract
  exists and references the alphamind domain packages.
- AC3: ``uv run lint-imports`` exits 0 against the current codebase.
- AC5: ``CLAUDE.md`` documents ``lint-imports`` as part of the lint suite.
- AC6: every ``ignore_imports`` block carries a comment naming the punch-list
  item that will retire each entry.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Audit-finding labels (L1, L6, L9, L10, ...). Comments are lowered before
# matching, so the pattern uses lowercase ``l``.
_AUDIT_FINDING_PATTERN = re.compile(r"\bl\d{1,2}\b")


def _read_importlinter_config_text() -> str:
    """Return the raw text of whichever import-linter config file is in use."""
    importlinter_file = PROJECT_ROOT / ".importlinter"
    if importlinter_file.exists():
        return importlinter_file.read_text(encoding="utf-8")
    return (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")


def _looks_like_punch_list_comment(line: str) -> bool:
    """True if ``line`` is a comment naming a punch-list item or audit finding."""
    stripped = line.strip()
    if not stripped.startswith("#"):
        return False
    lowered = stripped.lower()
    if "punch-list" in lowered or "punchlist" in lowered:
        return True
    return bool(_AUDIT_FINDING_PATTERN.search(lowered))


def _line_ends_block(raw_line: str) -> bool:
    """True if ``raw_line`` terminates an ``ignore_imports`` value block."""
    stripped = raw_line.strip()
    if stripped.startswith("["):
        return True
    # A new top-level key (`key = value` without a `->` arrow, not indented,
    # not a comment) starts a new section.
    return (
        "=" in stripped
        and "->" not in stripped
        and not stripped.startswith("#")
        and not raw_line.startswith((" ", "\t"))
    )


def _find_ignore_import_blocks(lines: list[str]) -> list[tuple[int, int]]:
    """Return the inclusive line index pairs of each ``ignore_imports`` block."""
    blocks: list[tuple[int, int]] = []
    in_block = False
    block_start = -1
    for idx, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if stripped.startswith("ignore_imports"):
            in_block = True
            block_start = idx
            continue
        if in_block and _line_ends_block(raw_line):
            blocks.append((block_start, idx - 1))
            in_block = False
    if in_block:
        blocks.append((block_start, len(lines) - 1))
    return blocks


def test_import_linter_listed_as_dev_dependency() -> None:
    """AC1: ``import-linter`` is declared in the dev dependency group."""
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev_deps = pyproject["dependency-groups"]["dev"]
    assert any(dep.startswith("import-linter") for dep in dev_deps), (
        f"import-linter missing from [dependency-groups.dev]; saw: {dev_deps}"
    )


def test_lint_imports_cli_available() -> None:
    """AC1: the ``lint-imports`` CLI is installed and runnable."""
    result = subprocess.run(
        ["uv", "run", "lint-imports", "--help"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"`uv run lint-imports --help` exited {result.returncode}.\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def test_import_linter_config_exists_with_layered_contract() -> None:
    """AC2: an import-linter configuration exists with at least one Layered contract."""
    text = _read_importlinter_config_text()
    # The config can live in ``.importlinter`` (INI/TOML-like) or under
    # ``[tool.importlinter]`` in pyproject.toml. Accept any of INI's
    # ``type = layers``, TOML's ``type = "layers"``, or YAML's
    # ``type: layers``.
    assert "tool.importlinter" in text or "importlinter" in text, (
        "No import-linter config section found in pyproject.toml or .importlinter"
    )
    has_layered = 'type = "layers"' in text or "type = layers" in text or "type: layers" in text
    assert has_layered, "No `Layered` contract (type = layers) found in import-linter config"


def test_lint_imports_passes_against_current_codebase() -> None:
    """AC3: ``uv run lint-imports`` exits 0 on the current code.

    This is the load-bearing assertion of the story: the scaffolded
    contracts must encode rules that are *already* satisfied. Tightening
    happens in ALP-459 once the cycle-fix stories land.
    """
    result = subprocess.run(
        ["uv", "run", "lint-imports"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    assert result.returncode == 0, (
        f"`uv run lint-imports` exited {result.returncode} against the current codebase.\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def test_layered_contract_names_primary_domain_packages() -> None:
    """AC2/AC3: the Layered contract references the alphamind domain packages.

    Without these named in some Layered contract, any new upward import
    between domain layers (the failure mode the contract exists to catch)
    would slip through silently. The names below are the union of the
    layers called out in the story's Scope section.
    """
    text = _read_importlinter_config_text()
    required = [
        "alphamind.config",
        "alphamind.distillation",
        "alphamind.analysis",
        "alphamind.decision",
        "alphamind.execution",
    ]
    missing = [pkg for pkg in required if pkg not in text]
    assert not missing, (
        f"Import-linter config does not reference these alphamind packages: {missing}.\n"
        "Without them the contract cannot detect upward imports between layers."
    )


def test_every_ignore_imports_block_documents_punch_list_item() -> None:
    """AC6: every ``ignore_imports`` block carries a comment naming the
    punch-list item or audit finding that will retire each entry.

    Behavior tested: each contiguous run of entries under an
    ``ignore_imports = `` heading is preceded (within 20 lines) or contains
    within itself at least one comment line mentioning either ``punch-list``
    (the audit's ordered fix list) or a finding identifier of the form
    ``L<digit>`` (e.g. L1, L6, L9). This guarantees that every recorded
    exception ties back to a planned removal — the requirement of AC6 —
    without micromanaging exactly which line the comment lives on.
    """
    text = _read_importlinter_config_text()
    lines = text.splitlines()
    blocks = _find_ignore_import_blocks(lines)

    bad_blocks: list[str] = []
    for start, end in blocks:
        scan = lines[max(0, start - 20) : end + 1]
        if not any(_looks_like_punch_list_comment(line) for line in scan):
            bad_blocks.append(
                f"ignore_imports block at lines {start + 1}-{end + 1} has no "
                "preceding/internal comment mentioning a punch-list item or an "
                "audit finding (L<digit>)."
            )

    assert not bad_blocks, "\n".join(bad_blocks)
    # Sanity check that the parser found at least one block — otherwise
    # the assertion above would vacuously pass.
    assert blocks, "test parser found no `ignore_imports` block to validate"


@pytest.mark.parametrize("doc_path", ["CLAUDE.md"])
def test_claude_md_mentions_lint_imports(doc_path: str) -> None:
    """AC5: CLAUDE.md documents ``lint-imports`` as part of the lint suite."""
    text = (PROJECT_ROOT / doc_path).read_text(encoding="utf-8")
    assert "lint-imports" in text, f"`lint-imports` not documented in {doc_path}"
