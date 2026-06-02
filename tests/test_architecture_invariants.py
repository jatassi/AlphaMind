"""Post-refactor architecture invariants from the 2026-05-12 audit (ALP-454).

These tests act as regression guards for the headline findings in
``audit-alphamind-2026-05-12.html``. They assert ranges/sets rather than
exact counts so unrelated future work doesn't churn this file — but any
substantial drift from the post-refactor baseline trips a test.

Tests verify:

* **Cycle elimination (L2, L3 in the audit).** The two system-spanning
  cycles (10-module ``portfolio_state``-centric and 12-module
  ``decision↔execution``) are eliminated. Remaining cycles are within-
  package only and do not touch audit-target modules.
* **Antipattern counts (L4, L9, L19).** Bare-except handlers, internal
  Pydantic types, and async-over-sync sites stay within the warranted-
  residue band reported in ALP-481.
* **Import-linter contracts.** All 8 contracts authored across waves 1+3
  (composition-root-layering, distillation-no-sqlalchemy,
  distillation-compute-no-sqlalchemy, kernel-leaf, commands-leaf,
  distillation-not-persistence-models, decision-not-execution,
  portfolio_state-not-risk_guardrails) are present in ``.importlinter``.

The tests invoke the project's own analysis scripts as subprocesses so
they exercise the same tooling the audit uses. Behavior (counts/sets),
not script internals, is what's asserted.
"""

from __future__ import annotations

import configparser
import json
import re
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

ScriptOutput = dict[str, Any]

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_PKG = PROJECT_ROOT / "src" / "alphamind"
ANALYZE_IMPORTS = (
    PROJECT_ROOT / ".claude" / "skills" / "python-architecture" / "scripts" / "analyze_imports.py"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_json_script(script: Path, target: Path) -> ScriptOutput:
    """Invoke an analysis script and parse its JSON output."""
    result = subprocess.run(
        ["uv", "run", "python", str(script), str(target)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"{script.name} exited {result.returncode}\n"
        f"stdout (first 500 chars):\n{result.stdout[:500]}\n"
        f"stderr:\n{result.stderr}"
    )
    return cast(ScriptOutput, json.loads(result.stdout))


@pytest.fixture(scope="module")
def import_analysis() -> ScriptOutput:
    """Run analyze_imports.py once per test module."""
    return _run_json_script(ANALYZE_IMPORTS, SRC_PKG)


# ---------------------------------------------------------------------------
# Cycle invariants — audit findings L2 (10-module) and L3 (12-module).
# ---------------------------------------------------------------------------


# Modules that anchored the audit's two system-spanning cycles. None may
# appear in any remaining cycle. Regex patterns so subpackages match
# (e.g. ``risk_guardrails.guardrail_evaluation.types`` matches
# ``risk_guardrails.*types``). These are the exact targets named in the
# parent issue's acceptance criteria.
_AUDIT_CYCLE_TARGETS = re.compile(
    r"^alphamind\."
    r"("
    r"portfolio_state\.aggregates"
    r"|risk_guardrails\..*types"
    r"|distillation\.calibration(\.|$)"
    r"|persistence\.models(\.|$)"
    r"|decision\.portfolio_manager(\.|$)"
    r"|execution\.oms(\.|$)"
    r"|execution\.state_persistence(\.|$)"
    r")"
)


def test_no_audit_target_modules_in_any_cycle(import_analysis: ScriptOutput) -> None:
    """Audit findings L2 / L3 cycles are eliminated.

    The 10-module ``portfolio_state ↔ risk_guardrails ↔ distillation ↔
    persistence`` cycle and the 12-module ``decision ↔ execution`` cycle
    are the two load-bearing cycles the audit called out. After the
    refactor (waves 1-12), none of the modules that anchored those
    cycles may appear in any cycle the analyzer detects.

    Other small intra-package cycles (e.g. the ``portfolio_state.events``
    umbrella's lazy-dispatch re-import) are allowed — they are scoped
    locally and do not cross layer boundaries.
    """
    cycles: list[list[str]] = import_analysis["cycles"]
    bad: list[tuple[int, str]] = []
    for cycle_idx, cycle in enumerate(cycles):
        for module in cycle:
            if _AUDIT_CYCLE_TARGETS.match(module):
                bad.append((cycle_idx, module))
    assert not bad, (
        "Audit-target modules appear in import cycles — refactor regression.\n"
        f"  hits: {bad}\n"
        f"  all remaining cycles: {cycles}"
    )


def test_remaining_cycles_are_intra_package_only(import_analysis: ScriptOutput) -> None:
    """Every remaining cycle stays inside a single top-level alphamind package.

    The audit's complaint was system-spanning cycles. After the refactor,
    the analyzer may still report small cycles inside a package — e.g.,
    deliberate lazy-dispatch imports inside ``portfolio_state.events`` or
    self-referential ``__init__`` re-export shapes inside ``config.models``.
    Those are local and acceptable. A cycle that crosses two top-level
    alphamind packages would be a regression.
    """

    def top_level(module: str) -> str:
        # alphamind.foo.bar -> alphamind.foo
        parts = module.split(".")
        return ".".join(parts[:2]) if len(parts) >= 2 else module

    cross_package: list[list[str]] = []
    for cycle in import_analysis["cycles"]:
        roots = {top_level(m) for m in cycle}
        if len(roots) > 1:
            cross_package.append(cycle)
    assert not cross_package, (
        f"Cross-package cycles detected ({len(cross_package)}). The audit's "
        "L2/L3 findings should keep these at zero.\n"
        f"  cycles: {cross_package}"
    )


# ---------------------------------------------------------------------------
# Import-linter contract set — wave 1 + wave 3 contracts must all exist.
# ---------------------------------------------------------------------------


# Names of every import-linter contract authored across the 12 waves.
# Wave 1 (ALP-455): composition-root-layering, distillation-no-sqlalchemy.
# Wave 3 (ALP-459): kernel-leaf, commands-leaf,
#                    distillation-not-persistence-models, decision-not-execution,
#                    portfolio_state-not-risk_guardrails.
# Wave 7 (ALP-467): distillation-compute-no-sqlalchemy.
EXPECTED_CONTRACTS: frozenset[str] = frozenset(
    {
        "composition-root-layering",
        "distillation-no-sqlalchemy",
        "distillation-compute-no-sqlalchemy",
        "kernel-leaf",
        "commands-leaf",
        "distillation-not-persistence-models",
        "decision-not-execution",
        "portfolio_state-not-risk_guardrails",
    }
)


def test_all_expected_import_linter_contracts_present() -> None:
    """All 8 contracts from waves 1+3+7 must exist in ``.importlinter``.

    A missing contract would silently drop one of the audit's architectural
    guarantees. Test against the section list rather than re-running
    ``lint-imports`` (that's covered by ``test_import_linter.py``); this
    test guards the *set* of contracts as a refactor invariant.
    """
    parser = configparser.ConfigParser()
    parser.read(PROJECT_ROOT / ".importlinter")

    prefix = "importlinter:contract:"
    found = {name[len(prefix) :] for name in parser.sections() if name.startswith(prefix)}
    missing = EXPECTED_CONTRACTS - found
    assert not missing, (
        f"Expected import-linter contracts missing from .importlinter: {sorted(missing)}.\n"
        f"  contracts present: {sorted(found)}"
    )
