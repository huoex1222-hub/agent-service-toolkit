import logging
import time
from collections.abc import Mapping
from typing import Any, cast
from uuid import UUID

from fastapi import HTTPException
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langchain_core.messages import (
    ChatMessage as LangchainChatMessage,
)
from langchain_core.outputs import LLMResult

from core import settings
from schema import ChatMessage

logger = logging.getLogger(__name__)


class LogLLMCalls(AsyncCallbackHandler):
    """One log line per LLM call, so a run's model activity is visible in the terminal.

    The Langfuse SDK raises the httpx logger to WARNING on import, so httpx's own
    per-request lines never reach this app's logs.
    """

    async def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        for batch in response.generations:
            for generation in batch:
                message = getattr(generation, "message", None)
                metadata = getattr(message, "response_metadata", None) or {}
                model = metadata.get("model_name") or metadata.get("model") or "unknown"
                usage = getattr(message, "usage_metadata", None) or {}
                tokens = ""
                if "input_tokens" in usage and "output_tokens" in usage:
                    tokens = f" {usage['input_tokens']}→{usage['output_tokens']} tokens"
                logger.info(f"LLM call: {model}{tokens}")


class LogGraphNodes(AsyncCallbackHandler):
    """One line per LangGraph node, so the terminal shows the node timeline Langfuse does.

    Both read the same callback stream. Everything inside a node -- nested runnables and
    the conditional-edge functions -- repeats the node's ``langgraph_node`` metadata; only
    the node's own run is named after it, which is what this filters on.
    """

    def __init__(self) -> None:
        self._runs: dict[UUID, tuple[str, float]] = {}

    async def on_chain_start(
        self,
        serialized: dict[str, Any] | None,
        inputs: dict[str, Any],
        *,
        run_id: UUID,
        name: str | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        node = (metadata or {}).get("langgraph_node")
        if not node or name != node:
            return
        self._runs[run_id] = (node, time.perf_counter())

    async def on_chain_end(self, outputs: Any, *, run_id: UUID, **kwargs: Any) -> None:
        node, started = self._runs.pop(run_id, ("", 0.0))
        if node:
            logger.info(f"Node: {node} ({time.perf_counter() - started:.2f}s)")

    async def on_chain_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        node, started = self._runs.pop(run_id, ("", 0.0))
        if node:
            logger.info(f"Node: {node} failed after {time.perf_counter() - started:.2f}s")


def ensure_model_available(model: Any) -> None:
    """Raise 400 if `model` isn't in the operator's AVAILABLE_MODELS allowlist."""
    if model not in settings.AVAILABLE_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"Model '{model}' is not available. "
            f"Allowed: {[m.value for m in settings.AVAILABLE_MODELS]}",
        )


def convert_message_content_to_string(content: str | list[str | dict]) -> str:
    if isinstance(content, str):
        return content
    text: list[str] = []
    for content_item in content:
        if isinstance(content_item, str):
            text.append(content_item)
            continue
        if content_item["type"] == "text":
            text.append(content_item["text"])
    return "".join(text)


def langchain_to_chat_message(message: BaseMessage) -> ChatMessage:
    """Create a ChatMessage from a LangChain message."""
    match message:
        case HumanMessage():
            human_message = ChatMessage(
                type="human",
                content=convert_message_content_to_string(message.content),
            )
            return human_message
        case AIMessage():
            ai_message = ChatMessage(
                type="ai",
                content=convert_message_content_to_string(message.content),
            )
            if message.tool_calls:
                ai_message.tool_calls = message.tool_calls
            if message.response_metadata:
                ai_message.response_metadata = message.response_metadata
            return ai_message
        case ToolMessage():
            tool_message = ChatMessage(
                type="tool",
                content=convert_message_content_to_string(message.content),
                tool_call_id=message.tool_call_id,
            )
            return tool_message
        case LangchainChatMessage():
            if message.role == "custom":
                custom_message = ChatMessage(
                    type="custom",
                    content="",
                    custom_data=cast(dict[str, Any], message.content[0]),
                )
                return custom_message
            else:
                raise ValueError(f"Unsupported chat message role: {message.role}")
        case _:
            raise ValueError(f"Unsupported message type: {message.__class__.__name__}")


def messages_from_checkpoint(checkpoint: Mapping[str, Any]) -> list[BaseMessage]:
    """Extract a thread's conversation from a raw checkpoint.

    Graph-state agents keep the conversation in the `messages` channel. Functional-API
    (`@entrypoint`) agents keep it in `__previous__` instead, which `aget_state` does not
    surface - it only returns the entrypoint's final value.
    """
    channel_values = checkpoint.get("channel_values") or {}
    messages = channel_values.get("messages")
    if not messages:
        previous = channel_values.get("__previous__")
        if isinstance(previous, Mapping):
            messages = previous.get("messages")
        elif isinstance(previous, list):
            messages = previous
    return [m for m in (messages or []) if isinstance(m, BaseMessage)]


def remove_tool_calls(content: str | list[str | dict]) -> str | list[str | dict]:
    """Remove tool calls from content."""
    if isinstance(content, str):
        return content
    # Currently only Anthropic models stream tool calls, using content item type tool_use.
    return [
        content_item
        for content_item in content
        if isinstance(content_item, str) or content_item["type"] != "tool_use"
    ]
