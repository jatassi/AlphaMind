#!/usr/bin/env python3
"""Build the import graph for a Python package and detect cycles.

Usage: python analyze_imports.py <path_to_package_root>

Output: JSON to stdout with:
  - adjacency: {module: [imported_first_party_modules]}
  - cycles: list of cycles found (each cycle is a list of modules)
  - layer_violation_candidates: modules whose imports cross likely layer boundaries
    (heuristic: if path contains 'domain' or 'core' and imports something containing
    'adapters' or 'infrastructure' or 'api', flag it)

If `import-linter` is configured (`.importlinter` or [tool.importlinter] in
pyproject.toml), the script reports the existence of contracts but doesn't run
the linter (the agent should run `lint-imports` separately if it's set up).

Only first-party imports go in the graph; the third-party noise is filtered.
"""

from __future__ import annotations

import ast
import json
import sys
from collections import defaultdict
from pathlib import Path


def module_name_from_path(path: Path, root: Path, first_party: str) -> str:
    """Map a file path to its dotted module name."""
    rel = path.relative_to(root)
    parts = list(rel.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return first_party
    return first_party + "." + ".".join(parts)


def imports_from(tree: ast.AST, current_module: str, first_party: str) -> set[str]:
    """Extract first-party module dependencies from an AST."""
    deps: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == first_party or alias.name.startswith(first_party + "."):
                    deps.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                # Relative import: resolve against current_module
                base_parts = current_module.split(".")
                # node.level=1 means "from . import x", level=2 means "from .. import x"
                base = base_parts[: -node.level] if node.level <= len(base_parts) else []
                if node.module:
                    deps.add(".".join(base + [node.module]))
                else:
                    deps.add(".".join(base))
            elif node.module and (
                node.module == first_party or node.module.startswith(first_party + ".")
            ):
                deps.add(node.module)
    # Trim self-loops (relative resolution can produce them in __init__ files)
    deps.discard(current_module)
    return deps


def find_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan's strongly connected components → cycles of length >= 2 or self-loops."""
    index_counter = [0]
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    sccs: list[list[str]] = []

    def strongconnect(node: str) -> None:
        indices[node] = index_counter[0]
        lowlinks[node] = index_counter[0]
        index_counter[0] += 1
        stack.append(node)
        on_stack.add(node)

        for successor in graph.get(node, ()):
            if successor not in indices:
                strongconnect(successor)
                lowlinks[node] = min(lowlinks[node], lowlinks[successor])
            elif successor in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[successor])

        if lowlinks[node] == indices[node]:
            component: list[str] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                component.append(w)
                if w == node:
                    break
            if len(component) > 1 or (len(component) == 1 and component[0] in graph.get(component[0], ())):
                sccs.append(component)

    sys.setrecursionlimit(max(1000, sys.getrecursionlimit()))
    for node in list(graph.keys()):
        if node not in indices:
            strongconnect(node)

    return sccs


LAYER_INNER_HINTS = ("domain", "core")
LAYER_OUTER_HINTS = ("adapters", "infrastructure", "api", "routes", "web", "cli")


def layer_violation(source: str, target: str) -> bool:
    """Heuristic: is this an inner-importing-outer dependency?"""
    src_inner = any(h in source.lower() for h in LAYER_INNER_HINTS)
    tgt_outer = any(h in target.lower() for h in LAYER_OUTER_HINTS)
    return src_inner and tgt_outer


def detect_import_linter_config(project_root: Path) -> dict:
    """Look for import-linter config and report what's there. We don't run it."""
    candidates = [
        project_root / ".importlinter",
        project_root / "importlinter.cfg",
        project_root / "pyproject.toml",
    ]
    for c in candidates:
        if c.exists():
            text = c.read_text(encoding="utf-8")
            if c.name == "pyproject.toml":
                if "[tool.importlinter]" in text or "[importlinter]" in text:
                    return {"found": True, "path": str(c)}
            else:
                return {"found": True, "path": str(c)}
    return {"found": False, "path": None}


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: analyze_imports.py <path>", file=sys.stderr)
        raise SystemExit(2)

    root = Path(sys.argv[1]).resolve()
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        raise SystemExit(2)

    # Walk up the directory tree to find the top-level package — the directory
    # whose parent does NOT contain an __init__.py. This lets the script be
    # invoked on a subpackage and still resolve dotted module names correctly.
    pkg_root = root
    while (pkg_root.parent / "__init__.py").exists():
        pkg_root = pkg_root.parent
    first_party = pkg_root.name
    pkg_root_parent = pkg_root.parent

    def module_name_for_file(path: Path) -> str:
        rel = path.relative_to(pkg_root_parent)
        parts = list(rel.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts)

    py_files = [
        p
        for p in root.rglob("*.py")
        if "__pycache__" not in p.parts and ".venv" not in p.parts
    ]

    graph: dict[str, set[str]] = defaultdict(set)
    parse_errors: list[dict] = []

    for path in py_files:
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as e:
            parse_errors.append({"path": str(path), "error": str(e)})
            continue

        module_name = module_name_for_file(path)
        deps = imports_from(tree, module_name, first_party)
        graph[module_name] |= deps

    cycles = find_cycles(graph)

    violations: list[dict] = []
    for source, targets in graph.items():
        for target in targets:
            if layer_violation(source, target):
                violations.append({"source": source, "target": target})

    project_root = root
    while project_root.parent != project_root:
        if (project_root / "pyproject.toml").exists():
            break
        project_root = project_root.parent

    output = {
        "root": str(root),
        "first_party_package": first_party,
        "import_linter_config": detect_import_linter_config(project_root),
        "module_count": len(graph),
        "edge_count": sum(len(v) for v in graph.values()),
        "cycles": cycles,
        "layer_violation_candidates": violations,
        "parse_errors": parse_errors,
        "adjacency": {k: sorted(v) for k, v in sorted(graph.items())},
    }
    json.dump(output, sys.stdout, indent=2)


if __name__ == "__main__":
    main()
