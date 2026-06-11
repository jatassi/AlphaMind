"""Dead-pointer guard for navigation docs: CLAUDE.md files, runbooks, operate-prod.

Every path referenced in a scanned navigation doc must resolve to a real file or
directory. Stale pointers are the worst documentation failure mode — they send agents on
wild-goose chases — so this turns a dead reference into a red CI check.

Scanned docs: every tracked ``CLAUDE.md``, every ``docs/runbooks/**/*.md`` (the prod
operational runbooks form a pointer web with the scripts they invoke), and
``.claude/skills/operate-prod/SKILL.md`` (a pure router over the runbooks).

What is checked, per doc:
  * Markdown link targets ``[text](path)`` (skipping URLs and ``#anchors``).
  * Inline ``code`` spans **outside** fenced ``` code blocks that look like repo paths.

Fenced code blocks are skipped on purpose: they hold commands and placeholder examples
(``tests/<sub-path>/``, ``test_x.py::test_case``) that are not real paths. Navigation
pointers that we want guaranteed live in prose / tables as inline code or links.

A candidate resolves if it exists relative to any of: the repo root, the doc's
own directory, or ``src/alphamind/`` (so ``execution/`` shorthand for a package resolves).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "alphamind"

# Vendored / copy directories that must not be scanned. Judged on the path RELATIVE to
# REPO_ROOT — an absolute-parts check would match the ``.claude`` in
# ``.claude/worktrees/<name>/...`` and silently exclude *every* doc when the suite runs
# inside a worktree checkout. ``.claude`` itself is only excluded for worktree copies
# (``.claude/worktrees/``); tracked skill files under ``.claude/skills/`` are scannable.
_VENDORED_PARTS = {"node_modules", ".git"}


def _excluded(f: Path) -> bool:
    rel_parts = f.relative_to(REPO_ROOT).parts
    if _VENDORED_PARTS & set(rel_parts):
        return True
    return ".claude" in rel_parts and "worktrees" in rel_parts


_FENCED_BLOCK = re.compile(r"```.*?```", re.DOTALL)
_MD_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
_INLINE_CODE = re.compile(r"`([^`]+)`")

# A token is "path-like" if it has no shell/placeholder metacharacters, contains a
# slash, and either ends in a slash or carries a known repo file extension.
_PATHISH = re.compile(r"^[A-Za-z0-9_./-]+$")
_EXTENSIONS = (
    ".md",
    ".py",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".lock",
    ".txt",
    ".ts",
    ".tsx",
    ".js",
    ".json",
    ".ps1",
    ".cfg",
)


def _scanned_docs() -> list[Path]:
    files = [REPO_ROOT / "CLAUDE.md"]
    files += sorted(SRC_ROOT.rglob("CLAUDE.md"))
    files += sorted((REPO_ROOT / "docs").rglob("CLAUDE.md"))
    files += sorted((REPO_ROOT / "docs" / "runbooks").rglob("*.md"))
    # operate-prod is the one router-class skill: every path in it is a navigation
    # pointer, so the pathish heuristic applies cleanly. Other skills' prose carries
    # illustrative example paths and runtime-artifact paths (audited 2026-06-10:
    # 12 false positives vs 1 real catch across the other 13 skills), so they stay
    # out of the scan set; add a skill here only if it is likewise pure navigation.
    files.append(REPO_ROOT / ".claude" / "skills" / "operate-prod" / "SKILL.md")
    deduped = list(dict.fromkeys(files))
    return [f for f in deduped if f.exists() and not _excluded(f)]


def _is_pathish(token: str) -> bool:
    if "/" not in token or not _PATHISH.match(token):
        return False
    return token.endswith("/") or token.endswith(_EXTENSIONS)


def _candidates(text: str) -> set[str]:
    body = _FENCED_BLOCK.sub("", text)
    found: set[str] = set()

    for target in _MD_LINK.findall(body):
        target = target.split("#", 1)[0].strip()
        if not target or "://" in target or target.startswith("mailto:"):
            continue
        found.add(target)

    for span in _INLINE_CODE.findall(body):
        span = span.strip().rstrip(".,;:")
        if _is_pathish(span):
            found.add(span)

    # Absolute paths (leading ``/``) are external machine references — e.g. the
    # production-host ``/Volumes/Users/jacks/AlphaMind/logs/`` paths documented
    # for operators — not repo navigation pointers. The resolver only checks
    # repo-relative bases, so an absolute path can never resolve; flagging it
    # would false-positive on any clean (non-``.claude``) checkout, e.g. Windows
    # CI. Repo navigation always uses relative paths, so drop the absolutes.
    return {ref for ref in found if not ref.startswith("/")}


def _resolves(doc: Path, ref: str) -> bool:
    ref = ref.lstrip("/") if not ref.startswith(("./", "../")) else ref
    return any((base / ref).exists() for base in (REPO_ROOT, doc.parent, SRC_ROOT))


def test_absolute_machine_paths_are_not_flagged() -> None:
    """Absolute paths are external machine references, not repo pointers (ALP-755).

    The root ``CLAUDE.md`` documents production-machine locations such as
    ``/Volumes/Users/jacks/AlphaMind/logs/``. Those are operator references to
    a different host's filesystem, not repo navigation pointers — the resolver
    only checks repo-relative bases, so an absolute path can never resolve and
    would false-positive on a clean (non-``.claude``) checkout like Windows CI.
    They must be excluded from the candidate set while relative repo pointers
    stay covered.
    """
    body = (
        "- Logs: `/Volumes/Users/jacks/AlphaMind/logs/` and `src/alphamind/` "
        "plus `docs/agents/linear.md`."
    )
    candidates = _candidates(body)
    assert "/Volumes/Users/jacks/AlphaMind/logs/" not in candidates
    assert "src/alphamind/" in candidates
    assert "docs/agents/linear.md" in candidates


def test_claude_md_files_exist() -> None:
    """At minimum the root navigation file must be present."""
    assert (REPO_ROOT / "CLAUDE.md").exists()


def test_runbook_web_is_in_scan_set() -> None:
    """The runbook map and the operate-prod router must exist and be scanned.

    These two docs anchor the prod-operations pointer web (the runbook split):
    ``docs/runbooks/README.md`` maps the runbooks, and the ``operate-prod`` skill
    routes situations onto them. If either goes missing — or the collector stops
    picking them up — the guard would silently shrink its coverage.
    """
    scanned = set(_scanned_docs())
    assert REPO_ROOT / "docs" / "runbooks" / "README.md" in scanned
    assert REPO_ROOT / ".claude" / "skills" / "operate-prod" / "SKILL.md" in scanned


def test_no_dead_pointers_in_nav_docs() -> None:
    dead: list[str] = []
    for doc in _scanned_docs():
        rel_doc = doc.relative_to(REPO_ROOT)
        for ref in sorted(_candidates(doc.read_text(encoding="utf-8"))):
            if not _resolves(doc, ref):
                dead.append(f"{rel_doc} -> {ref}")

    assert not dead, "Dead pointers in navigation docs:\n" + "\n".join(dead)


_SECTION_REF = re.compile(r"([a-z][a-z0-9-]*\.md) § (\d+(?:\.\d+)?)")


def test_runbook_section_refs_resolve() -> None:
    """Cross-doc ``<doc>.md § N[.M]`` references must name a real heading.

    The runbook split keeps the monolith's section numbering precisely so these
    prose references stay stable. Each one is validated against the target doc's
    headings (``## N.`` top level, ``### N.M`` subsection) — a renumbering or
    deletion in one runbook cannot silently strand the docs pointing at it.
    Fenced blocks are included on purpose: operators copy-paste from them.
    """
    runbooks = REPO_ROOT / "docs" / "runbooks"
    dead: list[str] = []
    for doc in sorted(runbooks.glob("*.md")):
        for name, sec in _SECTION_REF.findall(doc.read_text(encoding="utf-8")):
            target = runbooks / name
            if not target.exists():
                dead.append(f"{doc.name} -> {name} § {sec} (no such runbook)")
                continue
            heading = rf"^### {re.escape(sec)} " if "." in sec else rf"^## {sec}\. "
            if not re.search(heading, target.read_text(encoding="utf-8"), re.MULTILINE):
                dead.append(f"{doc.name} -> {name} § {sec} (no such heading)")

    assert not dead, "Dangling section references in runbooks:\n" + "\n".join(dead)
