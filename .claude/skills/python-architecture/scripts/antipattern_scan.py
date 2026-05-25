#!/usr/bin/env python3
"""Scan a Python package for the deterministic antipatterns from references/antipatterns.md.

Usage: python antipattern_scan.py <path>

Output: JSON to stdout, list of findings. Each finding has:
  - antipattern_id (e.g., 'L3' for mutable default arguments)
  - file (relative to scan root)
  - line
  - source (the matching source line)
  - note (optional: clarification)

This script is intentionally conservative — false positives are worse than
false negatives. The agent uses the findings as a starting point and
verifies them against the actual code.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path


def _line(source: str, lineno: int) -> str:
    lines = source.splitlines()
    if 1 <= lineno <= len(lines):
        return lines[lineno - 1].rstrip()
    return ""


# ---------- L3: mutable default arguments ----------
def scan_mutable_defaults(tree: ast.AST, source: str, path: str) -> list[dict]:
    findings: list[dict] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for default in node.args.defaults + node.args.kw_defaults:
                if default is None:
                    continue
                if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                    findings.append({
                        "antipattern_id": "L3",
                        "file": path,
                        "line": default.lineno,
                        "source": _line(source, default.lineno),
                        "note": "mutable default argument",
                    })
                elif isinstance(default, ast.Call):
                    if isinstance(default.func, ast.Name) and default.func.id in {"list", "dict", "set"}:
                        if not default.args and not default.keywords:
                            findings.append({
                                "antipattern_id": "L3",
                                "file": path,
                                "line": default.lineno,
                                "source": _line(source, default.lineno),
                                "note": f"mutable default via {default.func.id}()",
                            })
    return findings


# ---------- L4: bare except: or except Exception (heuristic — flag all, agent filters) ----------
def scan_broad_except(tree: ast.AST, source: str, path: str) -> list[dict]:
    findings: list[dict] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            if node.type is None:
                findings.append({
                    "antipattern_id": "L4",
                    "file": path,
                    "line": node.lineno,
                    "source": _line(source, node.lineno),
                    "note": "bare except:",
                })
            elif isinstance(node.type, ast.Name) and node.type.id in {"Exception", "BaseException"}:
                findings.append({
                    "antipattern_id": "L4",
                    "file": path,
                    "line": node.lineno,
                    "source": _line(source, node.lineno),
                    "note": f"except {node.type.id} (verify it's not the outer supervisor)",
                })
    return findings


# ---------- L5: from x import * ----------
def scan_star_imports(tree: ast.AST, source: str, path: str) -> list[dict]:
    findings: list[dict] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    findings.append({
                        "antipattern_id": "L5",
                        "file": path,
                        "line": node.lineno,
                        "source": _line(source, node.lineno),
                        "note": f"from {node.module} import *",
                    })
    return findings


# ---------- L8: mutable dataclass without frozen=True ----------
def scan_mutable_dataclass(tree: ast.AST, source: str, path: str) -> list[dict]:
    findings: list[dict] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for deco in node.decorator_list:
                # Match @dataclass and @dataclass(...)
                target = deco.func if isinstance(deco, ast.Call) else deco
                if not isinstance(target, (ast.Name, ast.Attribute)):
                    continue
                name = target.id if isinstance(target, ast.Name) else target.attr
                if name != "dataclass":
                    continue
                # Check kwargs for frozen=True
                kwargs = deco.keywords if isinstance(deco, ast.Call) else []
                frozen = any(
                    kw.arg == "frozen"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value is True
                    for kw in kwargs
                )
                if not frozen:
                    findings.append({
                        "antipattern_id": "L8",
                        "file": path,
                        "line": node.lineno,
                        "source": _line(source, node.lineno),
                        "note": f"@dataclass on {node.name} without frozen=True",
                    })
    return findings


# ---------- L9: Pydantic BaseModel outside boundary modules (heuristic) ----------
BOUNDARY_DIRS = {
    "api", "schema", "schemas", "io", "adapter", "adapters",
    "config", "settings", "ingest", "boundary", "dto", "dtos",
}


def scan_pydantic_internal(tree: ast.AST, source: str, path: str, abs_path: str) -> list[dict]:
    findings: list[dict] = []
    # Match against path *components*, not substrings — otherwise hints like
    # "io" false-positive on directory names like "sessions" or "fixtures".
    # If any directory in the file's path is a known boundary location, skip.
    # Normalise backslashes so the split works on Windows where str(Path) uses '\'.
    parts = abs_path.lower().replace("\\", "/").split("/")
    if any(p in BOUNDARY_DIRS for p in parts[:-1]):
        return findings
    # Look for class X(BaseModel) or class X(pydantic.BaseModel)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                base_name = ""
                if isinstance(base, ast.Name):
                    base_name = base.id
                elif isinstance(base, ast.Attribute):
                    base_name = base.attr
                if base_name == "BaseModel":
                    findings.append({
                        "antipattern_id": "L9",
                        "file": path,
                        "line": node.lineno,
                        "source": _line(source, node.lineno),
                        "note": f"Pydantic BaseModel '{node.name}' outside boundary modules",
                    })
    return findings


# ---------- L11: naive datetime.now() / datetime.utcnow() ----------
NAIVE_DATETIME_RE = re.compile(r"\bdatetime(?:\.datetime)?\.(?:now\s*\(\s*\)|utcnow\s*\()")


def scan_naive_datetime(source: str, path: str) -> list[dict]:
    findings: list[dict] = []
    for i, line in enumerate(source.splitlines(), start=1):
        if NAIVE_DATETIME_RE.search(line):
            # Filter false positive: datetime.now(timezone.utc) — but the regex above doesn't match that
            findings.append({
                "antipattern_id": "L11",
                "file": path,
                "line": i,
                "source": line.rstrip(),
                "note": "naive datetime — use datetime.now(timezone.utc)",
            })
    return findings


# ---------- L12: float for monetary fields (heuristic by name) ----------
MONEY_NAMES = {"price", "amount", "total", "fee", "cost", "balance", "revenue", "expense"}


def scan_float_money(tree: ast.AST, source: str, path: str) -> list[dict]:
    findings: list[dict] = []
    for node in ast.walk(tree):
        # Annotated function arguments
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in node.args.args + node.args.kwonlyargs + node.args.posonlyargs:
                if arg.annotation is None:
                    continue
                name_lower = arg.arg.lower()
                if name_lower in MONEY_NAMES or any(m in name_lower for m in MONEY_NAMES):
                    if isinstance(arg.annotation, ast.Name) and arg.annotation.id == "float":
                        findings.append({
                            "antipattern_id": "L12",
                            "file": path,
                            "line": arg.lineno,
                            "source": _line(source, arg.lineno),
                            "note": f"{arg.arg}: float — money should be Decimal",
                        })
        # Annotated assignments at class or module level
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name_lower = node.target.id.lower()
            if name_lower in MONEY_NAMES or any(m in name_lower for m in MONEY_NAMES):
                if isinstance(node.annotation, ast.Name) and node.annotation.id == "float":
                    findings.append({
                        "antipattern_id": "L12",
                        "file": path,
                        "line": node.lineno,
                        "source": _line(source, node.lineno),
                        "note": f"{node.target.id}: float — money should be Decimal",
                    })
    return findings


# ---------- L16: asyncio.create_task without TaskGroup parent (heuristic) ----------
def scan_create_task(source: str, path: str) -> list[dict]:
    """Heuristic: every asyncio.create_task call that isn't on a 'tg.' / 'task_group.' / 'group.' receiver."""
    findings: list[dict] = []
    for i, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if "asyncio.create_task(" in stripped:
            findings.append({
                "antipattern_id": "L16",
                "file": path,
                "line": i,
                "source": stripped,
                "note": "asyncio.create_task — confirm a TaskGroup parent supervises it",
            })
    return findings


