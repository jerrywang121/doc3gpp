"""Background job worker for the ``doc3gpp server`` surface.

T7 wires the asyncio worker loop (:mod:`doc3gpp.web.workers.job_worker`)
to the per-kind handlers (:mod:`doc3gpp.web.workers.handlers`). The
handlers module is the only place that maps a :class:`JobKind` to the
service method that performs the work — new job kinds land there.
"""

from doc3gpp.web.workers.handlers import JobHandlers
from doc3gpp.web.workers.job_worker import JobWorker

__all__ = ["JobHandlers", "JobWorker"]
