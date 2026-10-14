# Evaluating agents on datasets

`scripts/experiment.py` runs a [Langfuse dataset](https://langfuse.com/docs/datasets/overview) against the agent service: every item is sent to `POST /{agent}/invoke`, and the answer is recorded as a dataset run. Langfuse then holds the results — outputs, latency, cost and (optionally) automatic judge scores — so two configurations can be compared on the same questions instead of by impression.

This requires Langfuse tracing to be configured (`LANGFUSE_TRACING`, `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` in `.env`; see [`.env.example`](../.env.example)).

## How a run is linked to the agent's trace

The service uses a response's `run_id` as its Langfuse trace id (the callback handler is given a `trace_context`, see `src/service/service.py`). A dataset run item can therefore reference the agent's own trace — full span tree, tool calls, cost and prompt version — instead of a trace the script would have to synthesize.

## Collecting items

- **From production traces** (the habit worth forming): open an observation in Langfuse, click *Add to datasets*, and map its fields onto the item. Whatever you were unhappy with becomes a permanent regression case.
- **From a file**: pass `--seed` a JSON file of `{"input": ..., "expectedOutput": ...}` objects. The dataset is created if needed, and the items are appended.

## Running an experiment

```sh
# create the dataset, or append items to it
uv run python scripts/experiment.py --dataset research-basics --seed scripts/example_dataset.json

# one run per configuration; compare them side by side
uv run python scripts/experiment.py --dataset research-basics --model deepseek-v4-flash --run-name flash-2
uv run python scripts/experiment.py --dataset research-basics --model deepseek-v4-pro --run-name pro-2
```

The service must be running; its address is read from `AGENT_URL` in `.env`. `--agent` (default `research-assistant`), `--model`, `--limit`, `--user-id`, `--base-url` and `--timeout` are optional. `--run-name` groups the results: use a fresh name per configuration, and re-use a name only to append more items to the same run.

## Comparing two runs

```sh
uv run python scripts/experiment.py --dataset research-basics --compare flash-2 pro-2
```

This prints the runs side by side per question — latency, cost, judge score and a preview of the answer, with differing scores flagged — followed by a summary per run. The same results are in Langfuse under *Datasets → …*, where each cell links to the trace.

## Automatic scores (optional)

Langfuse [evaluators](https://langfuse.com/docs/evaluation/evaluation-methods/llm-as-a-judge) can score new traces with an LLM judge; those scores then appear in the comparison above. Two things are worth knowing:

- **An evaluator validates by really calling the model** when you save or activate it, so a slow model fails the setup with a validation error. Pick a *fast* model for judging: the judgement is a small task, and latency, not capability, is what usually breaks it.
- **A single item's score is noisy.** The same answer can score 1 in one run and 0 in the next. Treat per-item scores as a triage signal and compare means across runs.

Judge calls cost money like any other model call — sample the evaluator if your traffic is large.
