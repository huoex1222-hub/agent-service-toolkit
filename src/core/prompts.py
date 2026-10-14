"""Prompts that can be managed in Langfuse, with the local text as the fallback.

The constant in the agent file stays the source of truth for a checkout without
Langfuse (and for whenever it is unreachable or the prompt does not exist there yet).
When the same name exists in Langfuse, that version wins, so a prompt edit ships
without a redeploy, and the version used is linked to the trace.

Langfuse caches fetched prompts client-side (60s by default), so calling this on every
request costs a dictionary lookup; only the first call after the cache expires fetches.
"""

import logging
from typing import Any

from core.settings import settings

logger = logging.getLogger(__name__)

PROMPT_LABEL = "production"


class ManagedPrompt:
    """The prompt text, plus the Langfuse object when it came from there.

    Pass ``langfuse_prompt`` on as run metadata under that same key: the Langfuse
    handler links the generation to the prompt version that produced it.
    """

    __slots__ = ("langfuse_prompt", "text")

    def __init__(self, text: str, langfuse_prompt: Any | None = None) -> None:
        self.text = text
        self.langfuse_prompt = langfuse_prompt


def get_prompt(name: str, default: str, **variables: Any) -> ManagedPrompt:
    """The Langfuse prompt ``name`` (``production`` label) or ``default``.

    ``variables`` fill the prompt's ``{{placeholders}}``; the local default is expected
    to be already formatted by its caller.
    """
    if not settings.LANGFUSE_TRACING:
        return ManagedPrompt(default)

    try:
        from langfuse import get_client  # type: ignore[import-untyped]

        prompt = get_client().get_prompt(
            name,
            label=PROMPT_LABEL,
            type="text",
            fallback=default,
            fetch_timeout_seconds=3,
        )
        if getattr(prompt, "is_fallback", False):
            return ManagedPrompt(default)
        return ManagedPrompt(prompt.compile(**variables), prompt)
    except Exception as e:
        logger.warning(f"Langfuse prompt {name!r} unavailable, using the local default: {e}")
        return ManagedPrompt(default)
