"""Shared helpers across the three decision-layer agent harnesses.

Currently houses :func:`system_prompt_as_file` — a context manager that moves
the system-prompt text from the SDK subprocess argv to a tempfile so the
analyst / strategist / PM stay under the Windows ``CreateProcessW`` 32,767
character cmdline limit when paired with their JSON-Schema ``output_format``.
"""

from alphamind.decision._shared.prompt_file import system_prompt_as_file

__all__ = ["system_prompt_as_file"]
