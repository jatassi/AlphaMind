"""UTF-8 stdio bootstrap for operator verification scripts (ALP-491).

Windows defaults ``sys.stdout`` / ``sys.stderr`` to cp1252; verification
scripts routinely print Greek letters, smart quotes, em-dashes, and the
ad-hoc Unicode that LLM-produced briefs contain. Without reconfiguration
those bytes trip ``UnicodeEncodeError`` and lose the in-memory result.

Call :func:`configure_utf8_stdio` as the first statement of every
``verify_*`` ``main()`` so Windows and POSIX behave identically without
operator env vars.
"""

from __future__ import annotations

import sys


def configure_utf8_stdio() -> None:
    """Force ``sys.stdout`` and ``sys.stderr`` to UTF-8 with replace errors.

    No-op for streams without ``.reconfigure`` (notably pytest's capture
    wrappers and any third-party replacement that lacks the method).
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
