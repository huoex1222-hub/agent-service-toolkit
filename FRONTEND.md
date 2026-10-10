# 前端是怎么和服务交互的

读代码时最容易卡住的一环：后端接口（`/chatbot/stream` 那些）很清楚，但**前端那侧调用的代码在哪、怎么把 token 画到屏幕上**，一眼看不出来。整理于 2026-10-09。

## 一句话心智模型

**这个项目里没有「浏览器调用后端」这回事。** 浏览器只是个渲染终端；真正「调用后端」的代码（一个 httpx 客户端）跑在 **Streamlit 服务器进程**里。

```text
浏览器 ──WebSocket(/_stcore/stream)──> Streamlit 进程 ──httpx(REST/SSE)──> FastAPI 服务
  ↑                                      ↑                                  ↑
"显示终端"                     "前端逻辑 + HTTP 客户端"                "后端"
 只负责渲染                    src/client/client.py                   /{agent}/stream 等
```

三层里，第 1 层（浏览器→Streamlit）是 Streamlit 框架自己的协议，你写代码时碰不到；第 2 层才是这个项目的前端逻辑；第 3 层是你已经熟的后端。

第 1 层展开说清楚（实测于本机）：

| 项 | 值 |
| --- | --- |
| 协议 | **WebSocket**，单向一条长连接：`ws://localhost:8501/_stcore/stream`（握手 101 实测通过） |
| 帧格式 | **二进制 protobuf**，双向：下行 `ForwardMsg`（渲染增量）、上行 `BackMsg`（控件事件） |
| 服务端栈 | **Starlette + uvicorn**（不是老文章里的 Tornado）——所以响应头是 `server: uvicorn` |
| 例外 | 文件上传走 **HTTP POST**（`/_stcore/upload_file/{session_id}/{file_id}`），健康检查/静态资源也是普通 HTTP |

第 2 层则是**普通 HTTP/1.1**：httpx 发 `POST /{agent}/stream`，响应为 `text/event-stream`（SSE）——一个「不结束的响应」，没有 Upgrade、不是 WebSocket。

> 所以「Streamlit 用 socket 和后端通信吗」要看指哪一段：浏览器→Streamlit 是 WebSocket；Streamlit→Agent 服务是普通 HTTP，只不过流式响应的**传输层**当然是 TCP socket。

> 这一条直接解释了那个常见疑问：「为什么 devtools 里看不到 SSE」——因为 SSE 是**第 2 层**发的。详见 [NETWORK_CAPTURE.md](NETWORK_CAPTURE.md) 的「看不到的那一跳」。

## Streamlit 的执行模型

不是事件回调，是**不断重跑脚本**：

1. 用户操作（打字、点按钮）→ 浏览器经 WebSocket 把事件发给 Streamlit 进程
2. Streamlit 把 `main()` **从头到尾重跑一遍**（`src/streamlit_app.py:67`）
3. 重跑中产生的渲染指令（`st.chat_message(...)`、`st.write(...)`）以增量推回浏览器
4. 跨重跑要记住的东西放 `st.session_state`

所以 `streamlit_app.py` 描述的是「每次刷新页面该长什么样」。入口是文件末尾的 `asyncio.run(main())`——`st.*` 全是同步 API，但项目用 async 包了一层，才能 `await` 客户端调用。

## 一次发消息的时序

| # | 发生什么 | 代码 |
| --- | --- | --- |
| 1 | 确定用户身份：session → URL 参数 → 新生成（并写回 URL） | `get_or_create_user_id()` `streamlit_app.py:32` |
| 2 | **首次运行**才创建客户端；构造时 GET `/info` 拿 agent/模型清单 | `:94`、`client.py:62` |
| 3 | 侧边栏用清单填下拉框（**列表不是写死的，是后端拉的**） | `:188-201` |
| 4 | 取用户输入（语音启用时走 STT） | `:307-311` |
| 5 | 发 SSE 请求 `POST /{agent}/stream` | `agent_client.astream(...)` `:322`、`client.py:302` |
| 6 | 逐帧解析 SSE | `_parse_stream_line()` `client.py:179` |
| 7 | 边收边画 | `draw_messages()` `:369` |
| 8 | `st.rerun()` 丢掉临时容器，保证下一轮状态干净 | `:357` |
| 9 | 画反馈星标（有消息时） | `:364-366`、`handle_feedback()` `:528` |

服务端这一侧对应 `service.py:243` 的 `message_generator`：用 `agent.astream(stream_mode=["updates","messages","custom"], subgraphs=True)` 从 LangGraph 抽事件，转成 SSE 帧吐出去。

## 两边约定的数据格式

### SSE 信封（`client.py:179-202`）

```text
data: {"type": "token",   "content": "1"}      → 直接返回字符串（一个 token）
data: {"type": "message", "content": {...}}    → ChatMessage.model_validate(...)
data: {"type": "error",   "content": "..."}    → 包成一条 ai 消息显示
data: [DONE]                                    → 返回 None，循环结束
```

这层信封**不在 Pydantic 模型里**，是纯字符串约定，两端各写一半（生成在 `service.py`，解析在 `client.py`）。改协议时两边要一起改。

### Pydantic 契约（`src/schema/schema.py`）

