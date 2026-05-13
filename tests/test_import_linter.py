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

ALP-459 (this story) tightens the scaffolding with five additional contracts:

- ``kernel-leaf`` (forbidden): ``alphamind._kernel`` may not import any
  sibling top-level alphamind package.
- ``commands-leaf`` (forbidden): ``alphamind.commands`` may not import any
  sibling except ``alphamind._kernel``.
- ``distillation-not-persistence-models`` (layered): ``alphamind.distillation``
  sits above ``alphamind.persistence.models``; reverse direction is banned.
- ``decision-not-execution`` (forbidden, with architectural-exception
  ``ignore_imports`` for the PM MCP composition root ``submit_envelope.py``).
- ``portfolio_state-not-risk_guardrails`` (forbidden, with architectural-
  exception ``ignore_imports`` for the boundary translator ``library_snapshot.py``).
"""

from __future__ import annotations

import configparser
import fcntl
import re
import subprocess
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Cross-process advisory lock file used to serialise the two tests that
# call ``uv run lint-imports`` against a possibly-mutated source tree.
# Under pytest-xdist, each worker is its own process — without the lock,
# the synthetic-violation test can mutate ``alphamind.decision`` while a
# different worker is running ``lint-imports`` to assert the codebase is
# clean. The lock file lives under the worktree's ``.pytest_cache``
# directory so it survives across tests but is wiped between full runs.
_LINT_IMPORTS_LOCK = PROJECT_ROOT / ".pytest_cache" / "lint-imports.lock"


@contextmanager
def _lint_imports_lock() -> Iterator[None]:
    """Acquire an exclusive cross-worker lock around a ``lint-imports`` run.

    Uses POSIX ``flock``; macOS and Linux (the supported developer/CI
    platforms) both implement it. The lock file is created lazily.
    """
    _LINT_IMPORTS_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with _LINT_IMPORTS_LOCK.open("a") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


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
    """True if ``line`` is a comment naming a punch-list item, audit finding,
    or architectural exception.

    ALP-459 introduced ``ignore_imports`` blocks whose entries are NOT
    punch-list debt but architecturally-coherent boundary patterns
    (composition roots, boundary translators — see python-architecture
    skill §C4 / §B3). Those blocks carry an explicit "architectural
    exception" header instead of a punch-list pointer; both forms are
    accepted here.
    """
    stripped = line.strip()
    if not stripped.startswith("#"):
        return False
    lowered = stripped.lower()
    if "punch-list" in lowered or "punchlist" in lowered:
        return True
    if "architectural exception" in lowered:
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

    Serialised via ``_lint_imports_lock`` so a parallel xdist worker
    running the synthetic-violation test cannot transiently break the
    codebase while this test is asserting it's clean.
    """
    with _lint_imports_lock():
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


# ---------------------------------------------------------------------------
# ALP-459: tighten import-linter contracts.
#
# The tests below verify the five new contracts introduced by this story.
# They parse the ``.importlinter`` INI file directly (configparser) so the
# test asserts the file's structure rather than its rendered behavior — the
# behavior is covered by the end-to-end ``lint-imports`` run plus a
# synthetic-violation test that proves the contract framework actually
# catches an injected upward import.
# ---------------------------------------------------------------------------


def _parse_importlinter_config() -> configparser.ConfigParser:
    """Return the parsed ``.importlinter`` config."""
    parser = configparser.ConfigParser()
    parser.read(PROJECT_ROOT / ".importlinter")
    return parser


def _contract_section(parser: configparser.ConfigParser, name: str) -> configparser.SectionProxy:
    """Return the ``[importlinter:contract:<name>]`` section."""
    key = f"importlinter:contract:{name}"
    assert parser.has_section(key), (
        f"Contract section {key!r} missing from .importlinter. "
        f"Sections present: {parser.sections()}"
    )
    return parser[key]


