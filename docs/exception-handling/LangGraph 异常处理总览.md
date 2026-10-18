# LangGraph 异常处理总览

本目录梳理本项目的异常传播路径、LangGraph 自带的重试/超时能力，以及工具层和服务层的落地方式。整理于 2026-10-18，基于 `local-environment-fixes` 分支；行号是当时的快照，代码变动后以函数名为准。

相关文档：

- [异常处理文档大纲](README.md)
- [LangGraph 节点重试与超时](<LangGraph 节点重试与超时.md>)
- [LangGraph ToolNode 异常处理](<LangGraph ToolNode 异常处理.md>)
- [Agent 服务异常处理实践](<Agent 服务异常处理实践.md>)

## 一句话总结

整体策略是**边界处捕获、内部 fail fast、对外不泄露内部细节**：预期错误映射成 `HTTPException` / `AgentClientError`，未知错误统一 500 + 通用文案，栈只进日志；外部依赖（Langfuse、提示词、语音、存储）尽量降级而不是中断请求。

## 分层一览

| 层 | 位置 | 策略 |
| --- | --- | --- |
| HTTP 接口 | `src/service/service.py` | 预期错误 400/401/404/422；未知错误 500 `Unexpected error` |
| SSE 流式 | `service.message_generator` | 逐消息 error 事件后继续；外层兜底；`finally` 发 `[DONE]` |
| 启动/lifespan | `service.lifespan` | DB 初始化失败 fail fast；单个 agent 加载失败继续 |
| Agent / 工具 | `src/agents/` | 工具错误按类型分流；部分节点吞异常降级 |
| 配置 / 存储 | `core/settings.py`、`src/memory/` | 配置缺失启动即 `ValueError`；连接池 `finally` 关闭 |
| 可观测性 | `service/tracing.py`、`core/prompts.py` | 附加元数据/拉取提示词失败只告警并回退 |
| 语音 | `src/voice/` | 初始化失败禁用功能；转写/合成失败返回 `""` / `None` |
| 客户端 | `src/client/client.py` | `httpx.HTTPError` → `AgentClientError` |
| UI | `src/streamlit_app.py` | 捕获 `AgentClientError` → `st.error` + `st.stop` |

## 异常传播主流程

```text
LLM / 工具 / 节点内部抛异常
  ├─ 工具层：ToolNode 按 handle_tool_errors 分流（见 ToolNode 文档）
  │    ├─ 参数错误 → ToolMessage → 模型自纠
  │    ├─ 瞬时错误 → 重试；耗尽后继续上抛
  │    └─ 确定性错误 → ToolMessage → 模型自纠
  ├─ 节点层：RetryPolicy 重试 / TimeoutPolicy 超时 / error_handler 降级（见节点文档）
  │    └─ 仍失败 → LangGraph 终止本次 run（最后成功的 checkpoint 保留）
  └─ HTTP 层：
       ├─ /invoke    → 500 "Unexpected error" + X-Run-ID/X-Error-Code
       ├─ /stream    → SSE error 事件（code + run_id）+ [DONE]
       └─ /agui/run  → RUN_ERROR 事件（code + run_id）
```

### 失败后的状态

用 LangGraph 1.2.7 + `InMemorySaver` 实测：节点抛异常后，本次 run 的 `HumanMessage` 已写入 checkpoint，但失败节点的状态不会提交。

- 没有自动回滚，也没有“重试上一次 run”的接口
- 用户下一条消息会带着未得到回答的旧问题一起进入上下文
- 半截 AI 消息不会进入 state（流式 token 不写 state），但已到达客户端
- 流式失败时已经发出的 token 无法撤回，客户端会看到“半截回答 + 错误提示”

## 各入口处置

| 入口 | 行为 | 位置 |
| --- | --- | --- |
| `/invoke` | `logger.exception`（有栈）→ 500；`X-Run-ID` / `X-Error-Code` 响应头 + detail 带 run_id | `service.py:251-257` |
| `/stream` | `logger.exception`（有栈）→ 发 error 事件（`code` + `run_id`）→ `finally` 发 `[DONE]`；HTTP 状态仍为 200 | `service.py:256-380` |
| `/agui/run` | 捕获异常 → 发 `RUN_ERROR` 事件（`code` + `run_id`） | `agui.py:89-115` |

更多细节见 [Agent 服务异常处理实践](<Agent 服务异常处理实践.md>)。

## Agent / 工具层的降级点

