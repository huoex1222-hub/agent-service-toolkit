# LangGraph 节点重试与超时

`langgraph ~=1.2.7` 原生支持节点级重试、超时和错误处理。本文记录这些能力的用法、参数和坑，以及在本项目中的挂载位置。整理于 2026-10-18。

相关文档：

- [异常处理文档大纲](README.md)
- [LangGraph 异常处理总览](<LangGraph 异常处理总览.md>)
- [LangGraph ToolNode 异常处理](<LangGraph ToolNode 异常处理.md>)
- [Agent 服务异常处理实践](<Agent 服务异常处理实践.md>)

## 何时使用

| 失败类型 | 是否重试 | 原因 |
| --- | --- | --- |
| 连接失败、超时、429、5xx | 是 | 下一次可能成功 |
| `ValueError` / `TypeError` / 参数错误 | 否 | 同样的输入会得到同样的失败 |
| `RuntimeError` / 配置缺失 | 否 | 需要修配置或代码 |
| 流式中途失败 | 谨慎 | 已发给客户端的 token 无法撤回，重试会产生重复内容 |

节点级重试会**重跑整个节点**，所以只适合幂等、无副作用的节点；带副作用的工具或写操作应做到单次调用级（见 [ToolNode 文档](<LangGraph ToolNode 异常处理.md>)）。

## RetryPolicy

```python
from langgraph.types import RetryPolicy

graph.add_node(
    "model",
    acall_model,
    retry_policy=RetryPolicy(
        max_attempts=3,
        initial_interval=0.5,
        backoff_factor=2.0,
        max_interval=8.0,
        jitter=True,
        retry_on=...,
    ),
)
```

字段与默认值：

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `max_attempts` | 3 | **包含首次尝试**，即最多重试 2 次 |
| `initial_interval` | 0.5s | 第一次重试前的等待 |
| `backoff_factor` | 2.0 | 每次等待乘以该系数 |
| `max_interval` | 128s | 单次等待上限 |
| `jitter` | `True` | 加入随机抖动，避免并发重试同时打满 |
| `retry_on` | `default_retry_on` | 异常类型、类型元组，或 `Callable[[Exception], bool]` |

### retry_on 的坑

`default_retry_on` 的行为：

- `ConnectionError` → 重试
- `httpx.HTTPStatusError` / `requests.HTTPError` 的 5xx → 重试
- `RuntimeError`、`ValueError`、`TypeError`、`OSError`、`LookupError` 等 → **不重试**
- 其它异常 → 重试

需要注意：

1. **内置 `TimeoutError` 是 `OSError` 的子类**，默认不会被重试；要重试超时，显式把 `TimeoutError` 写进 `retry_on`。
2. **provider SDK 的 4xx 异常不在排除列表里**（如 `openai.BadRequestError`、`openai.AuthenticationError`），默认策略会误重试；建议显式传 `retry_on`。
3. 定义 `retry_on` 时优先匹配瞬时错误，例如：

```python
import httpx

def retry_on_transient(error: Exception) -> bool:
    if isinstance(error, (ConnectionError, TimeoutError, httpx.TransportError)):
        return True
    status = getattr(error, "status_code", None)
    return status == 429 or (isinstance(status, int) and status >= 500)
```

## 节点超时

```python
from langgraph.types import TimeoutPolicy

graph.add_node(
    "model",
    acall_model,
    timeout=TimeoutPolicy(run_timeout=120, idle_timeout=60),
)
```

- `run_timeout`：单次节点尝试的墙钟硬上限，不会被刷新
- `idle_timeout`：多久没有可观测进展就判定超时；`refresh_on="auto"` 时由回调和进度信号刷新，`"heartbeat"` 时只由 `runtime.heartbeat()` 刷新
- 超时抛 `NodeTimeoutError`；它**不继承内置 `TimeoutError`**，因此默认重试策略会重试它
- **仅支持 async 节点**；同步节点无法安全取消。本项目的模型节点都是 async

节点超时靠 asyncio 取消实现，适合作为兜底；底层 HTTP 请求仍建议配置 provider 自己的 `request_timeout`。

## error_handler

重试耗尽后可以进入降级节点：

```python
from langgraph.errors import NodeError
from langgraph.types import Command

def model_error_handler(state: AgentState, error: NodeError):
    logger.error(f"{error.node} failed: {error.error}")
    return Command(
        update={"messages": [AIMessage(content="模型服务暂时不可用，请稍后重试。")]},
        goto=END,
    )

graph.add_node("model", acall_model, error_handler=model_error_handler)
```

