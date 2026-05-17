"""Debug-e2e mode package — two-symbol public surface (story ALP-500 / 03).

Per the design doc § 6 P9, only the factory and the settings dataclass
are exported. The seeder, log-only broker shims, JSONL emitter, and
synthetic-portfolio constants live in their own submodules and are
imported directly by the CLI wiring (story 04) where each is needed.
"""

from alphamind.scheduler.debug_e2e.settings import (
    DebugE2ESettings,
    configure_debug_e2e,
)

__all__ = ["DebugE2ESettings", "configure_debug_e2e"]
