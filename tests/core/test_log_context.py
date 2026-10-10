import contextvars
import logging
from collections.abc import Iterator

import pytest

from core.log_context import (
    RUN_ID,
    RUN_STARTED_AT,
    THREAD_ID,
    USER_ID,
    CorrelationFilter,
    bind_correlation_ids,
    duration_suffix,
    mark_run_started,
)


@pytest.fixture(autouse=True)
def _reset_ids() -> Iterator[None]:
    tokens = [(var, var.set(None)) for var in (RUN_ID, THREAD_ID, USER_ID, RUN_STARTED_AT)]
    yield
    for var, token in tokens:
        var.reset(token)


def _record() -> logging.LogRecord:
    return logging.LogRecord("test", logging.INFO, __file__, 1, "message", None, None)


def test_filter_puts_the_bound_ids_on_the_record():
    bind_correlation_ids(run_id="run-1", thread_id="thread-1", user_id="user-1")

    record = _record()
    assert CorrelationFilter().filter(record) is True
    assert (record.run_id, record.thread_id, record.user_id) == ("run-1", "thread-1", "user-1")


def test_filter_renders_unbound_ids_as_dash():
    record = _record()

    CorrelationFilter().filter(record)

    assert (record.run_id, record.thread_id, record.user_id) == ("-", "-", "-")


def test_empty_values_are_ignored():
    bind_correlation_ids(run_id="run-1", thread_id=None, user_id="")

    record = _record()
    CorrelationFilter().filter(record)

    assert (record.run_id, record.thread_id, record.user_id) == ("run-1", "-", "-")


def test_duration_suffix_is_empty_without_a_marked_start():
    assert duration_suffix() == ""


def test_duration_suffix_reports_the_elapsed_time():
    mark_run_started()

    assert duration_suffix().startswith(" in ")
    assert duration_suffix().endswith("s")


def test_bindings_do_not_leak_into_a_context_copied_before_them():
    # What a sibling asyncio task sees: it gets a context copied at creation time,
    # before this request bound its ids.
    sibling = contextvars.copy_context()

    bind_correlation_ids(run_id="run-1", thread_id="thread-1", user_id="user-1")

    record = _record()
    sibling.run(CorrelationFilter().filter, record)
    assert record.run_id == "-"
