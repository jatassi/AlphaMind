"""Dead-pointer guard for CLAUDE.md navigation files.

Every path referenced in a tracked ``CLAUDE.md`` must resolve to a real file or
directory. Stale pointers are the worst documentation failure mode — they send agents on
wild-goose chases — so this turns a dead reference into a red CI check.

What is checked, per ``CLAUDE.md``:
  * Markdown link targets ``[text](path)`` (skipping URLs and ``#anchors``).
  * Inline ``code`` spans **outside** fenced ``` code blocks that look like repo paths.

Fenced code blocks are skipped on purpose: they hold commands and placeholder examples
(``tests/<sub-path>/``, ``test_x.py::test_case``) that are not real paths. Navigation
pointers that we want guaranteed live in prose / tables as inline code or links.

A candidate resolves if it exists relative to any of: the repo root, the ``CLAUDE.md``'s
own directory, or ``src/alphamind/`` (so ``execution/`` shorthand for a package resolves).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "alphamind"

# Directories whose CLAUDE.md files are copies / vendored and must not be scanned.
_EXCLUDED_PARTS = {".claude", "node_modules", ".git"}

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


def _claude_md_files() -> list[Path]:
    files = [REPO_ROOT / "CLAUDE.md"]
    files += sorted(SRC_ROOT.rglob("CLAUDE.md"))
    files += sorted((REPO_ROOT / "docs").rglob("CLAUDE.md"))
    return [f for f in files if f.exists() and not (set(f.parts) & _EXCLUDED_PARTS)]


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


def test_no_dead_pointers_in_claude_md() -> None:
    dead: list[str] = []
    for doc in _claude_md_files():
        rel_doc = doc.relative_to(REPO_ROOT)
        for ref in sorted(_candidates(doc.read_text(encoding="utf-8"))):
            if not _resolves(doc, ref):
                dead.append(f"{rel_doc} -> {ref}")

    assert not dead, "Dead pointers in CLAUDE.md files:\n" + "\n".join(dead)