| 位置 | 行为 | 性质 |
| --- | --- | --- |
| `agents/tools.py` `web_search` | 记录日志后重新抛出，交给 ToolNode 的重试层/错误处理器分类 | 瞬时错误可重试；确定性错误回给模型 |
| `agents/tools.py` `calculator` | 非法表达式抛 `ValueError`，由 handler 转成 `ToolMessage(status="error")` | 不重试，模型可据此自纠 |
| `agents/tools.py:55-64` `load_chroma_db` | 初始化 embedding 失败 → `RuntimeError` | fail fast，会终止该次 run |
| `agents/safeguard.py:96-97` | JSON 解析失败 → `SafetyAssessment.ERROR` | 路由把 `ERROR` 当 safe，**fail-open** |
| `agents/knowledge_base_agent.py:119-121` | 检索失败 → 空文档列表 | 降级；模型会回答“信息不足”，掩盖配置错误 |
| `agents/interrupt_agent.py:98-125/174-178` | store 读写失败 → 记日志继续 | best-effort |
| `agents/github_mcp_agent/github_mcp_agent.py:80-83` | MCP 初始化失败 → 无工具继续 | 吞掉错误，agent 仍出现在 `/info` |
| `research_assistant.py` `safeguard_input` | Safeguard 的 LLM 调用异常未被捕获 | 直接终止 run |
| `knowledge_base_agent.py` `acall_model` | 只兜了检索异常，最后的 LLM 调用无兜底 | 直接终止 run |
| `interrupt_agent.py` `determine_birthdate` | 只兜了 store 读写，birthdate 抽取的 LLM 调用无兜底 | 直接终止 run |

工具层的完整分流规则见 [LangGraph ToolNode 异常处理](<LangGraph ToolNode 异常处理.md>)。

## 配置 / 存储 / 可观测性

- `core/settings.py:177-178`：没有任何 LLM key → 导入时 `ValueError`，服务起不来
- `core/settings.py:243-255`：Azure deployment map JSON 解析失败 → `ValueError`；缺 deployment 同样
- `memory/postgres.py:14-37`、`memory/mongodb.py:24-37`：配置校验抛 `ValueError`；连接池在 `finally` 中关闭
- `core/prompts.py:58-60`：Langfuse 提示词不可用 → warning + 本地默认值
- `service/tracing.py:46-53`：附加 provider trace id 失败 → warning，然后仍调用父类
- `service/utils.py:77-80`：图节点失败会记一条 `Node: ... failed after ...`

## 待修问题索引

| # | 优先级 | 问题 | 负责文档 |
| --- | --- | --- | --- |
| 1 | 高 | 普通接口未知 agent 返回 500 而非 404 | [服务实践](<Agent 服务异常处理实践.md>) |
| 2 | 高 | `message_generator` 的 `_handle_input` / `get_agent` 在 `try` 之外 | [服务实践](<Agent 服务异常处理实践.md>) |
| 3 | 中 | 客户端只包装 `httpx.HTTPError`；错误帧的 code/run_id 已能透传，畸形 JSON 仍是裸异常 | [服务实践](<Agent 服务异常处理实践.md>) |
| 4 | ~~中~~ 已修复 | calculator 错误处理 + research-assistant 工具重试 | [ToolNode](<LangGraph ToolNode 异常处理.md>) |
| 5 | 低 | `/feedback` 的 LangSmith 调用无兜底 | [服务实践](<Agent 服务异常处理实践.md>) |
| 6 | 低 | agent 加载失败无对外信号 | [服务实践](<Agent 服务异常处理实践.md>) |
| 7 | 低（需产品决策） | Safeguard 解析失败 fail-open | 本文档降级点 / 节点文档 |
| 8 | 高 | 除 Azure 和 chatbot 节点超时外，LLM 请求没有超时 | [节点重试与超时](<LangGraph 节点重试与超时.md>) |
| 9 | 中 | 仅 chatbot 启用了 `RetryPolicy` | [节点重试与超时](<LangGraph 节点重试与超时.md>) |
| 10 | ~~中~~ 已修复（2026-10-18） | `/stream` 改用 `logger.exception`；补 `on_llm_error` | [服务实践](<Agent 服务异常处理实践.md>) |
| 11 | ~~中~~ 已修复（2026-10-18） | AG-UI 捕获异常并发 `RUN_ERROR` | [服务实践](<Agent 服务异常处理实践.md>) |

## 测试覆盖概览

- `tests/agents/test_tool_retry.py`：工具重试、错误分流、calculator/web_search 行为、research-assistant 集成
- `tests/service/test_threads.py:233-242`：checkpointer `alist` 抛错 → 500
- `tests/service/test_agui.py:144-146`：未知 agent → 404
- `tests/client/test_client.py`：HTTP 错误 → `AgentClientError`
- 缺口：未知 agent 的 404/500、流式入口提前异常、畸形 JSON/SSE、LLM 抛错时各入口行为、provider 超时/重试断言
