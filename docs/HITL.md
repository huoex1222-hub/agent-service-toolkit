# Human-in-the-Loop（HITL）现状

记录项目里的 HITL 实现：基于 LangGraph 的 `interrupt()` + `Command(resume=...)`。整理于 2026-10-18。

## 一句话总结

**有 HITL，但只是一个演示型实现**：`interrupt-agent` 在拿不到生日时暂停，等待用户回复后恢复。普通 API 和 AG-UI 都支持恢复；没有通用的工具审批/确认流程。

## 代码位置

| 位置 | 作用 |
| --- | --- |
| `src/agents/interrupt_agent.py:149-168` | `determine_birthdate` 拿不到生日时调用 `interrupt(...)`；恢复后把用户回复加入消息并重新抽取 |
| `src/service/service.py:192-202` | `_handle_input` 检查 `state.tasks` 里是否有 pending interrupt；有则把本次用户输入包装成 `Command(resume=user_input.message)`，否则作为普通 `HumanMessage` |
| `src/service/service.py:236-241` | `/invoke` 识别最终事件里的 `__interrupt__`，把第一个 interrupt 的值作为 AI 消息返回 |
| `src/service/service.py:295-301` | `/stream` 把 `__interrupt__` 更新转成 AI 消息推给客户端 |
| `src/agents/agents.py:53-55` | `interrupt-agent` 注册在 agent 列表里，路径为 `/interrupt-agent/invoke` 或 `/interrupt-agent/stream` |
| `docs/AGUI.md:77-78` | AG-UI 下 interrupt 以 `CUSTOM` 事件 `on_interrupt` 暴露，用 `forwardedProps.command.resume` 恢复 |
| `tests/service/test_service_real_graphs.py:135-185` | 真实图 + memory/sqlite checkpointer，验证 `/invoke`、`/stream` 都能恢复 interrupt |
| `tests/service/test_agui.py:227-240` | AG-UI 的 interrupt → `on_interrupt` → resume 测试 |

## 普通 API 流程

```text
POST /interrupt-agent/invoke  {"message": "hi", "thread_id": "t"}
  → 200，返回 AI 消息 "Please tell me your birthdate?"
POST /interrupt-agent/invoke  {"message": "1990-01-01", "thread_id": "t"}
  → _handle_input 检测到 pending interrupt
  → Command(resume="1990-01-01")
  → 继续执行，返回最终回答
```

要点：

- 恢复必须复用同一个 `thread_id`
- 依赖 checkpointer 持久化 interrupt 状态；服务在 lifespan 中注入 checkpointer/store
- `/invoke` 和 `/stream` 共用 `_handle_input`，所以两种调用都支持 HITL
- `/invoke` 只返回第一个 interrupt 的值；`/stream` 会把 `__interrupt__` 更新逐条作为 AI 消息发出

## AG-UI 流程

```text
POST /agui/interrupt-agent/run
  → 事件流里出现 CUSTOM / on_interrupt
POST /agui/interrupt-agent/run
  {"forwardedProps": {"command": {"resume": "blue"}}, "messages": []}
  → 恢复执行
```

- 恢复同样复用 `threadId`
- AG-UI 的 `runId` 是客户端生成的，不用于 LangSmith run id；`/feedback` 和 `/history` 没有桥接到 AG-UI

## Streamlit

没有专门的 HITL 控件：

- interrupt 的值会被当作一条普通 AI 消息显示
- 用户在聊天框里回复即可，下一次请求会自动走 resume 分支
- `src/streamlit_app.py:273-274` 为 `interrupt-agent` 提供了欢迎语

## 测试覆盖

- `tests/service/test_service.py::test_invoke_interrupt`
- `tests/service/test_service.py::test_stream_interrupt`
- `tests/service/test_service_real_graphs.py::test_invoke_resumes_an_interrupted_thread`
- `tests/service/test_service_real_graphs.py::test_stream_resumes_an_interrupted_thread`
- `tests/service/test_agui.py::test_agui_interrupt_and_resume`

## 哪些不是 HITL

- `/feedback` 的星级评价是**事后人工反馈**，不参与 agent 执行
- 工具调用没有“人工审批/确认”环节；`github-mcp-agent` 的删除、合并等操作不会先停下来等人批准
- 没有 `interrupt_before` / `interrupt_after` 的节点级配置，只有 `interrupt_agent` 里显式调用的 `interrupt()`

## 限制

1. **只有一个演示 agent**：`interrupt-agent` 用来展示生日提取，不是通用 HITL 框架
2. **恢复语义很粗**：普通 API 把“下一条用户消息”直接当作 resume 值，没有结构化表单、审批选项或同一轮多个 interrupt 的处理
3. **节点会重跑**：LangGraph 恢复时节点从头执行，`interrupt()` 之前的 LLM 调用、store 读写会再次发生；给含 interrupt 的节点配置重试要特别小心
4. **依赖 checkpointer**：interrupt 状态必须持久化，否则无法跨请求恢复
5. **没有超时/过期机制**：pending interrupt 会一直留在 checkpoint 里，等用户下次发消息

## 新增 HITL 节点时

1. 在节点里调用 `interrupt(payload)`，payload 会作为 AI 消息显示给用户
2. 恢复后节点会从头执行，确保 `interrupt()` 之前的操作可重复执行或幂等
3. 客户端必须复用同一个 `thread_id`
4. 普通 API 的恢复值就是下一条用户消息；如果需要结构化输入，需要自己约定 payload 格式
5. 如果同时支持 AG-UI，确认 `on_interrupt` / `command.resume` 的处理
