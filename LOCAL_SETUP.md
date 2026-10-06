# 本地启动记录

本机（Windows 11）把 agent-service-toolkit 跑起来的完整过程、踩过的坑和验证命令。整理于 2026-09-30。

## 文档索引

仓库根目录下这几份是**本机环境记录**，都不进 git：

| 文档 | 内容 | 什么时候看 |
| --- | --- | --- |
| **LOCAL_SETUP.md**（本文） | 环境现状、启动步骤、8 个坑、数据库与验证命令 | 重建环境，或遇到启动/连接问题 |
| [NETWORK_CAPTURE.md](NETWORK_CAPTURE.md) | 抓包与流量分析：Langfuse（语义层）、mitmproxy（字节层）、Wireshark | 想知道 agent 到底发了什么请求 |
| [LANGFUSE.md](LANGFUSE.md) | Langfuse 自托管的部署、端口改动、两个部署坑 | 启动或维护 tracing |
| [RAG.md](RAG.md) | rag-assistant 的 embedding 配置（硅基流动）、建库流程、两个坑 | 启用或调试 RAG |

`README.md` 和 `CLAUDE.md` 是仓库自带的，不属于本地记录。

几份文档的关系：

```text
LOCAL_SETUP.md          环境怎么起来、有哪些坑
 ├── NETWORK_CAPTURE.md 起来之后怎么看流量
 │    └── LANGFUSE.md   其中「方案①」的完整部署
 └── RAG.md             启用 rag-assistant 需要的配置
```

> 这几份 `.md` 都是 **untracked**。这仓库是 GitHub template，如果要推远端，注意别把它们一起带上（或加进 `.gitignore`）。

## 最终形态

```text
浏览器 ──→ Streamlit :8501 ──→ Agent 服务 :8081 ──→ DeepSeek API
                                      └──→ PostgreSQL :5432 / 库 agent_service
                                           （复用本机 dify 栈的 Postgres 实例）
```

出站流量再套两层（2026-10-04 加上的）：

```text
Agent 服务 :8081 ──→ mitmproxy :8888 ──→ Clash :7890 ──→ DeepSeek / 搜索 / GitHub MCP
                         │
                         ├──→ mitmweb UI :8088    原始 HTTP 报文
                         └──→ Langfuse :3000      agent 语义层 trace
```

| 组件 | 地址 | 说明 |
| --- | --- | --- |
| Streamlit UI | <http://localhost:8501> | 聊天界面 |
| Agent 服务 | <http://localhost:8081> | FastAPI，`/info` 可查 agent 和模型 |
| PostgreSQL | `localhost:5432` | dify 的 `dify-db_postgres-1` 容器，独立库 `agent_service` |
| mitmproxy | `127.0.0.1:8888` | 出站必须经过它，`.env` 的代理指向这里 |
| mitmweb UI | <http://127.0.0.1:8088> | 看原始报文，见 [NETWORK_CAPTURE.md](NETWORK_CAPTURE.md) |
| Langfuse | <http://localhost:3000> | agent 调用链，见 [LANGFUSE.md](LANGFUSE.md) |
| 默认模型 | `deepseek-v4-flash` | 只配了 `DEEPSEEK_API_KEY`，其余模型不可选 |

> **停 mitmproxy 前必须把 `.env` 的 `HTTP_PROXY`/`HTTPS_PROXY` 改回 `7890`**，否则服务完全出不了网。

## 本机环境

| 项 | 值 | 备注 |
| --- | --- | --- |
| Python | 3.13.5 | 仓库要求 `>=3.12,<3.15` |
| uv | **0.12.5** | 必须 ≥ 这个版本，见下文「坑 1」 |
| Docker | 29.3.1 | Docker Desktop，Postgres 由它托管 |
| 仓库 | `D:\stu\github\agent-service-toolkit` | Python 3.13 是 per-user 安装，uv 通过 pip 装在里面 |

