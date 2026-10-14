from unittest.mock import Mock, patch

from core.prompts import get_prompt


def test_returns_the_local_default_when_tracing_is_off():
    with patch("core.prompts.settings.LANGFUSE_TRACING", False):
        prompt = get_prompt("x", "local text")

    assert prompt.text == "local text"
    assert prompt.langfuse_prompt is None


def test_falls_back_to_the_local_default_when_langfuse_fails():
    client = Mock()
    client.get_prompt.side_effect = RuntimeError("unreachable")
    with (
        patch("core.prompts.settings.LANGFUSE_TRACING", True),
        patch("langfuse.get_client", return_value=client),
    ):
        prompt = get_prompt("x", "local text")

    assert prompt.text == "local text"
    assert prompt.langfuse_prompt is None


def test_uses_the_langfuse_prompt_and_returns_it_for_linking():
    client = Mock()
    remote = Mock(is_fallback=False)
    remote.compile.return_value = "remote text"
    client.get_prompt.return_value = remote
    with (
        patch("core.prompts.settings.LANGFUSE_TRACING", True),
        patch("langfuse.get_client", return_value=client),
    ):
        prompt = get_prompt("x", "local text", current_date="today")

    assert prompt.text == "remote text"
    assert prompt.langfuse_prompt is remote
    remote.compile.assert_called_once_with(current_date="today")


def test_ignores_the_sdk_fallback_prompt():
    # The SDK returns a prompt object wrapping our fallback when the name is unknown;
    # that must not be reported as a Langfuse prompt, or traces would link a version
    # that does not exist.
    client = Mock()
    client.get_prompt.return_value = Mock(is_fallback=True)
    with (
        patch("core.prompts.settings.LANGFUSE_TRACING", True),
        patch("langfuse.get_client", return_value=client),
    ):
        prompt = get_prompt("x", "local text")

    assert prompt.text == "local text"
    assert prompt.langfuse_prompt is None
