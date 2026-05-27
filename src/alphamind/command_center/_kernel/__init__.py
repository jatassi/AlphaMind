"""Bounded-context kernel for the command-center package.

Holds the typed primitives the rest of ``command_center/`` consumes —
identifier NewTypes (``ids``), control vocabulary (``control``), event
shapes (``events``), and the per-operator-action invocation handle
(``operator_invocation``). Mirrors the global :mod:`alphamind._kernel`
discipline at package scope: pure values, no I/O, no sibling-package
imports.
"""
