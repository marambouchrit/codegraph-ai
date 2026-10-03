"""Run analysis jobs in the background, inside the API process.

    POST /analyze  ->  runner.submit(job)  ->  the request returns at once
                                           ->  one worker thread runs the job

A deliberately small mechanism: a thread pool with ONE thread, so analyses run one
after the other (they share the CPU and the embedding model anyway) and other
projects simply wait in the queue. No broker, no extra service.

Its limits, by design: the queue lives in this process. If the server stops, queued
and running jobs are lost (the analysis state then reports them as interrupted), and
several API processes would each have their own queue. A larger deployment would
replace this class with a durable queue; nothing else needs to change, since the job
itself is a plain function and its state is persisted by AnalysisService.
"""

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

logger = logging.getLogger(__name__)

Job = Callable[[], None]


class JobRunner(Protocol):
    def submit(self, job: Job) -> None:
        """Schedule `job`; it must handle its own errors."""


class ThreadJobRunner:
    """One background thread; jobs are run in the order they were submitted."""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="analysis")

    def submit(self, job: Job) -> None:
        self._executor.submit(_safely, job)

    def shutdown(self) -> None:
        """Stop accepting jobs and drop the queued ones (a running one finishes its step)."""
        self._executor.shutdown(wait=False, cancel_futures=True)


def _safely(job: Job) -> None:
    try:
        job()
    except Exception:  # a job reports its own failure; this only protects the worker
        logger.exception("An analysis job raised an unexpected error")
