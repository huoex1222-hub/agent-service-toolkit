"""Run a Langfuse dataset against the agent service, as a Langfuse dataset run.

Each item is sent to POST /{agent}/invoke with a thread id scoped to the run (so runs
cannot bleed into each other's history), and the answer is linked back to the dataset
run. A response's run id *is* its Langfuse trace id, so the run item points at the
agent's own trace -- full span tree, cost and prompt version -- rather than at a trace
this script creates.

Usage (run from the repo root; PYTHONPATH=src is required, same as src/run_service.py):

    # create the dataset, or append items to it
    PYTHONPATH=src uv run python scripts/experiment.py --dataset research-basics \\
        --seed scripts/example_dataset.json

    # one run per configuration; compare them side by side in Langfuse
    PYTHONPATH=src uv run python scripts/experiment.py --dataset research-basics \\
        --model deepseek-v4-flash --run-name flash-1
    PYTHONPATH=src uv run python scripts/experiment.py --dataset research-basics \\
        --model deepseek-v4-pro --run-name pro-1

AGENT_URL (the service's address) is read from .env. The service must be running.
"""

import argparse
import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from client import AgentClient
from core.settings import settings

load_dotenv(override=True)


def _langfuse_api() -> httpx.Client:
    if not settings.LANGFUSE_PUBLIC_KEY or not settings.LANGFUSE_SECRET_KEY:
        raise SystemExit("LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY are not configured in .env.")
    return httpx.Client(
        base_url=settings.LANGFUSE_HOST,
        auth=(
            settings.LANGFUSE_PUBLIC_KEY.get_secret_value(),
            settings.LANGFUSE_SECRET_KEY.get_secret_value(),
        ),
        timeout=30,
    )


def seed_dataset(api: httpx.Client, dataset: str, path: Path) -> None:
    items: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
    created = api.post("/api/public/datasets", json={"name": dataset})
    if created.status_code not in (200, 201, 409):
        created.raise_for_status()
    for item in items:
        response = api.post(
            "/api/public/dataset-items",
            json={
                "datasetName": dataset,
                "input": item["input"],
                "expectedOutput": item.get("expectedOutput"),
            },
        )
        response.raise_for_status()
    print(f"Seeded {len(items)} item(s) into dataset {dataset!r}.")


def fetch_items(api: httpx.Client, dataset: str) -> list[dict[str, Any]]:
    response = api.get("/api/public/dataset-items", params={"datasetName": dataset, "limit": 100})
    response.raise_for_status()
    return response.json()["data"]


def _question(item: dict[str, Any]) -> str:
    value = item["input"]
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and "message" in value:
        return str(value["message"])
    return json.dumps(value, ensure_ascii=False)


async def run_experiment(api: httpx.Client, items: list[dict[str, Any]], args: Any) -> None:
    client = AgentClient(base_url=args.base_url, agent=args.agent, timeout=args.timeout)
    description = f"agent={args.agent} model={args.model or 'service default'}"
    latencies: list[float] = []

    for item in items:
        question = _question(item)
        thread_id = f"{args.run_name}-{item['id']}"
        started = time.perf_counter()
        try:
            message = await client.ainvoke(
                message=question,
                model=args.model,
                thread_id=thread_id,
                user_id=args.user_id,
            )
        except Exception as e:  # one bad item must not sink the run
            print(f"  {item['id']}  FAILED after {time.perf_counter() - started:.1f}s: {e}")
            continue

        elapsed = time.perf_counter() - started
        latencies.append(elapsed)
        answer = (message.content or "").replace("\n", " ")
        print(f"  {elapsed:6.1f}s  {answer[:70]!r}")

        response = api.post(
            "/api/public/dataset-run-items",
            json={
                "runName": args.run_name,
                "datasetItemId": item["id"],
                "traceId": (message.run_id or "").replace("-", ""),
                "runDescription": description,
            },
        )
        response.raise_for_status()

    if latencies:
        print(
            f"\n{len(latencies)}/{len(items)} ok, mean latency {sum(latencies) / len(latencies):.1f}s"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dataset", required=True, help="Langfuse dataset name")
    parser.add_argument("--seed", type=Path, help="JSON file of items to append first")
    parser.add_argument("--agent", default="research-assistant", help="agent key from /info")
    parser.add_argument(
        "--model", default=None, help="model key from /info; the service default when omitted"
    )
    parser.add_argument("--run-name", default=None, help="dataset run name")
    parser.add_argument("--user-id", default="experiment", help="user id recorded on the traces")
    parser.add_argument("--base-url", default=None, help="agent service URL (default: AGENT_URL)")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    api = _langfuse_api()
    if args.seed:
        seed_dataset(api, args.dataset, args.seed)

    items = fetch_items(api, args.dataset)
    if not items:
        raise SystemExit(f"Dataset {args.dataset!r} has no items -- seed it with --seed.")

    if not args.base_url:
        args.base_url = os.getenv("AGENT_URL")
    if not args.base_url:
        raise SystemExit("Set AGENT_URL in .env, or pass --base-url.")
    args.run_name = (
        args.run_name or f"{args.agent}-{args.model or 'default'}-{uuid.uuid4().hex[:6]}"
    )

    print(f"Running {len(items)} item(s) as {args.run_name!r} against {args.base_url} ...")
    asyncio.run(run_experiment(api, items, args))
    print(f"Compare runs in Langfuse: {settings.LANGFUSE_HOST} -> Datasets -> {args.dataset}")


if __name__ == "__main__":
    main()
