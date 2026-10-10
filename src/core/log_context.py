"""Correlation ids in log records -- the stdlib stand-in for Java's MDC.

Thread-locals don't work here: one thread runs many coroutines, so the ids live in
contextvars instead. A request binds them once with :func:`bind_correlation_ids`, and
:class:`CorrelationFilter` copies them onto every record emitted afterwards, so log lines
can be grepped by run or thread.
"""

import logging
import time
from contextvars import ContextVar

RUN_ID: ContextVar[str | None] = ContextVar("run_id", default=None)
THREAD_ID: ContextVar[str | None] = ContextVar("thread_id", default=None)
USER_ID: ContextVar[str | None] = ContextVar("user_id", default=None)
RUN_STARTED_AT: ContextVar[float | None] = ContextVar("run_started_at", default=None)

LOG_FORMAT = (
    "%(asctime)s %(levelname)-8s [%(run_id)s/%(thread_id)s] %(name)s:%(lineno)d: %(message)s"
)


class CorrelationFilter(logging.Filter):
    """Puts the bound ids on every record; unbound ones render as ``-``."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = RUN_ID.get() or "-"
        record.thread_id = THREAD_ID.get() or "-"
        record.user_id = USER_ID.get() or "-"
        return True


def bind_correlation_ids(
    *, run_id: str | None = None, thread_id: str | None = None, user_id: str | None = None
) -> None:
    """Bind the current request's ids for the rest of this context's lifetime.

    Contexts are copied per asyncio task, so a request bound here does not leak into
    requests handled by other tasks.
    """
    if run_id:
        RUN_ID.set(str(run_id))
    if thread_id:
        THREAD_ID.set(str(thread_id))
    if user_id:
        USER_ID.set(str(user_id))


def mark_run_started() -> None:
    """Record the run's start so its finish line can report the duration."""
    RUN_STARTED_AT.set(time.perf_counter())


def run_duration() -> float | None:
    started = RUN_STARTED_AT.get()
    return None if started is None else time.perf_counter() - started


def duration_suffix() -> str:
    """``" in 4.3s"`` for the finish line, or ``""`` if no start was marked."""
    duration = run_duration()
    return f" in {duration:.1f}s" if duration is not None else ""


def configure_logging(level: int) -> None:
    """Root logging config with correlation ids in the line format."""
    handler = logging.StreamHandler()
    handler.addFilter(CorrelationFilter())
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logging.basicConfig(level=level, handlers=[handler])