# ---------- L17: external call without timeout (heuristic) ----------
EXTERNAL_CALL_RE = re.compile(
    r"\b(httpx|requests|aiohttp)\.(?:get|post|put|delete|patch|request|head|options)\s*\("
)


def scan_no_timeout(source: str, path: str) -> list[dict]:
    findings: list[dict] = []
    for i, line in enumerate(source.splitlines(), start=1):
        m = EXTERNAL_CALL_RE.search(line)
        if not m:
            continue
        # Crude: check the next 5 lines for a timeout= kwarg
        snippet = "\n".join(source.splitlines()[i - 1 : i + 4])
        if "timeout" not in snippet:
            findings.append({
                "antipattern_id": "L17",
                "file": path,
                "line": i,
                "source": line.rstrip(),
                "note": f"{m.group(1)} call without visible timeout=",
            })
    return findings


# ---------- L19: async def without await (rough heuristic) ----------
def scan_async_no_await(tree: ast.AST, source: str, path: str) -> list[dict]:
    findings: list[dict] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            has_await = False
            for sub in ast.walk(node):
                if isinstance(sub, (ast.Await, ast.AsyncFor, ast.AsyncWith)):
                    has_await = True
                    break
            if not has_await:
                findings.append({
                    "antipattern_id": "L19",
                    "file": path,
                    "line": node.lineno,
                    "source": _line(source, node.lineno),
                    "note": f"async def {node.name} contains no await",
                })
    return findings


# ---------- L20: free-text logging.info(f"...") ----------
LOG_FSTRING_RE = re.compile(r"\b(log(?:ger|ging)?|log)\.(debug|info|warning|warn|error|critical)\s*\(\s*f['\"]")