## 为什么不能用仓库默认配置

本机的 dify 栈（`dify-db_postgres-1` + `com.docker.backend.exe`）已经占用了 **5432** 和 **8080** 两个端口。所以：

- 不能用 `docker compose up`（`compose.yaml` 里 postgres 写死 `5432:5432`，`agent_service` 写死 `8080:8080`，都会端口冲突）
- 服务和数据库都得改配置，走「本地 Python + 复用 dify 的 PG」这条路

## 完整启动步骤

### 1. 升级 uv 到 0.12.5

```sh
python -m pip install --upgrade "uv==0.12.5"
uv --version   # 期望：uv 0.12.5
```

### 2. 在 dify 的 Postgres 里建库

```sh
docker exec dify-db_postgres-1 psql -U postgres -c "CREATE DATABASE agent_service;"
```

表结构不用管，服务启动时 langgraph 的 `setup()` 会自动建（见下）。

### 3. 配置 `.env`

`.env` 是 gitignored 的，不会被提交。关键几项：

```ini
# 端口：8080 被 dify 占了，改用 8081
HOST=0.0.0.0
PORT=8081

# 服务地址：必须显式设置
# 不设的话 Streamlit 会用 http://{HOST}:{PORT} 拼出 http://0.0.0.0:8081，Windows 上可能连不通
AGENT_URL=http://localhost:8081

# 数据库：复用 dify 实例的独立库
DATABASE_TYPE=postgres
POSTGRES_USER=postgres
POSTGRES_PASSWORD=<dify 的 Postgres 密码>
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=agent_service

# 模型：只配 DeepSeek，DEFAULT_MODEL 留空会自动落到 deepseek-v4-flash
DEEPSEEK_API_KEY=<你的 key>
USE_FAKE_MODEL=false
```

> `AUTH_SECRET` 目前是空的，服务**没有任何鉴权**，且 `HOST=0.0.0.0` 监听所有网卡。本地开发无所谓，但别在公共网络上开着。要长期跑就设上，客户端会要求 bearer token。

### 4. 装依赖

```sh
uv sync --frozen
```

`--frozen` 是必须的：严格按照 `uv.lock` 安装（251 个包），不做任何重新解析。**不要**用裸 `uv sync`，见下文「坑 1」。

### 5. 起服务（两个终端）

```sh
# 终端 A：Agent 服务
uv run python src/run_service.py

# 终端 B：Streamlit UI
uv run streamlit run src/streamlit_app.py
```

Windows 下激活虚拟环境是 `.venv\Scripts\activate`，不是 README 里写的 `source .venv/bin/activate`。

想调试时看详细日志，加 `LOG_LEVEL=INFO` 前缀，例如 `LOG_LEVEL=INFO uv run python src/run_service.py`。

## 坑

### 坑 1：uv 版本太老会悄悄丢掉供应链冷却策略

仓库 `pyproject.toml` 里有：

```toml
[tool.uv]
exclude-newer = "14 days"
```

这是供应链冷却——只解析发布满 14 天的版本。**uv 0.8.12 解析不了这种相对时间写法**，会报 TOML 解析错误，然后「ignoring existing lockfile due to removal of timestamp cutoff」，把锁文件里的冷却信息丢掉并重新解析。

用 `uv sync --frozen` 时它是安全的（锁文件原样使用），但只要不加 `--frozen` 跑一次 `uv sync` 或 `uv add xxx`，`uv.lock` 就会被降级覆盖。所以本机升到了 0.12.5。

### 坑 2：Windows 上 psycopg 连不上（uvicorn 行为变更）

启动后日志报：

```text
WARNING:psycopg.pool:error connecting in 'pool-1': Psycopg cannot use the 'ProactorEventLoop'
to run in async mode.
```

`src/run_service.py` 原本用 `asyncio.set_event_loop_policy(WindowsSelectorEventLoopPolicy())` 处理这个，但**在 uvicorn ≥ 0.36 上这已经是死代码**：