def _split_module_list(raw: str) -> list[str]:
    """Split an import-linter multi-line module list (one per line) into entries."""
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _enumerate_alphamind_top_level_packages() -> list[str]:
    """List every top-level ``alphamind.<pkg>`` package on disk.

    Enumerating dynamically guards against the leaf-contract forbidden list
    drifting out of sync when a new package is added — the contract must
    cover every sibling that exists today.
    """
    src = PROJECT_ROOT / "src" / "alphamind"
    return sorted(
        f"alphamind.{entry.name}"
        for entry in src.iterdir()
        if entry.is_dir() and (entry / "__init__.py").exists()
    )


def test_kernel_leaf_contract_forbids_every_sibling() -> None:
    """``alphamind._kernel`` is a true leaf: no upward dependencies allowed.

    Every sibling top-level alphamind package must appear in the forbidden
    list. Sourcing ``alphamind`` directly would collide with the source-
    module ancestor (grimp raises ``ValueError: Modules have shared
    descendants``), so the contract enumerates siblings explicitly.
    """
    parser = _parse_importlinter_config()
    section = _contract_section(parser, "kernel-leaf")

    assert section["type"] == "forbidden"
    sources = _split_module_list(section["source_modules"])
    assert sources == ["alphamind._kernel"], sources

    forbidden = set(_split_module_list(section["forbidden_modules"]))
    expected_siblings = {
        pkg for pkg in _enumerate_alphamind_top_level_packages() if pkg != "alphamind._kernel"
    }
    assert forbidden == expected_siblings, (
        f"kernel-leaf forbidden_modules out of sync with src/alphamind/ on disk.\n"
        f"  missing from forbidden: {expected_siblings - forbidden}\n"
        f"  extra in forbidden:     {forbidden - expected_siblings}"
    )


def test_commands_leaf_contract_forbids_every_sibling_except_kernel() -> None:
    """``alphamind.commands`` is a leaf that may import ``alphamind._kernel``.

    Every other top-level alphamind sibling must appear in the forbidden list.
    """
    parser = _parse_importlinter_config()
    section = _contract_section(parser, "commands-leaf")

    assert section["type"] == "forbidden"
    sources = _split_module_list(section["source_modules"])
    assert sources == ["alphamind.commands"], sources

    forbidden = set(_split_module_list(section["forbidden_modules"]))
    expected_siblings = {
        pkg
        for pkg in _enumerate_alphamind_top_level_packages()
        if pkg not in {"alphamind.commands", "alphamind._kernel"}
    }
    assert forbidden == expected_siblings, (
        f"commands-leaf forbidden_modules out of sync with src/alphamind/ on disk.\n"
        f"  missing from forbidden: {expected_siblings - forbidden}\n"
        f"  extra in forbidden:     {forbidden - expected_siblings}"
    )


def test_distillation_not_persistence_models_is_layered() -> None:
    """The distillation->persistence.models contract is downgraded to Layered.

    The forbidden shape would carry 23 ``ignore_imports`` entries — that
    is an inversion of the contract's purpose. Authored as a Layered
    contract instead (story spec §C, AC5): distillation sits above
    persistence.models, so the reverse direction (which currently has 0
    chains) is the rule actually enforced.
    """
    parser = _parse_importlinter_config()
    section = _contract_section(parser, "distillation-not-persistence-models")

    assert section["type"] == "layers"
    layers = _split_module_list(section["layers"])
    # Higher (allowed-to-import) layer listed first per import-linter
    # ``layers`` semantics.
    assert layers == ["alphamind.distillation", "alphamind.persistence.models"], layers


