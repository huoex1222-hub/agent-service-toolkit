# LangGraph ToolNode 异常处理

工具调用的异常要按类型分流。核心原则：**重试会用同一份 `ToolCall` 参数重新执行**，所以参数错误重试永远不会成功，必须把错误回给模型让它改参数。整理于 2026-10-18。

相关文档：

- [异常处理文档大纲](README.md)
- [LangGraph 异常处理总览](<LangGraph 异常处理总览.md>)
- [LangGraph 节点重试与超时](<LangGraph 节点重试与超时.md>)
- [Agent 服务异常处理实践](<Agent 服务异常处理实践.md>)

## 三类错误

| 类型 | 典型来源 | 处理 |
| --- | --- | --- |
| 参数校验失败 | Pydantic `ValidationError` → `ToolInvocationError` | 不重试；`ToolNode` 默认转成 `ToolMessage(status="error")`，模型据此修改参数 |
| 确定性逻辑错误 | 非法表达式、缺配置、`ValueError` / `RuntimeError` | 不重试；返回错误消息给模型，或 fail fast |
| 瞬时错误 | 连接失败、超时、429、5xx、ddgs 限流/超时 | 重试 + 指数退避 + jitter + 单次超时 |

## ToolNode 的默认行为

`ToolNode(tools)` 的 `handle_tool_errors` 默认是 `_default_handle_tool_errors`：

- `ToolInvocationError`（参数校验失败）→ 转成 `ToolMessage(status="error")`，run 继续，模型看到错误后可以修正参数
- 其它异常 → **重新抛出**，终止整个 run

`GraphBubbleUp`（包括 `interrupt`）始终重新抛出，不受 `handle_tool_errors` 影响。

## 异常分流：handle_tool_errors

`handle_tool_errors` 支持：

| 值 | 行为 |
| --- | --- |
| `True` / 异常类型 / 类型元组 | 匹配的异常转成默认模板的 `ToolMessage` |
| 字符串 | 匹配的异常转成固定文本的 `ToolMessage` |
| `Callable` | 处理类型由函数注解推断；返回字符串 → `ToolMessage`，`raise` → 继续上抛 |

自定义 callable 的典型写法：

```python
from langgraph.prebuilt.tool_node import ToolInvocationError

def handle_tool_errors(error: Exception) -> str:
    if is_retryable_tool_error(error):
        raise error                       # 交给重试层
    if isinstance(error, ToolInvocationError):
        return error.message              # 保留参数错误详情
    logger.warning(f"Tool error returned to the model: {error}")
    return f"Tool failed: {error}"        # 回给模型自纠
```

关键点：

- **瞬时错误必须 `raise`**：一旦转成 `ToolMessage`，重试层就再也看不到它
- **参数错误不要 `raise`**：否则会用同样的参数白重试
- 注解为 `Exception` 时所有异常都会进 handler；注解为特定类型/Union 时，只有匹配类型进 handler，其余自动上抛

## 单次调用级重试：awrap_tool_call

推荐在 `ToolNode` 上使用 `awrap_tool_call`。它只包裹**单个工具调用**，不会重跑同一步里的其它工具：

```python
import asyncio, random

RETRYABLE = (ConnectionError, TimeoutError, httpx.TransportError)

async def retry_tool_call(request, execute):
    delay = 0.5
    for attempt in range(1, 4):
        try:
            async with asyncio.timeout(30):       # 单次调用超时
                return await execute(request)
        except RETRYABLE:
            if attempt == 3:
                raise
            await asyncio.sleep(delay + random.uniform(0, delay * 0.2))
            delay = min(delay * 2, 8)             # 指数退避，封顶 8s
```

挂载：

```python
ToolNode(
    tools,
    handle_tool_errors=handle_tool_errors,
    awrap_tool_call=retry_tool_call,
)
```

- 异步路径使用 `awrap_tool_call`；同步路径对应 `wrap_tool_call`
- `execute` 可以被多次调用；每次调用相互独立
- 重试耗尽后抛出的异常仍会经过 `handle_tool_errors`

## 节点级重试与超时

```python
agent.add_node(
    "tools",
    ToolNode(tools),
    retry_policy=RetryPolicy(max_attempts=2, retry_on=...),
    timeout=TimeoutPolicy(run_timeout=120),
)
```

- 重试的是**整个 ToolNode**：一步里有多个工具调用（或并行调用）时会全部重跑
- 只适合只读、幂等的工具（搜索、计算、RAG 检索）
- 有副作用的工具（GitHub 的 create/delete/merge）不要用节点级重试
- `RetryPolicy` 参数细节见 [LangGraph 节点重试与超时](<LangGraph 节点重试与超时.md>)

## 工具内部处理

最细粒度是在 `@tool` 函数内部：

- HTTP 客户端超时：`DDGS(timeout=30)`、`httpx(timeout=...)`
- async 工具：`async with asyncio.timeout(...)`
- 返回错误字符串而不是抛异常（本项目的 `calculator`）
- 在函数内部重试，适合需要幂等控制的场景

注意：**同步工具在线程池中执行，`asyncio.timeout` 只能取消等待，无法终止线程**；工具自身仍需要有超时兜底。

## 本项目落地

### 共享策略：`src/agents/tool_retry.py`

- `retry_tool_call`：3 次尝试，0.5s 起指数退避（封顶 8s、20% jitter），每次 30s 超时
- 只重试瞬时错误：`ConnectionError`、`TimeoutError`、`httpx.TransportError`、ddgs `RatelimitException` / `TimeoutException`，以及带 `status_code` 的 429/5xx
- `handle_tool_errors`：瞬时错误重新抛出；`ToolInvocationError` 保留原始消息；其它错误转成 `ToolMessage("Tool failed: ...")`

### research-assistant

`src/agents/research_assistant.py`：

```python
agent.add_node(
    "tools",
    ToolNode(tools, handle_tool_errors=handle_tool_errors, awrap_tool_call=retry_tool_call),
)
```

`src/agents/tools.py` 的配合改动：

- `calculator` 非法表达式返回错误字符串，不再抛 `ValueError`
- `web_search` 保留日志，但改为重新抛出：瞬时错误进入重试层，确定性错误由 handler 转成工具消息

### 尚未接入

- `rag-assistant`：待按同样方式接入
- `github-mcp-agent` / supervisor：工具可能带副作用，使用 `AgentMiddleware.awrap_tool_call` 做单次调用级重试，不重跑整个 ToolNode

## 验证

`tests/agents/test_tool_retry.py` 覆盖：

- 瞬时错误重试后成功、非瞬时错误不重试、重试耗尽
- `handle_tool_errors` 的分流行为
- `calculator` / `web_search` 的新行为
- 真实 `research-assistant` 图上“模型发起非法 Calculator 调用 → 错误回传 → 模型继续”的集成测试

ToolNode 端到端实验输出：

```text
Tool flaky_tool failed (attempt 1/3): simulated outage 1
Tool flaky_tool failed (attempt 2/3): simulated outage 2
retry result: 3 ok
Tool error returned to the model: deterministic failure
non-retryable: error | Tool failed: deterministic failure
```
