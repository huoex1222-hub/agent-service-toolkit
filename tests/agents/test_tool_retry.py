from unittest.mock import MagicMock

import pytest
from ddgs.exceptions import RatelimitException
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from agents import tool_retry
from agents.tool_retry import handle_tool_errors, retry_tool_call
from agents.tools import calculator_func, web_search_func


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(tool_retry, "TOOL_INITIAL_DELAY", 0.0)


def make_request(name: str = "Calculator") -> MagicMock:
    request = MagicMock()
    request.tool_call = {"name": name, "args": {}, "id": "call-1"}
    return request


@pytest.mark.asyncio
async def test_retry_tool_call_retries_transient_failure() -> None:
    calls = 0

    async def execute(request):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ConnectionError("temporary")
        return ToolMessage(content="ok", tool_call_id="call-1")

    result = await retry_tool_call(make_request(), execute)

    assert calls == 3
    assert result.content == "ok"


@pytest.mark.asyncio
async def test_retry_tool_call_does_not_retry_value_error() -> None:
    calls = 0

    async def execute(request):
        nonlocal calls
        calls += 1
        raise ValueError("bad arguments")

    with pytest.raises(ValueError):
        await retry_tool_call(make_request(), execute)

    assert calls == 1


@pytest.mark.asyncio
async def test_retry_tool_call_gives_up_after_max_attempts() -> None:
    calls = 0

    async def execute(request):
        nonlocal calls
        calls += 1
        raise ConnectionError("still down")

    with pytest.raises(ConnectionError):
        await retry_tool_call(make_request(), execute)

    assert calls == tool_retry.TOOL_MAX_ATTEMPTS


def test_handle_tool_errors_reraises_transient() -> None:
    with pytest.raises(ConnectionError):
        handle_tool_errors(ConnectionError("down"))


def test_handle_tool_errors_returns_deterministic_message() -> None:
    message = handle_tool_errors(ValueError("bad"))

    assert "Tool failed" in message
    assert "bad" in message


def test_calculator_raises_on_invalid_expression() -> None:
    with pytest.raises(ValueError, match="raised error"):
        calculator_func("1 +")


def test_web_search_reraises_rate_limit(monkeypatch) -> None:
    class FailingDDGS:
        def __init__(self, **kwargs):
            pass

        def text(self, *args, **kwargs):
            raise RatelimitException("rate limited")

    monkeypatch.setattr("agents.tools.DDGS", FailingDDGS)

    with pytest.raises(RatelimitException):
        web_search_func("query")


class ScriptedToolModel(BaseChatModel):
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted-tool-model"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise NotImplementedError

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "Calculator",
                        "args": {"expression": "1 +"},
                        "id": "call-1",
                    }
                ],
            )
        else:
            message = AIMessage(content="done")
        return ChatResult(generations=[ChatGeneration(message=message)])


@pytest.mark.asyncio
async def test_research_assistant_returns_tool_error_to_model(monkeypatch) -> None:
    import agents.research_assistant as research_assistant

    model = ScriptedToolModel()
    monkeypatch.setattr(research_assistant, "get_model", lambda *args, **kwargs: model)

    result = await research_assistant.research_assistant.ainvoke(
        {"messages": [HumanMessage(content="calculate 1 +")]}
    )

    tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert model.calls == 2
    assert len(tool_messages) == 1
    assert tool_messages[0].status == "error"
    assert "raised error" in tool_messages[0].content