这个文件是**前后端共享的唯一事实来源**：FastAPI 用它做校验和 OpenAPI，Streamlit 客户端用它做序列化。同一个文件两边 import，所以 schema 不会漂移。

| 类型 | 用途 |
| --- | --- |
| `StreamInput` | 请求体（message / thread_id / user_id / model / stream_tokens） |
| `ChatMessage` | SSE 的 `message` 帧，也是 UI 里渲染的一条消息 |
| `ServiceMetadata` | `GET /info` 的返回 → 填充侧边栏 |
| `UserThreads` / `ChatHistory` | `GET /threads` / `POST /history` |
| `Feedback` | `POST /feedback` |

## 渲染：一批消息怎么变成界面

`draw_messages()`（`:369`）是核心，它吃一个 `AsyncGenerator[ChatMessage | str, None]`，同一个函数**既用于重放历史，也用于流式新消息**（靠 `is_new` 区分）：

- **收到字符串**（token）→ 懒创建一个 `st.empty()` 占位符，每来一个 token 就用累积内容**覆盖写**一次（`:400-414`）→ 浏览器里的打字机效果
- **收到 `ChatMessage(type="ai")`** → 有内容就用最终文本覆盖占位符，并**重置流式变量**给下一段 token 让路（`:441-447`）
- **带 `tool_calls`** → 每个工具调用建一个 `st.status()` 折叠面板，从消息流里 `anext()` 取出配对的 `ToolMessage`，写 Input/Output、状态改 `complete`（`:449-494`）；`transfer_to*` 的调用递归处理子 agent（`handle_sub_agent_msgs`）
- **`type="custom"`** → bg-task-agent 的任务清单

`st.session_state.last_message` 存着「当前气泡」的容器引用（`:393` 重置）——因为后续消息要画进同一个气泡，它也是反馈星标的挂载点。

## 状态放在哪（最容易混的一点）

| 状态 | 存哪 | 生命周期 |
| --- | --- | --- |
| `agent_client`、`messages`、`thread_id`、`last_message` | `st.session_state`（**服务端内存**，按浏览器会话） | 标签页会话 |
| `user_id`、`thread_id`、`agent` | `st.query_params`（URL 里） | 可分享、可书签 |
| **对话历史（真正的记忆）** | 后端 Postgres checkpointer | 永久 |
| threads 列表 | `@st.cache_data(ttl=600)`（`:56`） | 缓存；新建对话（`:352`）或切换 agent（`:200`）时 `.clear()` |

**要点**：UI 里的 `st.session_state.messages` 只是**渲染缓存**，不是数据源。切 thread 或从带参数的 URL 恢复时，会用 `POST /history` 从后端重新拉一份（`:125`、`:175`）。真相永远在服务端。

## 其他交互点

| 功能 | 触发 | 请求 |
| --- | --- | --- |
| 非流式模式 | 设置里关掉 "Stream results" | `ainvoke` 一次性返回（`:343-355`） |
| 历史会话列表 | 侧边栏 "Previous Chats" | `GET /threads?user_id=…&agent_id=…` |
| 切换会话 | 点列表项 | `POST /history`，然后重建 URL 参数 |
| 评分 | 星标控件 | `POST /feedback` → **LangSmith** 的 run（`run_id` 就是它；Langfuse 的 trace id 是它自己生成的，不是这个 id） |
| 语音 | `.env` 配 `VOICE_*_PROVIDER` | OpenAI STT/TTS（本机未启用） |

## 常见困惑

**改了 `.env` 的 `AGENT_URL`，刷新页面没用？**
`AgentClient` 是**会话级单例**（`:94` 只在 session_state 里没有时创建）。刷新标签页不会重建它，要**新开标签页**或重启 Streamlit。

**URL 里为什么有 `?user_id=…&thread_id=…&agent=…`？**
三个都来自 `st.query_params`，是「可分享的会话链接」机制（`:240-259` 的 Share 对话框就是拼这个）。agent 参数还会经 `bind="query-params"` 双向绑定到下拉框（`:199`）。

**UI 卡住/报错时，怎么判断是哪一层的问题？**

| 症状 | 大概率是哪层 | 怎么查 |
| --- | --- | --- |
| 红框 "Error connecting to agent service" | 第 2→3 层之间 | `curl http://localhost:8081/info`（注意 LOCAL_SETUP 坑 9 的端口顶占） |
| 有回复但不是流式的 | 第 3 层 | 后端 `stream_tokens` / 模型不支持流式 |
| 后端有 trace，界面不动 | 第 1 层或渲染逻辑 | 看 Streamlit 控制台报错；`draw_messages` 的占位符逻辑 |
| 完全看不到请求 | 正常 | 第 1 层只有 WebSocket，见 [NETWORK_CAPTURE.md](NETWORK_CAPTURE.md) |

## 如果想要「真·浏览器前端」

那就不用 Streamlit 了，走 AG-UI：`POST /agui/{agent}/run`，浏览器直连服务。届时浏览器才是请求发起方，要自己处理 CORS 和 bearer token，见 [docs/AGUI.md](docs/AGUI.md)；仓库里 `scripts/agui-client` 是个 Node 版参考实现。

---

**相关**：三层结构与抓包的关系见 [NETWORK_CAPTURE.md](NETWORK_CAPTURE.md)；环境与端口问题见 [LOCAL_SETUP.md](LOCAL_SETUP.md)。
