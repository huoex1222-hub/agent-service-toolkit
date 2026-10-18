# 后端接口设计

记录后端接口的契约、路由、流式格式，以及普通 API 与 AG-UI 的关系。整理于 2026-10-18。

## 一句话总结

后端接口是**本仓库里用 FastAPI 路由 + Pydantic 模型手写定义的**，不是从外部 OpenAPI 规范生成的。唯一遵循外部协议的是 `/agui`（AG-UI 协议）；其余接口是本项目自定义的 REST + SSE。

## 两套接口

| 接口 | 是否 AG-UI | 主要消费方 |
| --- | --- | --- |
| `/invoke`、`/stream`、`/history`、`/threads`、`/feedback`、`/info` | 否 | 本仓库的 Streamlit 前端、`AgentClient` |
| `/agui/run`、`/agui/{agent_id}/run` | 是 | CopilotKit 等 AG-UI 兼容前端 |

两套接口共享：

- 同一个 agent 注册表（`src/agents/agents.py`）
- 同一个 checkpointer / thread（`/stream` 开的会话可以用 AG-UI 接着聊，反之亦然）
- 同一套 bearer 鉴权
- 同一套 Langfuse / LangSmith tracing

## 依据一览

| 依据 | 位置 | 说明 |
| --- | --- | --- |
| Web 框架 | `src/service/service.py` | FastAPI + uvicorn；`app = FastAPI(...)`，由 `src/service/__init__.py` 导出 |
| 数据契约 | `src/schema/schema.py` | 请求/响应 Pydantic 模型；前后端共用，也是 OpenAPI 的来源 |
| 路由 | `service.py`、`service/agui.py` | 手写 `@router.post/get`；agent 维度路由 + 默认 agent 别名 |
| 鉴权 | `service.py:66-76` | `HTTPBearer` + 可选 `AUTH_SECRET`，挂在 router 依赖上，AG-UI 共用 |
| 流式协议 | `service.py:message_generator` | 自定义 SSE：`message` / `token` / `error` / `[DONE]` |
| AG-UI | `service/agui.py` | `ag-ui-langgraph` 负责 LangGraph → AG-UI 事件翻译 |
| 执行层 | `src/agents/agents.py` | LangGraph 的 `CompiledStateGraph | Pregel`；`ainvoke` / `astream` |
| OpenAPI | FastAPI 自动生成 | `/openapi.json`、`/docs`、`/redoc` |
| 客户端 | `src/client/client.py` | 手写 httpx 客户端，复用同一套 Pydantic 模型 |

## 路由一览

| 方法 | 路径 | 请求模型 | 响应 |
| --- | --- | --- | --- |
| GET | `/info` | — | `ServiceMetadata` |
| POST | `/invoke`、`/{agent_id}/invoke` | `UserInput` | `ChatMessage` |
| POST | `/stream`、`/{agent_id}/stream` | `StreamInput` | `text/event-stream` |
| POST | `/feedback` | `Feedback` | `FeedbackResponse` |
| POST | `/history`、`/{agent_id}/history` | `ChatHistoryInput` | `ChatHistory` |
| GET | `/threads`、`/{agent_id}/threads` | `UserThreadsInput`（query） | `UserThreads` |
| GET | `/health` | — | JSON 状态 |
| POST | `/agui/run`、`/agui/{agent_id}/run` | `RunAgentInput`（AG-UI） | AG-UI SSE 事件 |

设计要点：

- **agent 维度**：`/{agent_id}/...` 走指定 agent；不带 agent 的路径走 `DEFAULT_AGENT`
- **operation_id**：成对路由显式指定不同的 `operation_id`，避免 OpenAPI 冲突；`custom_generate_unique_id` 生成默认 operation id，便于客户端代码生成
- **请求/响应模型**：全部在 `src/schema/schema.py`，带 `description` / `examples`，直接反映在 `/docs` 和 `/redoc`

## 数据契约

`src/schema/schema.py` 是前后端共享的唯一事实来源：

- 请求：`UserInput`、`StreamInput`（增加 `stream_tokens`）、`ChatHistoryInput`、`UserThreadsInput`、`Feedback`
- 响应：`ChatMessage`、`ChatHistory`、`UserThreads`、`ThreadSummary`、`ServiceMetadata`、`AgentInfo`、`FeedbackResponse`
- `UserInput` 支持 `model`、`thread_id`、`user_id`、`agent_config`；`agent_config` 的保留键会被 422 拒绝

