"""Retry and error-routing policy shared by ToolNode-based agents.

Invalid tool arguments are returned to the model as tool output so it can correct
them; transient infrastructure errors are retried per tool call with backoff;
deterministic tool failures are also returned to the model instead of ending the run.
"""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from ddgs.exceptions import RatelimitException as DDGSRateLimitError
from ddgs.exceptions import TimeoutException as DDGSTimeoutError
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest, ToolInvocationError
from langgraph.types import Command

logger = logging.getLogger(__name__)

TOOL_MAX_ATTEMPTS = 3
TOOL_INITIAL_DELAY = 0.5
TOOL_MAX_DELAY = 8.0
TOOL_CALL_TIMEOUT = 30.0

RETRYABLE_TOOL_ERRORS = (
    ConnectionError,
    TimeoutError,
    httpx.TransportError,
    DDGSRateLimitError,
    DDGSTimeoutError,
)


def is_retryable_tool_error(error: Exception) -> bool:
    if isinstance(error, RETRYABLE_TOOL_ERRORS):
        return True
    status = getattr(error, "status_code", None)
    return status == 429 or (isinstance(status, int) and status >= 500)


def handle_tool_errors(error: Exception) -> str:
    if is_retryable_tool_error(error):
        raise error
    if isinstance(error, ToolInvocationError):
        return error.message
    logger.warning(f"Tool error returned to the model: {error}")
    return f"Tool failed: {error}"


async def retry_tool_call(
    request: ToolCallRequest,
    execute: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
) -> ToolMessage | Command[Any]:
    delay = TOOL_INITIAL_DELAY
    for attempt in range(1, TOOL_MAX_ATTEMPTS + 1):
        try:
            async with asyncio.timeout(TOOL_CALL_TIMEOUT):
                return await execute(request)
        except Exception as error:
            if not is_retryable_tool_error(error):
                raise
            if attempt == TOOL_MAX_ATTEMPTS:
                logger.warning(
                    f"Tool {request.tool_call['name']} failed after {attempt} attempts: {error}"
                )
                raise
            logger.warning(
                f"Tool {request.tool_call['name']} failed "
                f"(attempt {attempt}/{TOOL_MAX_ATTEMPTS}): {error}"
            )
            await asyncio.sleep(delay + random.uniform(0, delay * 0.2))
            delay = min(delay * 2, TOOL_MAX_DELAY)
    raise AssertionError("unreachable")
