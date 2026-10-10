from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from core.provider_headers import (
    TrimProviderHeaders,
    provider_trace_headers,
    trim_response_headers,
)

CAPTURED_HEADERS = {"x-ds-trace-id": "abc123", "server": "openresty", "date": "now"}
TRIMMED_HEADERS = {"x-ds-trace-id": "abc123"}


def _generation(response_metadata: dict[str, Any]) -> ChatGeneration:
    return ChatGeneration(message=AIMessage(content="hi", response_metadata=response_metadata))


def test_trim_keeps_only_the_trace_header():
    metadata: dict[str, Any] = {"headers": dict(CAPTURED_HEADERS), "finish_reason": "stop"}

    trim_response_headers(metadata)

    assert metadata == {"headers": dict(TRIMMED_HEADERS), "finish_reason": "stop"}


def test_trim_drops_the_headers_key_when_nothing_is_kept():
    metadata: dict[str, Any] = {"headers": {"server": "openresty"}, "finish_reason": "stop"}

    trim_response_headers(metadata)

    assert metadata == {"finish_reason": "stop"}


def test_trim_leaves_metadata_without_headers_alone():
    metadata: dict[str, Any] = {"finish_reason": "stop"}

    trim_response_headers(metadata)

    assert metadata == {"finish_reason": "stop"}


def test_provider_trace_headers_reads_the_generation_metadata():
    result = LLMResult(generations=[[_generation({"headers": dict(CAPTURED_HEADERS)})]])

    assert provider_trace_headers(result) == {"x-ds-trace-id": "abc123"}


def test_provider_trace_headers_is_empty_without_headers():
    result = LLMResult(generations=[[_generation({})]])

    assert provider_trace_headers(result) == {}


@pytest.mark.asyncio
async def test_trim_handler_trims_the_streaming_chunk():
    message = AIMessage(content="", response_metadata={"headers": dict(CAPTURED_HEADERS)})
    chunk = SimpleNamespace(generation_info={"headers": dict(CAPTURED_HEADERS)}, message=message)

    await TrimProviderHeaders().on_llm_new_token("hi", chunk=chunk)

    assert chunk.generation_info == {"headers": dict(TRIMMED_HEADERS)}
    assert message.response_metadata == {"headers": dict(TRIMMED_HEADERS)}


@pytest.mark.asyncio
async def test_trim_handler_trims_the_final_response():
    result = LLMResult(generations=[[_generation({"headers": dict(CAPTURED_HEADERS)})]])

    await TrimProviderHeaders().on_llm_end(result)

    generation = result.generations[0][0]
    assert isinstance(generation, ChatGeneration)
    assert generation.message.response_metadata == {"headers": dict(TRIMMED_HEADERS)}
