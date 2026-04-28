"""No-magic-numbers audit for the distillation layer (story 14b).

Enforces the discipline stated in
``docs/design/02-distillation-layer/threshold-calibration.md``
section "Where each threshold lives": Class A thresholds reach the code only
through the loaded :class:`DistillationConfig`. Numeric literals in distillation
source files that match a value in ``config/distillation.yaml`` are treated as
bypass attempts and reported as violations.

The audit is a static-analysis pytest test. It loads the YAML, builds a value
set excluding a small allowlist of values pervasive in Python code (``0``,
``1``, ``0.0``, ``1.0``, ``100``, ``100.0``, plus booleans), AST-walks the
distillation source tree plus the Pydantic model file, and fails if any
literal in code matches a threshold value.

The audit is deliberately specific to Class A bypass — it does not duplicate
ruff's general magic-number lint.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]

DISTILLATION_YAML = REPO_ROOT / "config" / "distillation.yaml"

# Source paths the audit scans. Both the distillation runtime tree and the
# Pydantic model file are in scope: per the story, ``models/distillation.py``
# is NOT exempted because every field is required (no literal defaults).
DISTILLATION_SRC_DIR = REPO_ROOT / "src" / "alphamind" / "distillation"
DISTILLATION_MODEL_FILE = REPO_ROOT / "src" / "alphamind" / "config" / "models" / "distillation.py"

ALLOWLIST_FILE = Path(__file__).parent / "no_magic_numbers_allowlist.txt"

# Values pervasive in Python code that almost never represent a Class A
# threshold even when their numeric value happens to overlap with a YAML
# threshold value. Stored as ints; ``0 == 0.0`` and ``1 == 1.0`` in Python so
# the float forms documented in the story spec compare equal here.
PERVASIVE_VALUES: frozenset[int] = frozenset({0, 1, 100})


# ---------------------------------------------------------------------------
# Data carriers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CodeLiteral:
    """A numeric literal captured from a source file via :mod:`ast`."""

    file: Path
    line: int
    value: int | float


@dataclass(frozen=True, slots=True)
class Violation:
    """A code literal whose value matches a Class A threshold."""

    file: Path
    line: int
    value: int | float
    matched_yaml_key: str

    def remediation(self) -> str:
        """Suggested fix routing the value through the loader path."""
        return f"remediation: read the value via config.distillation.{self.matched_yaml_key}"


# ---------------------------------------------------------------------------
# YAML flattening
# ---------------------------------------------------------------------------


def _flatten_yaml(prefix: str, obj: object, out: list[tuple[str, object]]) -> None:
    """Walk a YAML-decoded structure into ``(dotted_key, value)`` tuples."""
    if isinstance(obj, dict):
        for raw_key, raw_value in obj.items():
            key = str(raw_key)
            new_prefix = f"{prefix}.{key}" if prefix else key
            _flatten_yaml(new_prefix, raw_value, out)
    elif isinstance(obj, list):
        for index, item in enumerate(obj):
            _flatten_yaml(f"{prefix}[{index}]", item, out)
    else:
        out.append((prefix, obj))


def flatten_distillation_yaml(yaml_path: Path) -> list[tuple[str, object]]:
    """Load ``yaml_path`` and return all leaf ``(key_path, value)`` tuples."""
    raw = yaml.safe_load(yaml_path.read_text())
    pairs: list[tuple[str, object]] = []
    _flatten_yaml("", raw, pairs)
    return pairs


def build_threshold_value_index(
    pairs: Iterable[tuple[str, object]],
) -> dict[int | float, str]:
    """Build a value→YAML-key map.

    Booleans are excluded — they are not numeric magic numbers and would
    produce too many false positives. Pervasive values are excluded so the
    audit does not flag every ``0``, ``1``, or ``100`` in code. Strings and
    other non-numeric leaves are excluded since the audit only inspects
    integer/float literals.

    When two YAML keys carry the same value, the first key wins for
    reporting; the violation set is unaffected.
    """
    index: dict[int | float, str] = {}
    for key, value in pairs:
        if isinstance(value, bool):
            continue
        if not isinstance(value, (int, float)):
            continue
        if value in PERVASIVE_VALUES:
            continue
        if value not in index:
            index[value] = key
    return index


# ---------------------------------------------------------------------------
# Source scanning
# ---------------------------------------------------------------------------


def iter_source_files(src_dir: Path, model_file: Path) -> list[Path]:
    """Enumerate every ``.py`` file the audit scans."""
    files: list[Path] = []
    if src_dir.exists():
        files.extend(sorted(p for p in src_dir.rglob("*.py") if p.is_file()))
    if model_file.exists():
        files.append(model_file)
    return files


def collect_literals(file_path: Path) -> list[CodeLiteral]:
    """Parse ``file_path`` and return every int/float ``ast.Constant``.

    Booleans are filtered (``isinstance(True, int)`` is ``True`` in Python so
    we must check for ``bool`` before checking for ``int``). Complex numbers
    are skipped — they cannot match a YAML scalar.
    """
    source = file_path.read_text()
    tree = ast.parse(source, filename=str(file_path))
    literals: list[CodeLiteral] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant):
            continue
        value = node.value
        if isinstance(value, bool):
            continue
        if not isinstance(value, (int, float)):
            continue
        literals.append(CodeLiteral(file=file_path, line=node.lineno, value=value))
    return literals


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AllowlistEntry:
    """One ``<file>:<line>:<value>:<reason>`` row from the allowlist."""

    file: str
    line: int
    value: int | float
    reason: str


def parse_allowlist(allowlist_path: Path) -> list[AllowlistEntry]:
    """Parse the allowlist file into structured entries.

    Lines are ``<file>:<line>:<value>:<reason>``. Blank lines and lines
    starting with ``#`` are skipped. Values are parsed as int when possible,
    else as float. A malformed row is a test-fixture bug and raises ``ValueError``.
    """
    if not allowlist_path.exists():
        return []
    entries: list[AllowlistEntry] = []
    for raw_line_number, raw_line in enumerate(allowlist_path.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":", 3)
        if len(parts) != 4:
            raise ValueError(
                f"{allowlist_path}:{raw_line_number}: malformed allowlist entry "
                f"(expected '<file>:<line>:<value>:<reason>'): {raw_line!r}"
            )
        file_part, line_part, value_part, reason_part = parts
        try:
            line_no = int(line_part)
        except ValueError as exc:
            raise ValueError(
                f"{allowlist_path}:{raw_line_number}: line number is not an int: {line_part!r}"
            ) from exc
        parsed_value: int | float
        try:
            parsed_value = int(value_part)
        except ValueError:
            try:
                parsed_value = float(value_part)
            except ValueError as exc:
                raise ValueError(
                    f"{allowlist_path}:{raw_line_number}: value is not numeric: {value_part!r}"
                ) from exc
        entries.append(
            AllowlistEntry(
                file=file_part.strip(),
                line=line_no,
                value=parsed_value,
                reason=reason_part.strip(),
            )
        )
    return entries


def is_allowlisted(
    literal: CodeLiteral,
    repo_root: Path,
    allowlist: Iterable[AllowlistEntry],
) -> bool:
    """Return ``True`` if ``literal`` matches an allowlist entry exactly."""
    try:
        rel = literal.file.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        rel = literal.file.as_posix()
    for entry in allowlist:
        if entry.file != rel:
            continue
        if entry.line != literal.line:
            continue
        if entry.value != literal.value:
            continue
        return True
    return False


# ---------------------------------------------------------------------------
# Audit pipeline
# ---------------------------------------------------------------------------


def audit_distillation_layer(
    *,
    yaml_path: Path,
    src_dir: Path,
    model_file: Path,
    allowlist_path: Path,
    repo_root: Path,
) -> list[Violation]:
    """Run the full audit and return the violation list.

    Pure: every input is a parameter, no global state, no side effects beyond
    reading files. The pytest test calls this with the production paths; the
    fixture tests call it with synthetic paths.
    """
    threshold_index = build_threshold_value_index(flatten_distillation_yaml(yaml_path))
    allowlist = parse_allowlist(allowlist_path)

    violations: list[Violation] = []
    for source_file in iter_source_files(src_dir, model_file):
        for literal in collect_literals(source_file):
            if literal.value not in threshold_index:
                continue
            if is_allowlisted(literal, repo_root, allowlist):
                continue
            violations.append(
                Violation(
                    file=source_file,
                    line=literal.line,
                    value=literal.value,
                    matched_yaml_key=threshold_index[literal.value],
                )
            )
    return violations


def format_report(violations: list[Violation], repo_root: Path) -> str:
    """Render a human-readable per-violation report."""
    lines: list[str] = [f"Magic-number violations: {len(violations)}", ""]
    for violation in violations:
        try:
            rel = violation.file.resolve().relative_to(repo_root.resolve()).as_posix()
        except ValueError:
            rel = violation.file.as_posix()
        lines.append(f"  {rel}:{violation.line}")
        lines.append(
            f"    literal value {violation.value!r} matches config key {violation.matched_yaml_key}"
        )
        lines.append(f"    {violation.remediation()}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Pytest entry points
# ---------------------------------------------------------------------------


def test_no_magic_numbers_in_distillation_layer() -> None:
    """Distillation source carries no Class A threshold literals.

    On a clean tree the violation list is empty; if it is not, the failure
    message names every offending file/line/value/key tuple plus a
    remediation hint pointing at the loader path.
    """
    violations = audit_distillation_layer(
        yaml_path=DISTILLATION_YAML,
        src_dir=DISTILLATION_SRC_DIR,
        model_file=DISTILLATION_MODEL_FILE,
        allowlist_path=ALLOWLIST_FILE,
        repo_root=REPO_ROOT,
    )
    if violations:
        pytest.fail(format_report(violations, REPO_ROOT))


# ---------------------------------------------------------------------------
# Fixture tests for the audit's matching logic
# ---------------------------------------------------------------------------


def _write_yaml_fixture(path: Path) -> None:
    """A minimal YAML carrying a couple of threshold values for fixture tests."""
    path.write_text(
        "anomaly_detection:\n  volume_anomaly_sigma: 2.5\n  earnings_revision_cluster_count: 3\n"
    )


def test_audit_flags_known_violation(tmp_path: Path) -> None:
    """A synthetic source file carrying a YAML-matching literal is flagged."""
    yaml_path = tmp_path / "distillation.yaml"
    _write_yaml_fixture(yaml_path)

    src_dir = tmp_path / "src"
    src_dir.mkdir()
    offender = src_dir / "calibration.py"
    offender.write_text(
        '"""Synthetic offender for audit fixture test."""\n'
        "\n"
        "def detect(values: list[float]) -> bool:\n"
        "    sigma_threshold = 2.5\n"
        "    return any(v > sigma_threshold for v in values)\n"
    )

    violations = audit_distillation_layer(
        yaml_path=yaml_path,
        src_dir=src_dir,
        model_file=tmp_path / "nonexistent_model.py",
        allowlist_path=tmp_path / "empty_allowlist.txt",
        repo_root=tmp_path,
    )

    assert len(violations) == 1
    only = violations[0]
    assert only.file == offender
    assert only.line == 4
    assert only.value == 2.5
    assert only.matched_yaml_key == "anomaly_detection.volume_anomaly_sigma"

    report = format_report(violations, tmp_path)
    assert "src/calibration.py:4" in report
    assert "literal value 2.5" in report
    assert "anomaly_detection.volume_anomaly_sigma" in report


def test_audit_respects_allowlist_entry(tmp_path: Path) -> None:
    """A synthetic violation paired with an allowlist entry produces no report."""
    yaml_path = tmp_path / "distillation.yaml"
    _write_yaml_fixture(yaml_path)

    src_dir = tmp_path / "src"
    src_dir.mkdir()
    offender = src_dir / "calibration.py"
    offender.write_text(
        '"""Synthetic offender for allowlist fixture test."""\n'
        "\n"
        "def detect(values: list[float]) -> bool:\n"
        "    sigma_threshold = 2.5\n"
        "    return any(v > sigma_threshold for v in values)\n"
    )

    allowlist_path = tmp_path / "allowlist.txt"
    allowlist_path.write_text(
        "# Format: <file>:<line>:<value>:<reason>\n"
        "src/calibration.py:4:2.5:fixture-only allowance for the audit's own test\n"
    )

    violations = audit_distillation_layer(
        yaml_path=yaml_path,
        src_dir=src_dir,
        model_file=tmp_path / "nonexistent_model.py",
        allowlist_path=allowlist_path,
        repo_root=tmp_path,
    )

    assert violations == []