- 处理器可注入 `NodeError(node, error)`，`node` 是失败节点名，`error` 是原始异常
- 处理器本身抛异常时，整个 run 失败（不会再次进入处理器）
- `error_handler` 是“重试耗尽后的最后一道”，不能替代重试

## 挂载位置

| 场景 | 用法 |
| --- | --- |
| 单个 StateGraph 节点 | `graph.add_node(..., retry_policy=..., timeout=..., error_handler=...)` |
| 全图默认 | `graph.set_node_defaults(retry_policy=..., timeout=..., error_handler=...)`；**子图不继承** |
| 函数式 agent | `@entrypoint(retry_policy=..., timeout=...)` |
| `create_agent` / `create_supervisor` | 内部节点不可直接访问；用 `model.with_retry(...)` 包装模型，逐次重试 LLM 调用 |
| 单个 Runnable | `runnable.with_retry(retry_if_exception_type=(...), stop_after_attempt=3, wait_exponential_jitter=True)` |

本项目对应关系：

| Agent | 形态 | 可挂载点 |
| --- | --- | --- |
| `chatbot` | `@entrypoint` | 装饰器参数（**已启用**） |
| `research-assistant` | StateGraph | `add_node("model", ...)` 或 `set_node_defaults` |
| `rag-assistant` | StateGraph | 同上 |
| `interrupt-agent` | StateGraph | 同上；含 `interrupt()` 的节点重试需谨慎 |
| `knowledge-base-agent` | StateGraph | 同上 |
| `bg-task-agent` | StateGraph | 同上；`bg_task` 节点有进度写入，重试需谨慎 |
| `github-mcp-agent` | `create_agent` | `model.with_retry(...)` |
| `langgraph-supervisor-*` | `create_supervisor` | `model.with_retry(...)` |
| `Safeguard` | 普通类 | 包装内部 model，或在 `guard_input` 节点配置策略 |

## 与 SDK 重试的关系

项目没有应用层重试，下面是各 provider 的实际默认值（实测于 2026-10-18）：

| Provider | 重试 | 超时 |
| --- | --- | --- |
| Azure OpenAI | 3 次（`core/llm.py:100` 显式配置） | 60s（`core/llm.py:99`） |
| OpenAI / DeepSeek / OpenRouter（均为 `ChatOpenAI`） | SDK 默认 2 次 | **未设置；底层 httpx 为 `Timeout(None)`，即不超时** |
| Anthropic | SDK 默认 2 次 | **未设置；底层同样为 `Timeout(None)`** |
| Groq | 2 次（库默认） | 未设置，取决于 SDK 默认 |
| Google / Vertex | 6 次（库默认） | 未设置 |
| Ollama / Bedrock | 库默认 | 未设置 |

**不要简单叠加**：SDK 已重试 2 次时，LangGraph 再配 `max_attempts=3` 意味着最多 9 次请求。建议要么降低 SDK 的 `max_retries`，要么把 LangGraph 作为唯一的重试层。

## 本项目现状

`chatbot` 是目前的试点，`src/agents/chatbot.py:8-17`：

```python
CHATBOT_RETRY = RetryPolicy(
    max_attempts=3,
    initial_interval=0.5,
    backoff_factor=2.0,
    max_interval=8.0,
)
CHATBOT_TIMEOUT = TimeoutPolicy(run_timeout=120, idle_timeout=60)


@entrypoint(retry_policy=CHATBOT_RETRY, timeout=CHATBOT_TIMEOUT)
```

实测结果：

- 连续失败 2 次后第 3 次成功，调用计数为 3
- 连续失败 3 次后原始 `ConnectionError` 上抛
- 把超时临时改为 0.3s 后，慢调用每次超时并重试，最终抛 `NodeTimeoutError`，调用计数为 3

尚未完成：

- 其他 agent 还没有配置节点级重试/超时（问题 9）
- 除 Azure 和 chatbot 之外，provider 层没有超时（问题 8）
- 没有降级模型策略

## 注意点

1. **节点级重试重跑整个节点**：模型节点安全；含工具调用、写操作、`interrupt()` 的节点要评估重复副作用。
2. **流式重复**：已经发给客户端的 token 无法撤回，重试会出现“半截 + 完整”的重复内容；`/invoke` 不受影响。
3. **子图不继承** `set_node_defaults`；supervisor 这类嵌套结构需要分别处理。
4. **`timeout` 只支持 async 节点**。
5. `error_handler` 失败会直接终止 run，处理器内部要保持简单、不能再依赖外部服务。

## 测试

- `tests/agents/test_tool_retry.py` 覆盖了工具侧的重试；节点级重试目前只有手工实验，没有回归测试
- 建议补充：`RetryPolicy` 成功/耗尽、`TimeoutPolicy` 超时、`error_handler` 降级
