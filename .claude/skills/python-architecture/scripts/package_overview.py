#!/usr/bin/env python3
"""Produce a structural overview of a Python package.

Usage: python package_overview.py <path_to_package_root>

Output: JSON to stdout. The agent reads the JSON and forms findings against
the principles in references/. This script intentionally produces *data*,
not opinions.

The output covers, per module:
  - Path (relative to the package root)
  - Line count (non-blank, non-comment) and total lines
  - Declared __all__, if any
  - Imports, categorised: stdlib / third-party / first-party-internal
  - Top-level public symbols (those not starting with _ and not in __all__-excluded)

And per-package summary:
  - Total modules
  - Modules without __all__
  - Modules over 400 lines (god-module candidates)
  - Modules importing into vs. out of each subpackage
"""

from __future__ import annotations

import ast
import json
import sys
import sysconfig
from pathlib import Path


def _stdlib_modules() -> set[str]:
    """Names of standard-library top-level modules. 3.10+ has sys.stdlib_module_names."""
    if hasattr(sys, "stdlib_module_names"):
        return set(sys.stdlib_module_names)
    # Conservative fallback for older Python; not expected in 3.13+.
    stdlib_path = Path(sysconfig.get_paths()["stdlib"])
    return {p.stem for p in stdlib_path.iterdir() if not p.name.startswith("_")}


STDLIB = _stdlib_modules()


def categorise_import(name: str, first_party: str) -> str:
    """Return 'stdlib' | 'third_party' | 'first_party'."""
    top = name.split(".")[0]
    if top == first_party or name.startswith(first_party + "."):
        return "first_party"
    if top in STDLIB or top in {"__future__"}:
        return "stdlib"
    return "third_party"


def extract_all(tree: ast.AST) -> list[str] | None:
    """Return the list literal assigned to module-level __all__, or None."""
    for node in ast.iter_child_nodes(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "__all__"
            and isinstance(node.value, (ast.List, ast.Tuple))
        ):
            return [
                e.value
                for e in node.value.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            ]
    return None


def extract_imports(tree: ast.AST) -> list[str]:
    """Return all imported module names (from `import x` and `from x import ...`)."""
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None:
                # Treat relative imports as first_party by resolving relative to the package
                if node.level > 0:
                    names.append("." * node.level + node.module)
                else:
                    names.append(node.module)
            elif node.level > 0:
                names.append("." * node.level)
    return names


def extract_public_symbols(tree: ast.AST) -> list[str]:
    """Return module-level def/class names that don't start with _."""
    names: list[str] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not node.name.startswith("_"):
                names.append(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and not target.id.startswith("_") and target.id.isupper():
                    names.append(target.id)
    return names


def count_code_lines(source: str) -> tuple[int, int]:
    """Return (total_lines, code_lines). Code lines exclude blank and comment-only."""
    lines = source.splitlines()
    total = len(lines)
    code = 0
    in_docstring = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        # Crude docstring detection (won't catch every case but good enough for a count).
        if stripped.startswith(('"""', "'''")):
            quote = stripped[:3]
            if stripped.count(quote) >= 2 and len(stripped) > 3:
                continue  # single-line docstring
            in_docstring = not in_docstring
            continue
        if in_docstring:
            continue
        code += 1
    return total, code


def analyse_module(path: Path, first_party: str) -> dict:
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as e:
        return {
            "path": str(path),
            "error": f"SyntaxError: {e}",
        }

    imports = extract_imports(tree)
    categorised = {"stdlib": [], "third_party": [], "first_party": []}
    for name in imports:
        if name.startswith("."):
            categorised["first_party"].append(name)
        else:
            categorised[categorise_import(name, first_party)].append(name)

    total_lines, code_lines = count_code_lines(source)

    return {
        "path": str(path),
        "total_lines": total_lines,
        "code_lines": code_lines,
        "all": extract_all(tree),
        "imports": categorised,
        "public_symbols": extract_public_symbols(tree),
    }


def walk_package(root: Path) -> tuple[str, list[Path]]:
    """Return (first-party top-level package name, list of .py files)."""
    if root.is_file() and root.suffix == ".py":
        return root.stem, [root]
    if not root.is_dir():
        raise SystemExit(f"Not a directory or .py file: {root}")
    py_files = sorted(root.rglob("*.py"))
    py_files = [p for p in py_files if "__pycache__" not in p.parts and ".venv" not in p.parts]
    first_party = root.name
    return first_party, py_files


def summarise(modules: list[dict]) -> dict:
    """Produce package-level rollups from per-module analysis."""
    valid = [m for m in modules if "error" not in m]
    god_threshold = 400
    return {
        "total_modules": len(modules),
        "errored_modules": [m["path"] for m in modules if "error" in m],
        "modules_without_all": [m["path"] for m in valid if m["all"] is None],
        "god_module_candidates": [
            {"path": m["path"], "code_lines": m["code_lines"]}
            for m in valid
            if m["code_lines"] > god_threshold
        ],
        "biggest_modules": sorted(
            [{"path": m["path"], "code_lines": m["code_lines"]} for m in valid],
            key=lambda x: -x["code_lines"],
        )[:10],
    }


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: package_overview.py <path>", file=sys.stderr)
        raise SystemExit(2)

    root = Path(sys.argv[1]).resolve()
    first_party, py_files = walk_package(root)

    modules = [analyse_module(p, first_party) for p in py_files]
    output = {
        "root": str(root),
        "first_party_package": first_party,
        "summary": summarise(modules),
        "modules": modules,
    }
    json.dump(output, sys.stdout, indent=2)


if __name__ == "__main__":
    main()
