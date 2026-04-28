"""Operator verification scripts (story 13).

The modules under ``alphamind.scripts`` carry the testable logic of the
``scripts/verify_*.py`` shims at the repository root. Each shim is a
thin wrapper around its corresponding module's ``main()`` entry point so
the operator runs the script via ``uv run python scripts/verify_<x>.py``
while the test suite imports the module directly.
"""
