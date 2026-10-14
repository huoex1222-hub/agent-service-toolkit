"""Langfuse callback handler that also stamps the provider's per-call trace id.

DeepSeek's ``x-ds-trace-id`` arrives with the response, so it can only be attached once
the LLM run ends -- and it has to happen here rather than in a sibling callback: the
parent handler detaches the observation during ``on_llm_end``, while async callbacks are
gathered concurrently, so ``update_current_generation`` finds no active span.
"""

import logging
from typing import Any
from uuid import UUID

from langchain_core.outputs import LLMResult
from langfuse.langchain import CallbackHandler  # type: ignore[import-untyped]

from core.provider_headers import provider_trace_headers

logger = logging.getLogger(__name__)


def run_id_as_trace_id(run_id: str) -> str | None:
    """The run id as a Langfuse trace id (32 hex chars), or None if it isn't a UUID.

    Langfuse trace ids are UUIDs without dashes, so a run's trace can be addressed from
    its run id alone: no lookup table, and a log line's run id matches the trace id.
    Anything else (clients pass arbitrary strings to AG-UI) returns None.
    """
    try:
        return UUID(str(run_id)).hex
    except (ValueError, AttributeError, TypeError):
        return None


class TracingCallbackHandler(CallbackHandler):  # type: ignore[misc]
    """Records a generation's provider trace id as observation metadata, so one Langfuse
    generation can be matched to one captured HTTP flow."""

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> Any:
        try:
            runs = getattr(self, "_runs", None)
            observation = runs.get(run_id) if isinstance(runs, dict) else None
            trace_headers = provider_trace_headers(response)
            if observation is not None and trace_headers:
                observation.update(metadata=trace_headers)
        except Exception:
            logger.warning("Could not attach provider trace id to the Langfuse run", exc_info=True)
        return super().on_llm_end(response, run_id=run_id, parent_run_id=parent_run_id, **kwargs)
