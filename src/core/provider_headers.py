"""Provider per-call trace ids: capture them from response headers, keep them small.

``include_response_headers=True`` makes langchain-openai put every response header
(~700 B) into the message's ``response_metadata``, where it would ride into the graph
state and every checkpoint. Only the provider's per-call id is worth keeping -- it ties
one Langfuse generation to one captured HTTP flow.
"""

from typing import Any

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.outputs import LLMResult

PROVIDER_TRACE_HEADERS = frozenset({"x-ds-trace-id"})


def trim_response_headers(response_metadata: dict[str, Any]) -> None:
    headers = response_metadata.get("headers")
    if not isinstance(headers, dict):
        return
    kept = {
        name: value for name, value in headers.items() if name.lower() in PROVIDER_TRACE_HEADERS
    }
    if kept:
        response_metadata["headers"] = kept
    else:
        response_metadata.pop("headers", None)


def provider_trace_headers(response: LLMResult) -> dict[str, str]:
    for batch in response.generations:
        for generation in batch:
            metadata = getattr(getattr(generation, "message", None), "response_metadata", None)
            headers = metadata.get("headers") if isinstance(metadata, dict) else None
            if not isinstance(headers, dict):
                continue
            found = {
                name: str(value)
                for name, value in headers.items()
                if name.lower() in PROVIDER_TRACE_HEADERS and value
            }
            if found:
                return found
    return {}


class TrimProviderHeaders(AsyncCallbackHandler):
    """Trim captured headers as they arrive.

    Streaming aggregates the caller's message from chunks, so the headers have to be
    trimmed on the chunk; the aggregated message is a new object built afterwards.

    Attached at model construction, so it must stay stateless: ``get_model`` is cached
    and one instance is shared by every request.
    """

    async def on_llm_new_token(self, token: Any, *, chunk: Any = None, **kwargs: Any) -> None:
        for metadata in (
            getattr(chunk, "generation_info", None),
            getattr(getattr(chunk, "message", None), "response_metadata", None),
        ):
            if isinstance(metadata, dict):
                trim_response_headers(metadata)

    async def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        for batch in response.generations:
            for generation in batch:
                metadata = getattr(getattr(generation, "message", None), "response_metadata", None)
                if isinstance(metadata, dict):
                    trim_response_headers(metadata)
