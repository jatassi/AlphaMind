"""Structural Protocols for the design's position/order/thesis abstractions.

Per ``docs/design/05-execution-layer/position-model.md`` § Base position, the
position layer exposes "an instrument hierarchy with a shared base interface".
The Protocols in this package formalise those interfaces so consumers reasoning
generically about a position need not depend on the concrete record/view
shapes.
"""

from __future__ import annotations

from alphamind.portfolio_state.protocols.positions import BasePositionProtocol

__all__ = ["BasePositionProtocol"]
