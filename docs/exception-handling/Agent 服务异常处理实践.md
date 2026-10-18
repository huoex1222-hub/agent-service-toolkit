# Agent 服务异常处理实践

本文记录 FastAPI 接口、SSE 流式、AG-UI、启动/关闭、日志和客户端/UI 的异常处理现状与待修项。整理于 2026-10-18，基于 `local-environment-fixes` 分支；行号是当时的快照。

相关文档：

- [异常处理文档大纲](README.md)
- [LangGraph 异常处理总览](<LangGraph 异常处理总览.md>)
- [LangGraph 节点重试与超时](<LangGraph 节点重试与超时.md>)
- [LangGraph ToolNode 异常处理](<LangGraph ToolNode 异常处理.md>)

## HTTP 状态码约定

| 场景 | 状态码 | 位置 |
| --- | --- | --- |
| Bearer token 错误 | 401 | `verify_bearer`（`src/service/service.py:66-76`） |
| 模型不在 `AVAILABLE_MODELS` | 400 | `ensure_model_available`（`src/service/utils.py:83-90`） |
| `agent_config` / `forwardedProps.configurable` 含保留键 | 422 | `service.py:170-174`、`src/service/agui.py:52-56` |
| 请求体 Pydantic 校验失败 | 422 | FastAPI 自动 |
| AG-UI 未知 agent | 404 | `src/service/agui.py:122-125` |
| 未知 agent（普通接口） | **500（应为 404，见问题 1）** | `service.py:229/264/457/491` |
| Agent 执行/历史/线程查询异常 | 500 `Unexpected error` | `service.py:251-253/473-475/498-500` |

已知错误的兜底写法一致：`logger.exception(...)` 记录完整栈，客户端只拿到 `"Unexpected error"`。

## 各端点

### `/invoke`

```python
try:
    ...
except Exception as e:
    logger.exception(f"An exception occurred: {e}")
    raise HTTPException(status_code=500, detail="Unexpected error")
```

- 把 `_handle_input` 放在 `try` 之外，所以模型不可用（400）和保留键（422）能按预期返回
- 其余异常统一 500；客户端只看到通用文案

### `/history` 与 `/threads`

- 都是 `except Exception` → `logger.exception` → 500 `Unexpected error`
- `get_agent()` 的 `KeyError` 在 `try` 之外，因此未知 agent 会变成 500
- `/threads` 在 `list_user_threads` 失败时返回 500（`tests/service/test_threads.py:233-242` 有覆盖）

### `/feedback`

- LangSmith 的 `client.create_feedback` **没有兜底**，异常会直接冒泡（问题 5）
- Langfuse 写分数单独 `try/except`，失败只记日志，不影响已写入 LangSmith 的结果（`service.py:435-445`）

### `/health`

- 只探测 Langfuse：失败时返回 `"langfuse": "disconnected"`，HTTP 仍为 200
- **不探测 LLM provider**，provider 全挂时健康检查仍为 `ok`

## SSE 流式（`message_generator`）

`service.py:256-370` 有三层保护：

1. **单条消息**：`langchain_to_chat_message` 失败 → 发 `{"type": "error", "content": "Unexpected error"}` → `continue`，流继续
2. **整个流**：外层 `except Exception` → 发 `{"type": "error", "content": "Internal server error"}`
3. **收尾**：`finally` 记录 `Run finished` 并发 `data: [DONE]`

注意 **`_handle_input` 和 `get_agent` 都在 `try` 之前**（问题 2）：

- 它们的异常不会触发上面任何一层
- 不会发 error 事件、不会发 `[DONE]`、`finally` 也不会执行

流式失败时已经发出的 token 无法撤回，客户端会看到“半截回答 + 错误提示”；HTTP 状态码保持 200。

## AG-UI

`src/service/agui.py`：

- `_base_config` 在返回 `StreamingResponse` **之前**执行，所以 404/422/400 都是正常的 HTTP 响应
- `_event_stream` 在流开始后**没有 try/except**；`ag-ui-langgraph` 只把显式 error 事件转成 `RunErrorEvent`，不会兜住抛出的异常
- LLM 或节点异常会导致 SSE 连接直接中断，而不是一条结构化的 `RUN_ERROR` 事件（问题 11）

## 启动与关闭（lifespan）

`service.py:79-119`：

- DB / store 初始化失败 → 记录后 `raise`，进程不启动（fail fast）
- 单个 agent `load_agent` 失败 → `logger.error` 后继续，其余 agent 正常提供服务
- 未配置 `AUTH_SECRET` → 只发 warning，所有接口无鉴权
- lazy agent 加载失败后，请求该 agent 会在 `get_agent` 抛 `RuntimeError` → 500（问题 6）

## LLM 失败时的入口行为

`RetryPolicy`、SDK 重试和超时的细节见 [LangGraph 节点重试与超时](<LangGraph 节点重试与超时.md>)。从服务侧看：

| 入口 | 行为 |
| --- | --- |
| `/invoke` | `logger.exception`（有栈）→ 500 `Unexpected error` |
| `/stream` | `logger.error`（**无栈**）→ error 事件 → `[DONE]`；HTTP 200 |
| `/agui/run` | 无捕获，SSE 连接中断 |