- `uvicorn/loops/asyncio.py` 现在直接返回 `asyncio.ProactorEventLoop`（win32 且非 subprocess 时）
- `uvicorn/server.py` 用 `asyncio.Runner(loop_factory=...)` 建循环，**完全绕过 event loop policy**
- 顺带一提，`set_event_loop_policy` 在 Python 3.14 上已废弃，而仓库声明支持到 3.14

修法（已应用在 `src/run_service.py`，**这是未提交的工作区改动**）：

```python
loop = "asyncio:SelectorEventLoop" if sys.platform == "win32" else "auto"
uvicorn.run(..., loop=loop)
```

自定义循环只能传点号路径字符串——uvicorn 的 `loop` 参数走 `import_from_string()`，传 callable 不生效。

> 这一条值得往上游提 issue/PR，影响所有 Windows + Postgres 用户。如果哪天 `git checkout src/run_service.py` 还原了文件，这个坑会复现。

### 坑 3：`.env` 里行内注释会被当成值

`python-dotenv` **不会**剥离行内注释。从 `.env.example` 直接拷过来的这两行：

```ini
VOICE_STT_PROVIDER=        # Speech-to-text provider (only 'openai' supported currently)
```

实际解析出来的值是 `# Speech-to-text provider (only 'openai' supported currently)`，不是空字符串。不会崩（`src/voice/stt.py` 捕获异常后禁用语音），但每次起 Streamlit 都会打一条 error 日志。语音功能需要 `OPENAI_API_KEY`，本机没配，所以清空这两行即可。

**结论：`.env` 里只在整行注释，不要在值后面写 `#`。**

### 坑 4：Git Bash 里 curl 传中文会乱码

`curl -d '{"message":"中文"}'` 在 Git Bash 里会按 GBK 编码传出去，服务端报 `There was an error parsing the body`。

解法：把 JSON 写进文件（确保 UTF-8），用 `--data-binary @file` 传。

### 坑 5：`/threads` 的 `agent_id` 有默认值

`GET /threads` 的 `agent_id` 默认是 `DEFAULT_AGENT`（即 `research-assistant`），不是「所有 agent」。用别的 agent 聊完去查列表，要显式带上：

```sh
curl "http://localhost:8081/threads?user_id=<uid>&agent_id=chatbot"
```

### 坑 6：联网搜索（research-assistant）在国内网络下不可用

`research-assistant` 的 `WebSearch` 工具原本用的是 `langchain_community` 的 `DuckDuckGoSearchResults`，它内部写死了 `DDGS()`——**既不吃代理配置、也不接受超时参数**。而这套环境有两个叠加问题：

1. ddgs 的 7 个可用文本引擎（brave / duckduckgo / grokipedia / mojeek / startpage / wikipedia / yahoo）**全部在墙外**
2. 走代理后每个连接要 3–7 秒，ddgs 的默认超时太短，引擎还没返回就被掐断

表现是 `research-assistant` 每次必失败，报 `TimeoutException ... operation timed out`。

**实测数据**（中文查询词，各 3 次）：

| 配置 | 成功率 | 耗时 |
| --- | --- | --- |
| 默认超时 | 0/3 | — |
| 30s 超时、不设代理 | 2/3 | 57.9s / 失败 / 4.6s |
| **30s 超时 + 走代理** | **3/3** | **2.5–3.4s** |

**解法**：自己写工具、显式传 30 秒超时（`langchain_community` 的 wrapper 传不进去），代理由 `.env` 里的 `HTTP_PROXY`/`HTTPS_PROXY` 提供（`primp` 认环境变量）。但光有这一半还不够——见坑 7。

> `SEARCH_TIMEOUT = 30` 不是随手填的：30 秒是这套网络下的甜点值。代理节点慢的时候（实测 github 走代理要 12.8s）余量仍然够。

