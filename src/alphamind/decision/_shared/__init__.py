"""Shared helpers across the three decision-layer agent harnesses.

Houses :func:`system_prompt_as_file` — a context manager that moves the
system-prompt text from the SDK subprocess argv to a tempfile so the
analyst / strategist / PM stay under the Windows ``CreateProcessW`` 32,767
character cmdline limit when paired with their JSON-Schema ``output_format`` —
and :func:`direction_display`, the single source of the ``Underlying:`` line
direction word shared by the strategist and PM input bundles.
"""

from alphamind.decision._shared.direction_display import direction_display
from alphamind.decision._shared.prompt_file import system_prompt_as_file
from alphamind.decision._shared.tool_surface import surface_tool_names

__all__ = ["direction_display", "surface_tool_names", "system_prompt_as_file"]
