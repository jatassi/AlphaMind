"""Feedback loop — placeholder namespace (scheduled for ALP-131)."""

from __future__ import annotations


def __getattr__(name: str) -> object:
    raise NotImplementedError("feedback_loop scheduled for ALP-131")