def test_decision_not_execution_contract_with_composition_root_exception() -> None:
    """``alphamind.decision`` must not import ``alphamind.execution`` — except
    from the PM MCP composition root ``submit_envelope/`` package.

    The ignored edges are the composition root's wiring imports. They are
    NOT punch-list debt (python-architecture skill §C4: composition roots
    legitimately wire concrete implementations across layers). ALP-464 (06b)
    decomposed the single-file ``submit_envelope.py`` into a five-submodule
    package; the per-submodule edges below replace the pre-decomposition
    single ``submit_envelope -> execution.*`` edges. The block retires when
    ALP-482 hoists the package to a top-level ``composition_roots/``.
    """
    parser = _parse_importlinter_config()
    section = _contract_section(parser, "decision-not-execution")

    assert section["type"] == "forbidden"
    sources = _split_module_list(section["source_modules"])
    assert sources == ["alphamind.decision"], sources
    forbidden = _split_module_list(section["forbidden_modules"])
    assert forbidden == ["alphamind.execution"], forbidden

    ignored = _split_module_list(section["ignore_imports"])
    # Per-submodule edges after ALP-464 decomposition. Each entry pairs the
    # decomposed submodule with the concrete ``execution.*`` import it owns;
    # the union covers the same wiring surface as the pre-decomposition single
    # ``submit_envelope -> execution.*`` block.
    pkg = "alphamind.decision.portfolio_manager.submit_envelope"
    expected_edges = {
        f"{pkg}.dispatch -> alphamind.execution.broker_adapter",
        f"{pkg}.dispatch -> alphamind.execution.broker_adapter.errors",
        f"{pkg}.dispatch -> alphamind.execution.broker_adapter.order_modify",
        f"{pkg}.dispatch -> alphamind.execution.broker_adapter.order_options",
        f"{pkg}.dispatch -> alphamind.execution.oms.broker_dispatch",
        f"{pkg}.dispatch -> alphamind.execution.state_persistence.tables.orders",
        f"{pkg}.dispatch -> alphamind.execution.state_persistence.tables.positions",
        f"{pkg}.dispatch -> alphamind.execution.state_persistence.tables.positions_codec",
        f"{pkg}.persist -> alphamind.execution.oms.broker_dispatch",
        f"{pkg}.persist -> alphamind.execution.state_persistence.config",
        f"{pkg}.persist -> alphamind.execution.state_persistence.write_paths.phase2",
        f"{pkg}.process -> alphamind.execution.oms.command_ids",
        f"{pkg}.server -> alphamind.execution.broker_adapter",
        f"{pkg}.server -> alphamind.execution.oms.broker_dispatch",
    }
    assert set(ignored) == expected_edges, (
        "decision-not-execution ignore_imports does not match grimp-discovered edges.\n"
        f"  missing: {expected_edges - set(ignored)}\n"
        f"  extra:   {set(ignored) - expected_edges}"
    )


def test_portfolio_state_not_risk_guardrails_with_boundary_translator_exception() -> None:
    """``alphamind.portfolio_state`` must not import ``alphamind.risk_guardrails``
    — except from the boundary translator ``library_snapshot.py``.

    The 2 ignored edges bridge feature shapes (Pydantic <-> dataclass) at
    the portfolio_state/risk_guardrails boundary. python-architecture
    skill §B3 recognises boundary translators as a legitimate exception.
    The block retires when ALP-483 relocates the file to risk_guardrails/.
    """
    parser = _parse_importlinter_config()
    section = _contract_section(parser, "portfolio_state-not-risk_guardrails")

    assert section["type"] == "forbidden"
    sources = _split_module_list(section["source_modules"])
    assert sources == ["alphamind.portfolio_state"], sources
    forbidden = _split_module_list(section["forbidden_modules"])
    assert forbidden == ["alphamind.risk_guardrails"], forbidden

    ignored = _split_module_list(section["ignore_imports"])
    source = "alphamind.portfolio_state.library_snapshot"
    targets = [
        "alphamind.risk_guardrails.guardrail_evaluation",
        "alphamind.risk_guardrails.guardrail_evaluation.types",
    ]
    expected_edges = {f"{source} -> {target}" for target in targets}
    assert set(ignored) == expected_edges, (
        "portfolio_state-not-risk_guardrails ignore_imports does not match "
        "grimp-discovered edges.\n"
        f"  missing: {expected_edges - set(ignored)}\n"
        f"  extra:   {set(ignored) - expected_edges}"
    )


