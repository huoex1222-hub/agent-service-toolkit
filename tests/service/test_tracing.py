from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from langfuse.langchain import CallbackHandler  # type: ignore[import-untyped]

from service.tracing import TracingCallbackHandler, run_id_as_trace_id


class FakeObservation:
    def __init__(self) -> None:
        self.metadata: dict[str, Any] = {}

    def update(self, *, metadata: dict[str, Any]) -> None:
        self.metadata.update(metadata)


def _result(headers: dict[str, str] | None) -> LLMResult:
    response_metadata = {"headers": headers} if headers else {}
    return LLMResult(
        generations=[
            [ChatGeneration(message=AIMessage(content="hi", response_metadata=response_metadata))]
        ]
    )


def _handler(observation: FakeObservation | None, run_id: UUID) -> TracingCallbackHandler:
    # Bypass the parent's __init__ so no Langfuse client is constructed.
    handler = object.__new__(TracingCallbackHandler)
    handler._runs = cast(Any, {run_id: observation} if observation is not None else {})
    return handler


@pytest.fixture
def parent_on_llm_end(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(CallbackHandler, "on_llm_end", lambda self, response, **kwargs: None)


def test_stamps_the_provider_trace_id(parent_on_llm_end: None):
    run_id = uuid4()
    observation = FakeObservation()
    handler = _handler(observation, run_id)

    handler.on_llm_end(_result({"x-ds-trace-id": "abc123", "server": "openresty"}), run_id=run_id)

    assert observation.metadata == {"x-ds-trace-id": "abc123"}


def test_attaches_nothing_without_a_trace_header(parent_on_llm_end: None):
    run_id = uuid4()
    observation = FakeObservation()
    handler = _handler(observation, run_id)

    handler.on_llm_end(_result({"server": "openresty"}), run_id=run_id)

    assert observation.metadata == {}


def test_missing_observation_is_not_an_error(parent_on_llm_end: None):
    run_id = uuid4()
    handler = _handler(None, run_id)

    handler.on_llm_end(_result({"x-ds-trace-id": "abc123"}), run_id=run_id)


def test_a_failing_update_does_not_break_the_run(parent_on_llm_end: None):
    run_id = uuid4()

    class ExplodingObservation(FakeObservation):
        def update(self, *, metadata: dict[str, Any]) -> None:
            raise RuntimeError("boom")

    handler = _handler(ExplodingObservation(), run_id)

    handler.on_llm_end(_result({"x-ds-trace-id": "abc123"}), run_id=run_id)


def test_run_id_as_trace_id_strips_the_dashes():
    assert (
        run_id_as_trace_id("01a1250a-1713-7b90-a132-61bbeebeabee")
        == "01a1250a17137b90a13261bbeebeabee"
    )


def test_run_id_as_trace_id_rejects_non_uuids():
    assert run_id_as_trace_id("run-1") is None
    assert run_id_as_trace_id("") is None
