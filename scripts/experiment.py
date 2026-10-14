"""Run a Langfuse dataset against the agent service, as a Langfuse dataset run.

Each item is sent to POST /{agent}/invoke with a thread id scoped to the run (so runs
cannot bleed into each other's history), and the answer is linked back to the dataset
run. A response's run id *is* its Langfuse trace id, so the run item points at the
agent's own trace -- full span tree, cost and prompt version -- rather than at a trace
this script creates.

Usage (run from the repo root; PYTHONPATH is not needed, the script adds src/ itself):

    # create the dataset, or append items to it
    uv run python scripts/experiment.py --dataset research-basics \\
        --seed scripts/example_dataset.json

    # one run per configuration; compare them side by side in Langfuse
    uv run python scripts/experiment.py --dataset research-basics \\
        --model deepseek-v4-flash --run-name flash-1
    uv run python scripts/experiment.py --dataset research-basics \\
        --model deepseek-v4-pro --run-name pro-1

AGENT_URL (the service's address) is read from .env. The service must be running.
"""

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

# Importable from the repo root without PYTHONPATH=src; harmless when it is set.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from client import AgentClient  # noqa: E402
from core.settings import settings  # noqa: E402

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
    """The user's message, from either shape an item's input takes.

    Items added via the Langfuse UI (observation -> Add to dataset) keep the whole
    ``{"messages": [...]}`` array; items seeded from a file are plain strings.
    """
    value = item["input"]
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if "message" in value:
            return str(value["message"])
        messages = value.get("messages")
        if isinstance(messages, list):
            human = [m for m in messages if isinstance(m, dict) and m.get("type") == "human"]
            if human:
                return str(human[-1].get("content", ""))
    return json.dumps(value, ensure_ascii=False)


def _dataset_id(api: httpx.Client, dataset: str) -> str:
    response = api.get(f"/api/public/datasets/{dataset}")
    response.raise_for_status()
    return response.json()["id"]


def _scores_by_trace(api: httpx.Client) -> dict[str, list[tuple[str, float]]]:
    response = api.get("/api/public/scores", params={"limit": 100})
    response.raise_for_status()
    scores: dict[str, list[tuple[str, float]]] = {}
    for score in response.json()["data"]:
        scores.setdefault(score.get("traceId") or "", []).append(
            (score.get("name") or "?", score.get("value") or 0)
        )
    return scores


def _run_rows(api: httpx.Client, dataset_id: str, run_name: str) -> dict[str, dict[str, Any]]:
    """datasetItemId -> {trace id, latency, cost, answer, scores} for one run."""
    response = api.get(
        "/api/public/dataset-run-items",
        params={"datasetId": dataset_id, "runName": run_name, "limit": 100},
    )
    response.raise_for_status()
    scores = _scores_by_trace(api)
    rows: dict[str, dict[str, Any]] = {}
    for item in response.json()["data"]:
        trace_id = item["traceId"]
        trace = api.get(f"/api/public/traces/{trace_id}").json()
        messages = (trace.get("output") or {}).get("messages") or []
        rows[item["datasetItemId"]] = {
            "trace": trace_id,
            "latency": trace.get("latency") or 0.0,
            "cost": trace.get("totalCost") or 0.0,
            "answer": (messages[-1].get("content") if messages else "") or "",
            "scores": scores.get(trace_id, []),
        }
    return rows


def compare_runs(api: httpx.Client, dataset: str, run_a: str, run_b: str) -> None:
    """Print the two runs side by side, item by item (the Langfuse table is awkward for this)."""
    dataset_id = _dataset_id(api, dataset)
    rows_a = _run_rows(api, dataset_id, run_a)
    rows_b = _run_rows(api, dataset_id, run_b)
    questions = {i["id"]: _question(i) for i in fetch_items(api, dataset)}

    def cell(row: dict[str, Any] | None) -> str:
        if not row:
            return "（这次运行没有这条）"
        score = ", ".join(f"{n}={v:g}" for n, v in row["scores"]) or "无分"
        return (
            f"{row['latency']:5.1f}s ${row['cost']:.6f} {score:12} │ "
            f"{row['answer'].replace(chr(10), ' ')[:58]}"
        )

    print(f"\n=== {run_a}  vs  {run_b}   (dataset: {dataset})\n")
    for item_id, question in questions.items():
        a, b = rows_a.get(item_id), rows_b.get(item_id)
        if not a and not b:
            continue
        print(f"[{question[:56]}]")
        print(f"  {run_a:10} {cell(a)}")
        print(f"  {run_b:10} {cell(b)}")
        if a and b:
            sa = {n: v for n, v in a["scores"]}
            sb = {n: v for n, v in b["scores"]}
            for name in set(sa) & set(sb):
                if abs(sa[name] - sb[name]) > 1e-9:
                    print(f"  ⚠ {name}: {run_a}={sa[name]:g}  {run_b}={sb[name]:g}  ← 分数有差异")
            if a["answer"].strip() != b["answer"].strip():
                print("  ⚠ 两次回答内容不同")
        print()

    for name, rows in ((run_a, rows_a), (run_b, rows_b)):
        if not rows:
            continue
        n = len(rows)
        scores = [v for r in rows.values() for _, v in r["scores"]]
        mean_score = f"{sum(scores) / len(scores):.2f} (n={len(scores)})" if scores else "无"
        print(
            f"汇总 {name:10} {n} 条  均延迟 {sum(r['latency'] for r in rows.values()) / n:4.1f}s  "
            f"总成本 ${sum(r['cost'] for r in rows.values()):.6f}  均分 {mean_score}"
        )
    print(f"\n明细: {settings.LANGFUSE_HOST} -> Datasets -> {dataset}（点任一单元格进 trace）")


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
    parser.add_argument("--limit", type=int, default=None, help="run only the first N items")
    parser.add_argument(
        "--compare",
        nargs=2,
        metavar=("RUN_A", "RUN_B"),
        help="print two existing runs side by side and exit (no new run)",
    )
    args = parser.parse_args()

    api = _langfuse_api()
    if args.compare:
        compare_runs(api, args.dataset, *args.compare)
        return
    if args.seed:
        seed_dataset(api, args.dataset, args.seed)

    items = fetch_items(api, args.dataset)
    if args.limit:
        items = items[: args.limit]
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
