"""Module entry point: `python -m alphamind.distillation.replay_harness`.

Delegates to `cli.main()` and exits with its return code.
"""

from __future__ import annotations

import sys

from alphamind.distillation.replay_harness.cli import main

if __name__ == "__main__":
    sys.exit(main())