`AgentClient` 和 Streamlit 直接 import 这些模型，所以 schema 不会在前后端漂移。

## 普通 API 的流式协议

`POST /stream` 返回 `text/event-stream`，帧格式是本项目自定义的：

```text
data: {"type": "message", "content": {...ChatMessage...}}
data: {"type": "token", "content": "..."}
data: {"type": "error", "content": "Internal server error", "code": "node_timeout", "run_id": "..."}
data: [DONE]
```

- `message`：完整的中间/最终消息
- `token`：模型 token（`stream_tokens=true` 时）
- `error`：错误事件；`code` 是稳定分类，`run_id` 与 Langfuse trace id 对应
- `[DONE]`：流结束标记，`finally` 中始终发送

非流式 `/invoke` 的失败返回 500，并带 `X-Run-ID` / `X-Error-Code` 响应头；detail 里也包含 `run_id` 和 `code`。

错误契约的完整说明见 [Agent 服务异常处理实践](exception-handling/Agent%20服务异常处理实践.md)。

## AG-UI

`/agui` 是本项目对 [AG-UI 协议](https://docs.ag-ui.com) 的适配层：

- 请求体：标准 `RunAgentInput`
- 执行：`ag_ui_langgraph.LangGraphAgent(name=agent_id, graph=graph, config=config)`
- 事件：`agent.run(input_data)` 产出 AG-UI 事件，`EventEncoder` 编码为 SSE
- 路由：`/agui/run`（默认 agent）、`/agui/{agent_id}/run`
- 失败：捕获异常后发 `RUN_ERROR` 事件（`message` + `code`），不再直接断连
- `RAW` 事件被过滤，避免把完整 prompt 等内部信息发给客户端

共享与差异：

- 共享 bearer 鉴权、agent 注册表、checkpointer/thread、tracing
- `forwardedProps.configurable` 对应普通 API 的 `model` / `user_id` / `agent_config`；`thread_id`、`checkpoint_id`、`checkpoint_ns` 是保留键
- **`/feedback` 和 `/history` 没有桥接**：AG-UI 的 `runId` 是客户端生成的，不作为 LangSmith run id；AG-UI 客户端从事件流自行维护历史
- `interrupt` 以 `CUSTOM` 事件 `on_interrupt` 暴露；用 `forwardedProps.command.resume` 恢复。HITL 的完整梳理见 [HITL.md](HITL.md)
- AG-UI 的 shared-state 会发 `STATE_SNAPSHOT`，不要把秘密放进 graph state

完整用法见 [AGUI.md](AGUI.md)。

## OpenAPI 与客户端

- `GET /openapi.json`：机器可读的 OpenAPI
- `GET /docs`：Swagger UI
- `GET /redoc`：ReDoc（README 中也提到）
- SSE 响应不是标准 OpenAPI 类型，用 `responses=_sse_response_example()` 在文档里给出示例；真正的消费方是 `AgentClient._parse_stream_line`
- 仓库里没有生成式客户端；`operation_id` 是为将来生成客户端准备的

## 执行层

- `agents/agents.py` 维护 `agents: dict[str, Agent]`；`get_agent()` 返回已加载的 `CompiledStateGraph | Pregel`
- `/invoke` 使用 `agent.ainvoke(..., stream_mode=["updates", "values"])`
- `/stream` 使用 `agent.astream(..., stream_mode=["updates", "messages", "custom"], subgraphs=True)`
- `_handle_input` 负责生成/绑定 `run_id`、`thread_id`、`user_id`，构造 `RunnableConfig`，处理 interrupt 恢复
- checkpointer / store 由 `src/memory/` 根据 `DATABASE_TYPE` 初始化，lifespan 中注入到 agent

## 新增接口

1. 在 `src/schema/schema.py` 定义请求/响应模型
2. 在 `src/service/service.py` 添加路由和 `operation_id`；需要 agent 维度时同时提供 `/{agent_id}/...` 和默认路径
3. 流式接口沿用 `message_generator` 的 SSE 格式，或单独实现
4. 在 `src/client/client.py` 添加对应方法
5. 如果接口需要鉴权，确保挂在 `router` 下（AG-UI router 已包含在 `router` 内）
