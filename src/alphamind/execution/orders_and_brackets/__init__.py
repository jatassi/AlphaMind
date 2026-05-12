"""Orders and brackets — placeholder namespace (partially shipped as ALP-122).

The bracket-order types (BracketOrderParameters, BracketOrderType) live in
alphamind.execution.oms.command_models; this package is an empty skeleton
retained as a namespace anchor.
"""

from __future__ import annotations


def __getattr__(name: str) -> object:
    raise NotImplementedError(
        "orders_and_brackets scheduled for ALP-122; "
        "bracket-order types live in alphamind.execution.oms"
    )