def test_distillation_no_sqlalchemy_ignore_imports_unchanged() -> None:
    """The existing ``distillation-no-sqlalchemy`` punch-list ignore_imports are
    preserved.

    Punch-list items #9 (ALP-467) and #22 (ALP-473) are still pending, so
    every entry below must remain until those stories land. Regression
    guard: a future story tightening this contract by accident would drop
    entries here and silently re-introduce all 47 violations (24 direct
    sqlalchemy imports + 22 indirect via persistence.models + 1 indirect
    via data_sources._common).
    """
    parser = _parse_importlinter_config()
    section = _contract_section(parser, "distillation-no-sqlalchemy")
    ignored = _split_module_list(section["ignore_imports"])
    direct = [e for e in ignored if e.endswith("-> sqlalchemy")]
    indirect = [e for e in ignored if not e.endswith("-> sqlalchemy")]
    assert len(direct) == 24, (
        f"direct sqlalchemy ignore_imports count drifted: expected 24, got {len(direct)}.\n"
        f"entries:\n  " + "\n  ".join(direct)
    )
    assert len(indirect) == 23, (
        f"indirect ignore_imports count drifted: expected 23 (22 via "
        f"persistence.models + 1 via data_sources._common), got {len(indirect)}.\n"
        f"entries:\n  " + "\n  ".join(indirect)
    )


@contextmanager
def _temporary_synthetic_violation(target: Path, line: str) -> Iterator[None]:
    """Inject ``line`` into ``target`` for the duration of the context.

    The original content is restored from disk via ``git checkout`` in the
    finally block so a test failure cannot leave the worktree dirty.
    """
    original = target.read_text(encoding="utf-8")
    try:
        target.write_text(original + "\n" + line + "\n", encoding="utf-8")
        yield
    finally:
        # Restore from git as the canonical fallback in case the worktree
        # is left in an unexpected state. The plain ``write_text`` rewrite
        # would also work, but the git restore guarantees idempotency.
        target.write_text(original, encoding="utf-8")
        subprocess.run(
            ["git", "checkout", "--", str(target)],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
        )


def test_synthetic_violation_is_caught_by_lint_imports() -> None:
    """Injecting an upward import into ``decision/`` makes ``lint-imports`` fail.

    This is the load-bearing behavioural test of the story: the contracts
    must actually catch new violations, not just record old ones. The
    synthetic import is an ``alphamind.execution`` import added to an
    ``alphamind.decision`` module — every other ``decision/`` module
    (i.e., anything except the ``submit_envelope.py`` composition root)
    must be banned by ``decision-not-execution``.

    The file is restored from disk regardless of test outcome (see
    ``_temporary_synthetic_violation``); this guarantees the worktree is
    clean afterwards even if ``lint-imports`` itself crashes.
    """
    target = PROJECT_ROOT / "src" / "alphamind" / "decision" / "analyst" / "__init__.py"
    assert target.exists(), f"expected synthetic-violation target {target} to exist"

    violation_line = "from alphamind.execution.oms import command_ids  # noqa"
    # The mutation+lint window is held exclusive against every other
    # worker so the parallel "codebase is clean" test cannot observe
    # the transient broken state.
    with _lint_imports_lock(), _temporary_synthetic_violation(target, violation_line):
        result = subprocess.run(
            ["uv", "run", "lint-imports"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )

    assert result.returncode != 0, (
        "lint-imports exited 0 with a synthetic upward import injected into "
        f"{target.relative_to(PROJECT_ROOT)}.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    combined = result.stdout + result.stderr
    # ``lint-imports`` prints the contract's human-readable ``name`` rather
    # than its section slug, so match on the rendered name. The slug
    # (``decision-not-execution``) is verified separately in
    # ``test_decision_not_execution_contract_with_composition_root_exception``.
    parser = _parse_importlinter_config()
    contract_name = parser["importlinter:contract:decision-not-execution"]["name"]
    assert contract_name in combined, (
        f"lint-imports failed but did not name the {contract_name!r} contract — "
        "the contract that should have caught the synthetic violation.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    # The actual offending import must also be reported. This guards against
    # the contract appearing in output for some other reason (e.g., a kept
    # contract listed in summary) — we want proof the synthetic edge is
    # what tripped the contract.
    assert "alphamind.decision.analyst" in combined, (
        "lint-imports output does not mention the injected source module.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
