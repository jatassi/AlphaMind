"""Tests for the venue_configuration package shell (story ALP-378).

Story 01 only ships the package skeleton with a docstring; populated symbol
list is deferred to story 02g (ALP-385).
"""

from __future__ import annotations


def test_package_module_importable() -> None:
    import alphamind.execution.venue_configuration as venue_cfg

    assert venue_cfg is not None


def test_package_has_docstring_naming_role() -> None:
    """The package's ``__doc__`` must reference its design-doc role."""
    import alphamind.execution.venue_configuration as venue_cfg

    doc = venue_cfg.__doc__ or ""
    # Substrate fingerprint — names the package's role per
    # ``venue-configuration.md``.
    assert "venue" in doc.lower()
    assert len(doc.strip()) > 30  # not a one-liner placeholder