### 坑 7：工具抛异常会直接杀死整个 run（连带污染 thread）

这是坑 6 更深一层的根因，也是**最阴的一个**。langgraph 的 `ToolNode` 默认错误处理器是：

```python
def _default_handle_tool_errors(e: Exception) -> str:
    if isinstance(e, ToolInvocationError):
        return e.message
    raise e        # ← 其它异常一律往上抛
```

**它只吞「工具参数校验失败」，工具体内抛出的任何异常都会让整个 run 挂掉。** 而 `ddgs` 在**超时**和**搜不到结果**两种情况下都是抛异常，不是返回空列表。

连环后果：

```text
工具体抛异常 → run 死  → 检查点里留下「有 tool_calls、没有 ToolMessage」的残缺状态
             → 之后每次请求都把这段坏历史发给 DeepSeek
             → 400 "An assistant message with 'tool_calls' must be followed by tool messages"
             → 这个 thread 永久性报废（换新 thread 才能继续）
```

**所以工具函数必须永不抛异常**，失败要以**文本形式**返回给模型（让模型自己决定重试还是据实回答）。已实现：

```python
# src/agents/tools.py
try:
    results = DDGS(timeout=SEARCH_TIMEOUT).text(query, max_results=5)
except Exception as e:
    logger.warning(f"web search failed for {query!r}: {e}")
    return f"Search failed: {e}"
```

> `src/agents/tools.py` 里的 `calculator_func` 有**同一个隐患**：表达式非法时它 `raise ValueError`，同样会杀死 run。尚未处理。

**修复后实测**：中文查询（北京限行/天气）端到端 `POST /research-assistant/stream` → 200 OK / 14 秒，包含 2 轮共 4 次工具调用 + 流式输出，回答内容正确。

### 坑 8：`interrupt-agent` 的结构化输出在 DeepSeek 上不可用

`src/agents/interrupt_agent.py` 用 `with_structured_output()` 把对话里的生日解析成 `{birthdate, reasoning}`，而 langchain-openai 默认发 `response_format={"type":"json_schema"}`——**DeepSeek 不支持**。四种方法实测：

| 方法 | DeepSeek 反应 |
| --- | --- |
| 默认 / `json_schema` | ❌ `This response_format type is unavailable now` |
| `function_calling` | ❌ `Thinking mode does not support this tool_choice`（flash 是推理模型，思考模式下禁止强制 tool_choice） |
| `json_mode` | ✅ 可用，但**要求 prompt 里出现 "json" 字样** |

**解法**：`method="json_mode"` + 在 prompt 里写清 JSON 形状。但 `json_mode` **对 Anthropic 不合法**（类型和运行时都不接受），所以代码里按 provider 分流——只有 `model_name in DeepseekModelName` 时才用 json_mode，其他 provider 走默认。

两个实现细节的坑：

1. prompt 是 **f-string 模板**（`SystemMessagePromptTemplate.from_template`），JSON 示例里的花括号必须写成 `{{ }}`，否则 `.format()` 会把它当占位符。
2. `with_structured_output(method="json_mode")` 会触发 pyrefly 类型错误（基础接口的 `method` 只声明了 `function_calling`/`json_schema`），需要 `# type: ignore[bad-argument-type]`。

**实测**：两轮 `POST /interrupt-agent/invoke`（第一轮不提供生日 → 正确触发 `interrupt()` 询问；第二轮提供生日 → resume 并完成）均 200，且生日被写进长期记忆 `store` 表。

### 坏 thread 怎么识别和清理

看到 400 且错误信息含 `insufficient tool messages following tool_calls message`，就是这个病。清理：

```sql
DELETE FROM checkpoint_writes WHERE thread_id='<坏掉的 thread_id>';
DELETE FROM checkpoint_blobs  WHERE thread_id='<坏掉的 thread_id>';
DELETE FROM checkpoints       WHERE thread_id='<坏掉的 thread_id>';
```

