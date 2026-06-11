"""Rendered tool-name selection shared by the analyst and strategist runners.

The ``retrieve_options_chain`` tool (ALP-948) mounts only when the composition
supplies an :class:`~alphamind.state.repository.options_chain_read.OptionsChainReader`;
the AVAILABLE TOOLS render must match the mounted surface so the prompt never
advertises an uncallable tool.
"""

from __future__ import annotations

from alphamind.state.repository.options_chain_read import OptionsChainReader
from alphamind.state.repository.options_chain_tool_mcp import (
    RETRIEVE_OPTIONS_CHAIN_TOOL_NAME,
)

__all__ = ["surface_tool_names"]


def surface_tool_names(
    tool_names: tuple[str, ...],
    options_chain_reader: OptionsChainReader | None,
) -> tuple[str, ...]:
    """*tool_names* narrowed to this composition's mounted surface."""
    if options_chain_reader is not None:
        return tool_names
    return tuple(name for name in tool_names if name != RETRIEVE_OPTIONS_CHAIN_TOOL_NAME)
