import logging
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolCall, ToolMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from service.utils import LogGraphNodes, LogLLMCalls, langchain_to_chat_message


def test_messages_from_langchain() -> None:
    lc_human_message = HumanMessage(content="Hello, world!")
    human_message = langchain_to_chat_message(lc_human_message)
    assert human_message.type == "human"
    assert human_message.content == "Hello, world!"

    lc_ai_message = AIMessage(content="Hello, world!")
    ai_message = langchain_to_chat_message(lc_ai_message)
    assert ai_message.type == "ai"
    assert ai_message.content == "Hello, world!"

    lc_tool_message = ToolMessage(content="Hello, world!", tool_call_id="123")
    tool_message = langchain_to_chat_message(lc_tool_message)
    assert tool_message.type == "tool"
    assert tool_message.content == "Hello, world!"
    assert tool_message.tool_call_id == "123"

    lc_system_message = SystemMessage(content="Hello, world!")
    try:
        _ = langchain_to_chat_message(lc_system_message)
    except ValueError as e:
        assert str(e) == "Unsupported message type: SystemMessage"


def test_message_run_id_usage() -> None:
    run_id = "847c6285-8fc9-4560-a83f-4e6285809254"
    lc_message = AIMessage(content="Hello, world!")
    ai_message = langchain_to_chat_message(lc_message)
    ai_message.run_id = run_id
    assert ai_message.run_id == run_id


def test_messages_tool_calls() -> None:
    tool_call = ToolCall(name="test_tool", args={"x": 1, "y": 2}, id="call_Jja7")
    lc_ai_message = AIMessage(content="", tool_calls=[tool_call])
    ai_message = langchain_to_chat_message(lc_ai_message)
    assert ai_message.tool_calls[0]["id"] == "call_Jja7"
    assert ai_message.tool_calls[0]["name"] == "test_tool"
    assert ai_message.tool_calls[0]["args"] == {"x": 1, "y": 2}


@pytest.mark.asyncio
async def test_log_llm_calls_reports_model_and_tokens(caplog: pytest.LogCaptureFixture) -> None:
    response = LLMResult(
        generations=[
            [
                ChatGeneration(
                    message=AIMessage(
                        content="hi",
                        response_metadata={"model_name": "deepseek-v4-flash"},
                        usage_metadata={
                            "input_tokens": 821,
                            "output_tokens": 76,
                            "total_tokens": 897,
                        },
                    )
                )
            ]
        ]
    )

    with caplog.at_level(logging.INFO, logger="service.utils"):
        await LogLLMCalls().on_llm_end(response)

    assert "LLM call: deepseek-v4-flash 821→76 tokens" in caplog.text


@pytest.mark.asyncio
async def test_log_llm_calls_without_usage(caplog: pytest.LogCaptureFixture) -> None:
    response = LLMResult(generations=[[ChatGeneration(message=AIMessage(content="hi"))]])

    with caplog.at_level(logging.INFO, logger="service.utils"):
        await LogLLMCalls().on_llm_end(response)

    assert "LLM call: unknown" in caplog.text


@pytest.mark.asyncio
async def test_log_graph_nodes_logs_each_node_once(caplog: pytest.LogCaptureFixture) -> None:
    handler = LogGraphNodes()
    node_run, nested_run = uuid4(), uuid4()
    metadata = {"langgraph_node": "model", "langgraph_step": 2}

    with caplog.at_level(logging.INFO, logger="service.utils"):
        await handler.on_chain_start(None, {}, run_id=node_run, name="model", metadata=metadata)
        # A runnable inside the node repeats the same metadata under another name.
        await handler.on_chain_start(
            None, {}, run_id=nested_run, name="RunnableSequence", metadata=metadata
        )
        await handler.on_chain_end({}, run_id=nested_run)
        await handler.on_chain_end({}, run_id=node_run)

    assert caplog.text.count("Node: model") == 1


@pytest.mark.asyncio
async def test_log_graph_nodes_ignores_runs_without_node_metadata(
    caplog: pytest.LogCaptureFixture,
) -> None:
    handler = LogGraphNodes()
    run_id = uuid4()

    with caplog.at_level(logging.INFO, logger="service.utils"):
        await handler.on_chain_start(None, {}, run_id=run_id, metadata={})
        await handler.on_chain_end({}, run_id=run_id)

    assert "Node:" not in caplog.text


@pytest.mark.asyncio
async def test_log_graph_nodes_reports_a_failed_node(caplog: pytest.LogCaptureFixture) -> None:
    handler = LogGraphNodes()
    run_id = uuid4()
    metadata = {"langgraph_node": "tools", "langgraph_step": 3}

    with caplog.at_level(logging.INFO, logger="service.utils"):
        await handler.on_chain_start(None, {}, run_id=run_id, name="tools", metadata=metadata)
        await handler.on_chain_error(RuntimeError("boom"), run_id=run_id)

    assert "Node: tools failed" in caplog.text