重试耗尽后才会走到这里；如果配置了 LangGraph 重试，错误出现的时间会相应延后。

## 日志与可观测性

- `/invoke` 使用 `logger.exception`，有完整栈
- `/stream` 使用 `logger.error(f"Error in message generator: {e}")`，**没有栈**（问题 10）
- `LogLLMCalls` 只实现 `on_llm_end`，没有 `on_llm_error`：失败调用不会留下“哪个模型失败”的日志
- `LogGraphNodes.on_chain_error` 会记 `Node: xxx failed after Ns`，但不带异常内容
- 请求级的 `run_id` / `thread_id` / `user_id` 通过 `core/log_context.py` 的 contextvar 注入日志格式，便于按 run 或 thread 检索

## 客户端与 UI

### AgentClient

- 每个 HTTP 方法把 `httpx.HTTPError` 包成 `AgentClientError`
- **但响应体解析不在 `try` 内**：`response.json()` / `model_validate` 失败会抛原始 `JSONDecodeError` / `ValidationError`（问题 3）
- `_parse_stream_line` 对畸形 SSE 抛裸 `Exception`（`client.py:186-195`）
- `/stream` 的 error 事件被转成 `ChatMessage(type="ai", content="Error: Internal server error")`（`client.py:199-201`），**作为普通 AI 消息显示并留在会话里**，不是错误提示

### Streamlit

- `AgentClientError` → `st.error` + `st.stop`（`streamlit_app.py:359-361/544-553`）
- 意外消息类型同样 `st.error` + `st.stop`（`streamlit_app.py:415-418/522-525`）
- 由于上面的裸异常，服务端返回畸形数据时 UI 的错误处理可能被绕过

## 待修问题

| # | 优先级 | 问题 | 位置 |
| --- | --- | --- | --- |
| 1 | 高 | 普通接口未知 agent 返回 500 而非 404；`get_agent()` 的 `KeyError` 未被捕获 | `service.py:229/264/457/491`；对照 `agui.py:122-125` |
| 2 | 高 | `message_generator` 的 `_handle_input` / `get_agent` 在 `try` 之外，异常时不发 error 事件、不发 `[DONE]`、`finally` 不执行 | `service.py:264-267` |
| 3 | 中 | 客户端只包装 `httpx.HTTPError`；JSON/模型校验错误和流解析错误会以裸异常冒泡，Streamlit 捕获不到 | `client.py:70-73/128-132/177/186-195` |
| 5 | 低 | `/feedback` 的 LangSmith 调用无兜底，与相邻代码风格不一致 | `service.py:427-434` |
| 6 | 低 | agent 加载失败无对外信号：GitHub MCP 静默降级；lazy agent 加载失败后请求变成 `RuntimeError` → 500 | `github_mcp_agent.py:80-83`、`service.py:104-109`、`agents/agents.py:79-82` |
| 10 | 中 | `/stream` 失败无栈日志；没有 `on_llm_error`，失败调用不可见 | `service.py:366`、`service/utils.py:26-44` |
| 11 | 中 | AG-UI 流中异常未捕获，连接直接中断而非发 `RUN_ERROR` 事件 | `agui.py:89-107` |

建议修复顺序：

1. **问题 1**：在 `get_agent` 调用处复用 AG-UI 的 `KeyError → 404`；`/stream` 需要先完成 agent 解析再返回 `StreamingResponse`，否则只能在流内发 error 事件
2. **问题 2**：把 `get_agent` + `_handle_input` 移入 `try`，或在 generator 入口单独兜底并发 `[DONE]`
3. **问题 3**：`_parse_stream_line` 与响应解析统一抛 `AgentClientError`，并保留原始异常为 `__cause__`
4. **问题 10**：`/stream` 改用 `logger.exception`；给 `LogLLMCalls` 补 `on_llm_error`
5. **问题 11**：`_event_stream` 包 try/except，按 AG-UI 协议发 `RUN_ERROR` 事件后正常结束
6. 其余按需处理

## 测试覆盖

现有覆盖：

- `tests/service/test_threads.py:233-242`：checkpointer `alist` 抛错 → 500
- `tests/service/test_agui.py:144-146`：未知 agent → 404
- `tests/client/test_client.py`：HTTP 错误 → `AgentClientError`
- `tests/service/test_streamlit_app.py`：正常路径无异常

缺口：

- 普通接口未知 agent 的 404/500 行为
- `message_generator` 在进入 `try` 之前失败的流行为
- 客户端收到畸形 JSON / 畸形 SSE 时的异常类型
- LLM 抛错时 `/invoke`、`/stream`、`/agui/run` 各自的行为（500 / error 事件 / 连接中断）
- 各 provider 的超时与重试配置断言
- `on_llm_error` 日志是否产生

> 注：`tests/app/test_streamlit_app.py::test_app_simple_non_streaming` 在 Windows 本地会因 `tests/conftest.py` 的 `mock_env` 清空环境变量、Streamlit 后台线程执行 `Path.home()` 失败而报错；这是环境问题，与业务代码无关。
