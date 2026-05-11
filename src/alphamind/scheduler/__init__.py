"""AlphaMind pipeline scheduler package (story 01 — ALP-442).

Public surface re-exported here mirrors what the story acceptance
criteria pin: ``PipelineSession`` + ``new_session`` for per-process
handles, ``PipelineSupervisor`` for asyncio task supervision, and
``configure_pipeline_logging`` for the standard log file shape. See
``docs/architecture/infrastructure.md`` § Process supervision and
``docs/design/05-execution-layer/state-persistence.md`` for the
surrounding design contract.
"""

from alphamind.scheduler.logging_setup import configure_pipeline_logging
from alphamind.scheduler.session import PipelineSession, new_session
from alphamind.scheduler.supervisor import PipelineSupervisor

__all__ = [
    "PipelineSession",
    "PipelineSupervisor",
    "configure_pipeline_logging",
    "new_session",
]