## 数据库

服务于 2026-09-30 首次启动时自动建表：

| 表 | 用途 |
| --- | --- |
| `checkpoints` / `checkpoint_blobs` / `checkpoint_writes` | 短期记忆（对话状态） |
| `checkpoint_migrations` | 上述表的 migration 记录（当前 10 条） |
| `store` / `store_migrations` | 长期记忆（当前 4 条 migration） |

一些查询：

```sh
# 看表
docker exec dify-db_postgres-1 psql -U postgres -d agent_service -c "\dt"

# 看某个 thread 落了多少数据
docker exec dify-db_postgres-1 psql -U postgres -d agent_service -tAc \
  "SELECT count(*) FROM checkpoints WHERE thread_id='<uuid>'"
```

**注意**：这些表在 dify 的数据卷里。dify 那边如果 `docker compose down -v` 重建卷，聊天记录会一起丢掉。介意的话可以改用独立容器（映射到 5433），改 `.env` 两行即可。

另外 `scripts/smoke_test.sh:132` 硬编码了 `POSTGRES_PORT=5432` 和密码 `postgres`，和本机这套配置对不上，它的 postgres 分支跑不了（不影响服务本身）。

## 验证命令

```sh
# 服务信息（agent 列表 + 可用模型）
curl -s http://localhost:8081/info

# 健康检查
curl -s http://localhost:8081/health

# 真实调用一个 agent（把 JSON 写进文件避免中文乱码）
cat > /tmp/payload.json <<'EOF'
{"message":"用一句中文自我介绍，不超过20个字。","thread_id":"7f3a9c1e-2b4d-4e6f-8a91-0c5d7e2f4a6b","user_id":"me"}
EOF
curl -s -X POST http://localhost:8081/chatbot/invoke \
  -H "Content-Type: application/json; charset=utf-8" \
  --data-binary @/tmp/payload.json

# 按 thread 取对话历史
curl -s -X POST http://localhost:8081/history \
  -H "Content-Type: application/json" \
  -d '{"thread_id":"7f3a9c1e-2b4d-4e6f-8a91-0c5d7e2f4a6b"}'

# 列出某用户在某 agent 下的所有会话
curl -s "http://localhost:8081/threads?user_id=me&agent_id=chatbot"
```

可用 agent：`/info` 里的 `agents` 字段，共 10 个。默认 `research-assistant`（带联网搜索和计算器，调用更慢更贵）；想快速验证用 `chatbot`（单次 LLM 调用）。

`github-mcp-agent` 因为没配 `GITHUB_PAT`，启动时会打一条 warn，该 agent 无工具可用——符合预期。

## 已知问题（上游既有，未处理）

`POST /history` 查询**不存在的 thread** 会返回 500：

```text
ERROR:service.service:An exception occurred: 'messages'
```

`src/service/service.py:442` 直接 `state_snapshot.values["messages"]` 下标取值，空 state 就 KeyError。真实存在的 thread 一切正常。用 `.get("messages", [])` 或返回 404 更合理。

## 待办

- [ ] 把 `run_service.py` 的 Windows 事件循环修复提到上游（issue 或 PR）
- [ ] 修掉 `/history` 对空 thread 的 500
- [ ] `calculator_func` 的同类隐患：非法表达式会 `raise ValueError` 杀死 run（见坑 7），应改成返回提示文本
- [ ] 坏 thread：`4a04e978-…`（旧代码遗留）、`89a6e0b0-…`（坑 7 触发）需要清理
- [ ] 清空 `.env` 里两行带行内注释的 `VOICE_*_PROVIDER`
- [ ] 决定 `AUTH_SECRET` 是否要设（当前服务裸奔）
- [ ] 测试数据 thread `7f3a9c1e-2b4d-4e6f-8a91-0c5d7e2f4a6b` / user `startup-check` 可以删
