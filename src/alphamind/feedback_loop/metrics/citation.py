"""Registry discovery shim for the citation-chain metrics (ALP-886 story 06d).

The citation-chain metric cores live in
:mod:`alphamind.feedback_loop.citation.chain` (the cross-layer flagship's home,
alongside its reference-ID parser), but the metric registry discovers ``METRICS``
tuples by walking :mod:`alphamind.feedback_loop.metrics`. This module re-exports
the citation chain's tuple under that package so the registry finds it with zero
edits to the registry — the same self-registration contract every metric story
follows (parent ALP-131 pre-resolved (G)).
"""

from __future__ import annotations

from alphamind.feedback_loop.citation.chain import METRICS

__all__ = ["METRICS"]