def scan_fstring_log(source: str, path: str) -> list[dict]:
    findings: list[dict] = []
    for i, line in enumerate(source.splitlines(), start=1):
        if LOG_FSTRING_RE.search(line):
            findings.append({
                "antipattern_id": "L20",
                "file": path,
                "line": i,
                "source": line.rstrip(),
                "note": "f-string log — prefer structured fields",
            })
    return findings


# ---------- L21: mock.patch on third-party library ----------
THIRD_PARTY_PATCH_HINTS = (
    "httpx.", "requests.", "aiohttp.", "redis.", "sqlalchemy.", "boto3.", "pymongo.",
)


def scan_third_party_patch(source: str, path: str) -> list[dict]:
    findings: list[dict] = []
    for i, line in enumerate(source.splitlines(), start=1):
        for hint in THIRD_PARTY_PATCH_HINTS:
            if f'patch("{hint}' in line or f"patch('{hint}" in line:
                findings.append({
                    "antipattern_id": "L21",
                    "file": path,
                    "line": i,
                    "source": line.rstrip(),
                    "note": f"mock.patch of third-party library ({hint.rstrip('.')})",
                })
                break
    return findings


# ---------- L25: print() in non-CLI code ----------
def scan_print(tree: ast.AST, source: str, path: str) -> list[dict]:
    """Flag print() calls. Heuristic: skip files under cli/ or scripts/ and __main__.py."""
    findings: list[dict] = []
    # Match directory *components* (not substrings) so the check works for both
    # top-level dirs (`cli/foo.py`) and nested dirs (`distillation/cli/foo.py`).
    # Normalise backslashes so Windows-style relative paths (`cli\\foo.py`) are
    # handled identically. The prior `"/cli/" in path` substring check missed
    # both top-level dirs (no leading slash in `rel`) and Windows paths.
    parts = path.replace("\\", "/").split("/")
    if "cli" in parts[:-1] or "scripts" in parts[:-1] or parts[-1] == "__main__.py":
        return findings
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
            findings.append({
                "antipattern_id": "L25",
                "file": path,
                "line": node.lineno,
                "source": _line(source, node.lineno),
                "note": "print() — use logging",
            })
    return findings


# ---------- L26: pandas iterrows / itertuples ----------
PANDAS_ROW_RE = re.compile(r"\.iterrows\s*\(|\.itertuples\s*\(")


def scan_pandas_iterrows(source: str, path: str) -> list[dict]:
    findings: list[dict] = []
    for i, line in enumerate(source.splitlines(), start=1):
        if PANDAS_ROW_RE.search(line):
            findings.append({
                "antipattern_id": "L26",
                "file": path,
                "line": i,
                "source": line.rstrip(),
                "note": "pandas row iteration — vectorise",
            })
    return findings


def scan_module(path: Path, root: Path) -> list[dict]:
    rel = str(path.relative_to(root))
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, UnicodeDecodeError) as e:
        return [{
            "antipattern_id": "PARSE_ERROR",
            "file": rel,
            "line": 0,
            "source": "",
            "note": str(e),
        }]

    findings: list[dict] = []
    findings.extend(scan_mutable_defaults(tree, source, rel))
    findings.extend(scan_broad_except(tree, source, rel))
    findings.extend(scan_star_imports(tree, source, rel))
    findings.extend(scan_mutable_dataclass(tree, source, rel))
    findings.extend(scan_pydantic_internal(tree, source, rel, str(path)))
    findings.extend(scan_naive_datetime(source, rel))
    findings.extend(scan_float_money(tree, source, rel))
    findings.extend(scan_create_task(source, rel))
    findings.extend(scan_no_timeout(source, rel))
    findings.extend(scan_async_no_await(tree, source, rel))
    findings.extend(scan_fstring_log(source, rel))
    findings.extend(scan_third_party_patch(source, rel))
    findings.extend(scan_print(tree, source, rel))
    findings.extend(scan_pandas_iterrows(source, rel))
    return findings


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: antipattern_scan.py <path>", file=sys.stderr)
        raise SystemExit(2)

    root = Path(sys.argv[1]).resolve()
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        raise SystemExit(2)

    py_files = [
        p
        for p in root.rglob("*.py")
        if "__pycache__" not in p.parts and ".venv" not in p.parts
    ]

    all_findings: list[dict] = []
    for path in py_files:
        all_findings.extend(scan_module(path, root))

    by_id: dict[str, int] = {}
    for f in all_findings:
        by_id[f["antipattern_id"]] = by_id.get(f["antipattern_id"], 0) + 1

    output = {
        "root": str(root),
        "total_findings": len(all_findings),
        "by_antipattern": dict(sorted(by_id.items())),
        "findings": all_findings,
    }
    json.dump(output, sys.stdout, indent=2)


if __name__ == "__main__":
    main()
