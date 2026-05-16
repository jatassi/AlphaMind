"""UTF-8 stdio bootstrap for operator verification scripts.

Windows defaults ``sys.stdout`` / ``sys.stderr`` to cp1252; verification
scripts routinely print Greek letters, smart quotes, em-dashes, and the
ad-hoc Unicode that LLM-produced briefs contain. Without reconfiguration
``str.encode`` against the default codec raises ``UnicodeEncodeError``
and loses the in-memory result.

Call :func:`configure_utf8_stdio` as the first statement of every
``verify_*`` ``main()`` so Windows and POSIX behave identically without
operator env vars.
"""

from __future__ import annotations

import sys


def configure_utf8_stdio() -> None:
    """Switch ``sys.stdout`` and ``sys.stderr`` to UTF-8 with replace errors.

    Streams without a ``.reconfigure`` method are left as-is. ``replace``
    is precautionary — Python ``str`` always round-trips through UTF-8,
    so it only fires for stdin reads of malformed bytes.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
