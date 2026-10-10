# 抓包与流量分析方案

记录如何观察这个项目发出去的流量。整理于 2026-10-04。

## 先分清抓哪一层

项目的流量分五块，每块适合的工具完全不同：

| # | 链路 | 协议 | 说明 |
| --- | --- | --- | --- |
| 1 | Streamlit:8501 → Agent 服务:8081 | 明文 HTTP + SSE | **浏览器 devtools 和 mitmproxy 都看不到**，见下节 |
| 2 | Agent 服务 → **DeepSeek** | HTTPS 出站 | 多数人真正想抓的就是这个 |
| 3 | Agent 服务 → ddgs 搜索 | HTTPS 出站，**走 Clash 代理** | 并发十几个搜索引擎 |
| 4 | Agent 服务 → GitHub MCP (`api.githubcopilot.com`) | HTTPS 出站 | |
| 5 | Agent 服务 → PostgreSQL:5432 | PG 协议，非 HTTP | 要用 `log_statement`，见下文 |

出站流量（2/3/4）由 `.env` 里的 `HTTP_PROXY` / `HTTPS_PROXY` 决定走不走代理，见 [LOCAL_SETUP.md](LOCAL_SETUP.md) 的坑 6。

## 看不到的那一跳：Streamlit → Agent 服务

第 1 条链路是个观测死角，两个原因叠加：

**浏览器 devtools 看不到** —— Streamlit 是服务端框架。浏览器只跟 Streamlit 服务器保持**一条 WebSocket**（`ws://localhost:8501/_stcore/stream`，二进制 protobuf 双向帧），那条 SSE 是 **Streamlit 的 Python 进程**用 httpx 发的（`src/client/client.py:302` 的 `client.stream()` + `aiter_lines()`），消费完再以 Markdown 增量推给浏览器。所以在 Network 面板里**根本不存在**这个请求；WS 面板里能看到的也只有二进制帧，读不出内容。

> 容易过时的知识点：Streamlit 1.59.2 **已经不是 Tornado 了**，服务端换成了 Starlette + uvicorn（响应头 `server: uvicorn`，代码在 `streamlit/web/server/starlette/`）。别照老文章去理解它的服务器。

**mitmproxy 也看不到** —— 本机有个 Windows 用户环境变量（`HKCU\Environment`）：

```text
NO_PROXY = localhost,127.0.0.1,::1
```

httpx 默认 `trust_env=True`，认这个变量，所以 localhost 这一跳**直接绕过 8888**。实测：把 `HTTP_PROXY` 指到一个死端口，`http://localhost:8081/info` 照样返回 200。mitmproxy 里因此只有「服务 → 外网」那几段（DeepSeek / 搜索 / GitHub MCP）。

### 想看到这一跳，用这三个

```sh
# 1. curl 直接怼（-N 关掉 curl 自己的缓冲，中文按 LOCAL_SETUP 坑 4 走文件）
curl -N -X POST http://localhost:8081/chatbot/stream \
  -H "Content-Type: application/json; charset=utf-8" \
  --data-binary @/tmp/payload.json
# 出来的是裸 SSE 帧：data: {"type": "token", "content": "1"} ... data: [DONE]

# 2. AG-UI 客户端（Node，走 /agui，能看事件流）
cd scripts/agui-client && npm install
AGENT_URL=http://localhost:8081 node client.mjs "你好" chatbot

# 3. Langfuse（语义层，比裸帧可读得多）
```

**为什么不让浏览器直连**：服务没有 CORS 中间件（`src/` 里没有 `CORSMiddleware`），跨源会被浏览器拦掉。而且 `/stream` 是 **POST + JSON body**，浏览器原生 `EventSource` 只支持 GET、带不了 body —— 就算 CORS 通了也得用 `fetch()` + `ReadableStream` 手动解析。`docs/AGUI.md` 里也是同样的结论：生产上让 CopilotKit runtime 在服务端转一道。

> 反过来说：如果你在 devtools 里「看到了 SSE」，那多半是别的东西（比如某个直连 8081 的页面），不是 Streamlit 发的。

## 方案① 应用层 tracing（首选）—— 已部署

仓库**原生支持** Langfuse 和 LangSmith，`.env.example` 里就有这些键，默认全注释着。

**优势**：拿到的是**结构化的 agent 调用链**——完整 prompt、模型返回、工具调用、token 数、耗时、嵌套关系。调试 agent 行为比裸报文直观一个数量级，而且不需要任何证书或重启代理。

**本机已部署完成，部署记录与踩到的坑见 [LANGFUSE.md](LANGFUSE.md)**（含目录结构、端口改动、minio 权限坑）。

### Langfuse 自托管（数据不出本机）—— 快速回顾

`scripts/smoke_test.sh:183` 的 `smoke_langfuse()` 里有完整配方：用 `LANGFUSE_INIT_*` 环境变量在首次启动时种子化组织/项目/密钥，**不需要注册账号**。

```sh
cd langfuse-stack && docker compose up -d     # 本机已按此布置，UI 在 :3000
```

`.env` 里的四行（已配好）：

```ini
LANGFUSE_TRACING=true
LANGFUSE_HOST=http://localhost:3000
LANGFUSE_PUBLIC_KEY=pk-lf-local
LANGFUSE_SECRET_KEY=sk-lf-local
```

> **磁盘警告**：`smoke_test.sh:200` 注明首次要拉 **~5 GB** 镜像。C 盘紧张时先确认放哪，或先做 Docker 清理。

### LangSmith 云（需要账号，数据出本机）

**走的是 langsmith 自己的环境变量机制，不是仓库的 settings。** 仓库里 `settings.py:110-115` 声明的 `LANGCHAIN_TRACING_V2` / `LANGCHAIN_API_KEY` / `LANGCHAIN_PROJECT` / `LANGCHAIN_ENDPOINT` **在 `src/` 下没有任何代码引用**，属于死字段。

真正生效的是 `langsmith.utils.get_env_var(namespaces=("LANGSMITH", "LANGCHAIN"))`——**两个前缀都认，`LANGSMITH_` 优先**。所以 `.env.example` 里的 `LANGSMITH_TRACING=true` 是正确写法：

```ini
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=lsv2_...
LANGSMITH_PROJECT=agent-service-toolkit
# LANGSMITH_ENDPOINT 默认就是 https://api.smith.langchain.com，一般不用写
```

`TRACING_V2` 和 `TRACING` 两种后缀都可以（前者优先）。

**与 Langfuse 并存**：可以，机制不同——Langfuse 是仓库代码显式挂 `CallbackHandler`（`service.py:156-160`），LangSmith 是 langchain 的全局 tracer（环境变量启用后自动注入）。两者挂在同一个 run tree 上，各报各的后端。实测两者同时开启：服务 HTTP 200 正常，Langfuse 照常记录 trace。

> **无有效 API key 时不会崩**，只会打两条 WARNING 然后继续（实测）。所以误开这个开关不会把服务搞挂。

#### 坑：LangSmith 在 mitmproxy 链路上会证书校验失败

不同 HTTP 库认的 CA 环境变量不一样：

| 库 | 用它的组件 | 认哪个变量 |
| --- | --- | --- |
| httpx | DeepSeek / OpenAI 调用 | `SSL_CERT_FILE` ✅ |
| requests / urllib3 | **LangSmith 上报** | `SSL_CERT_FILE` ❌，要 `REQUESTS_CA_BUNDLE` |

只设 `SSL_CERT_FILE` 时，LangSmith 会报 `SSLCertVerificationError: unable to get local issuer certificate`。`.env` 里补上：

```ini
REQUESTS_CA_BUNDLE=C:\Users\huoex\.mitmproxy\combined-ca.pem
```

（本机实测：只设前者 → `SSLError`；两者都设 → `HTTP 401` 正常。）

## 方案② mitmproxy（看原始 HTTP 报文）—— 已部署

**插入点现成**——项目本来就通过 Clash 转发流量，把 mitmproxy 串在中间即可，无需改网络配置：

**插入点现成**——项目本来就通过 Clash 转发流量，把 mitmproxy 串在中间即可，无需改网络路由。

### 启动方式

**首选：VS Code 任务**（`.vscode/tasks.json`）：

```text
Ctrl+Shift+P → Tasks: Run Task → mitmweb（启动抓包代理）
```

同一个文件里还有一个 `mitmweb（检查是否在跑）`，只是 `netstat` 查一下 8888 / 8088，不启停任何东西。

想更顺手可以给它绑个快捷键：`Ctrl+K Ctrl+S` → 搜索 `workbench.action.tasks.runTask` → 绑定，然后在 keybindings 里给它加 `"args": "mitmweb（启动抓包代理）"`。

> 任务里的 `command` 用的是 `${env:USERPROFILE}/.local/bin/mitmweb.exe` 绝对路径（`uv tool` 装的 shim）。**这个任务里的 `${env:USERPROFILE}` 是会被替换的**——别和 `vectorExplorer.pythonPath` 搞混，那种扩展自己读的设置不做变量替换。

**备选：仓库根目录的 `start-mitmproxy.cmd`**——双击即可，或从任意终端执行。它多了个端口守卫（8888 被占则提示并退出）。

> 该文件**必须保持纯 ASCII**：cmd.exe 按系统 ANSI 代码页（中文 Windows 是 GBK）解析 `.cmd`，UTF-8 的中文会变成乱码命令。所以说明文字都在本文档里，不在脚本里。
>
> ⚠️ 任务和 `.cmd` 的参数是**两份**。改端口/参数记得同步，或者只留其中一个。

手动启动（等价）：

```sh
export PATH="$USERPROFILE/.local/bin:$PATH"   # uv tool 装的命令在用户目录下
mitmweb --mode upstream:http://127.0.0.1:7890 \
        --listen-host 127.0.0.1 --listen-port 8888 \
        --web-host 127.0.0.1 --web-port 8088 \
        --web-open-browser \
        -w "$USERPROFILE/.mitmproxy/flows.mitm"
```

| 参数 | 为什么这么写 |
| --- | --- |
| `--mode upstream:…7890` | 串联到 Clash，而不是替换它（部分站点仍需 Clash 的规则路由） |
| `--listen-host 127.0.0.1` | **默认绑 `0.0.0.0`，等于在局域网上开一个开放代理**，必须收紧 |
| `--listen-port 8888` | 8080 被 dify 占、8081 是 agent 服务、8501 是 Streamlit |
| `--web-port 8088` | mitmweb 的 web 端口默认就是 8081，会跟 agent 服务撞 |
| `--web-open-browser` | mitmweb 的 UI **需要带 token 的 URL** 才能调它的 API（否则 `/flows` 返回 403），只有让它自己开浏览器才能拿到 token |
| `-w flows.mitm` | 落盘一份，可用 `mitmdump -nr` 事后离线读；也方便用文件大小确认抓没抓到 |

### 证书：用合并包，不要只用 mitmproxy 的 CA

```sh
cat .venv/Lib/site-packages/certifi/cacert.pem ~/.mitmproxy/mitmproxy-ca-cert.pem \
  > ~/.mitmproxy/combined-ca.pem
```

`SSL_CERT_FILE` 会**替换**默认证书包。如果只放 mitmproxy 的 CA，那些绕过代理的直连（`NO_PROXY` 命中、TCP 直连）就没法完成 TLS 校验了。合并后两边都正常（本机实测 122 张证书）。

### `.env` 改动

```ini
HTTP_PROXY=http://127.0.0.1:8888
HTTPS_PROXY=http://127.0.0.1:8888
SSL_CERT_FILE=C:\Users\huoex\.mitmproxy\combined-ca.pem
```

`load_dotenv()` 在 `run_service.py:8` 执行，早于任何连接建立，所以 `SSL_CERT_FILE` 来得及生效。

### 验证确实抓到了

```sh
mitmdump -nr "$USERPROFILE/.mitmproxy/flows.mitm" | tail
```

本机实测输出：

```text
POST https://api.deepseek.com/chat/completions      << 200 OK 33.6k
POST https://api.githubcopilot.com/mcp/             << 200 OK 124k
GET  https://api.githubcopilot.com/mcp/             << 405 Method Not Allowed
DELETE https://api.githubcopilot.com/mcp/           << 204 No Content
```

一次 `chatbot` 调用让 flows 文件从 8.2 KB 涨到 245 KB。

### 读单条 flow 的精确时间戳

`mitmdump -nr` 只给一行摘要，排查耗时问题需要每段的毫秒时间。用 mitmproxy 自带的 Python 直接读 dump（不用管 UI 的 403，那个 token 机制只挡 HTTP API，不挡文件）：

```sh
MP_PY="$USERPROFILE/AppData/Roaming/uv/tools/mitmproxy/Scripts/python.exe"
"$MP_PY" -c "
from mitmproxy.io import FlowReader
import datetime
with open(r'C:\Users\huoex\.mitmproxy\flows.mitm', 'rb') as f:
    for fl in FlowReader(f).stream():
        r, resp = fl.request, fl.response
        iso = lambda t: datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime('%H:%M:%S.%f')[:-3]
        print(f'{iso(r.timestamp_start)} {r.method} {r.pretty_host}{r.path[:40]} resp_end={iso(resp.timestamp_end)}')
"
```

`req_start / req_end / resp_start / resp_end` 四个点，比「总耗时」有用得多。

**对 SSE 来说 `resp_end` 可信**：它是整条响应体（含 `data: [DONE]`）收完的时刻，不是响应头到达的时刻。验证方法：拿它和 Langfuse 上同一个 `ChatOpenAI` span 的耗时对——正常时两者差几十毫秒（实测 0.9 s / 1.14 s 这种量级对得上）。**对不上就说明问题不在网络上**，见 [LOCAL_SETUP.md](LOCAL_SETUP.md) 的「未解」一节。

### 坑

1. **`primp` 这次没问题**（推翻了原先的猜测）：本机实测 ddgs 搜索穿过 mitmproxy 正常返回，DeepSeek 的 httpx 也正常。`SSL_CERT_FILE` 这条路对两者都有效。
2. **停 mitmproxy 前必须把 `.env` 的代理改回 7890** —— 否则服务完全出不了网（DeepSeek、搜索、GitHub 全断）。
3. **`flows.mitm` 含敏感数据**：GitHub PAT 明文在 `Authorization` 头里、所有 LLM prompt 明文。别外传，用完可删。
4. **UI 的 `/flows.json` 返回 403 是正常的** —— mitmweb 的 API 需要 token。用 `--web-open-browser` 走浏览器，或用 `mitmdump -nr` 读文件。

5. **`.env` 里的代理会被进程环境里已有的同名变量悄悄覆盖** —— 最难查的一个。

   `load_dotenv()` 默认**不覆盖已存在的环境变量**（`override=False`）。VS Code 的调试会话会往进程环境里注入 `HTTP_PROXY`（按系统代理，指向 Clash 的 7890），于是 `.env` 里改的 8888 永远进不去，流量直接打到 Clash，**mitmproxy 一个包都抓不到**。

   迷惑之处在于：服务日志一切正常、**Langfuse 也照样有 trace**（说明 `.env` 确实被加载了），只有 flows 文件不增长。

   诊断方法——一边发请求一边采样该进程的连接：

   ```sh
   PID=$(netstat -ano | grep ':8081.*LISTENING' | awk '{print $NF}')
   # 循环采样 netstat 过滤该 PID，看它连 8888 还是 7890
   ```

   本机实测：`.env` 写 8888，服务却稳定连 7890，即为此坑。

   **修法**：`src/run_service.py` 改成

   ```python
   load_dotenv(override=True)
   ```

   让 `.env` 成为权威来源。已改，修复后实测 flows 单次请求增长 691 KB。

   > 代价：这样进程环境变量就**不能**再覆盖 `.env` 了（比如 `PORT=9000 uv run ...` 会失效）。对这个以 `.env` 为配置源的项目是合理的取舍。

### 与 Langfuse 的分工

| | Langfuse | mitmproxy |
| --- | --- | --- |
| 层次 | **语义层**：agent 节点、工具调用、token 数、耗时 | **字节层**：原始 HTTP 报文、header、SSE 分块 |
| LLM 内容 | 解析后的 messages | **发出去的原始 JSON**（含 `tools`、`stream`、`temperature` 等 provider 专有字段） |
| 覆盖范围 | 只有 agent 内部 | 所有出站流量（含 GitHub MCP、ddgs 搜索） |

两者可同时开启，互不干扰。**Langfuse 看「agent 做了什么」，mitmproxy 看「网线上跑了什么」。**

### 精确关联：一条 generation ↔ 一个 flow

2026-10-10 起，DeepSeek 每次调用的 `x-ds-trace-id` 会记进 Langfuse，两边可以 1:1 对上，不用再靠时间戳猜：

```text
Langfuse 点开可疑的 generation → Metadata 面板 → x-ds-trace-id → 复制
   → mitmweb 过滤框输入  ~hs"<那个id>"   → 唯一命中这次请求（实测 238 条 flow 里命中 1 条）
```

实现分三处：`src/core/provider_headers.py`（白名单 + 裁剪回调）、`src/core/llm.py` 的 DeepSeek 分支（`include_response_headers=True`）、`src/service/tracing.py`（Langfuse handler 子类，在 `on_llm_end` 里把 id 写进 generation 的 metadata）。

几个注意点：

- **结构化输出拿不到这个 id**：`interrupt-agent` 的生日提取用 `with_structured_output(method="json_mode")`，而 langchain-openai 在请求带 `response_format` 时**直接跳过**响应头捕获（会在日志里打一条 UserWarning）。这类调用没有 trace id，属已知边界。
- 捕获到的响应头有 13 个（~700 B），回调只保留 trace id 一个键；否则它会随每条 AI 消息进 graph state 和检查点。trace id 同时会出现在 SSE 的 `message` 帧里（`service/utils.py:58` 复制 `response_metadata`）。
- 裁剪的白名单在 `PROVIDER_TRACE_HEADERS`（目前只有 DeepSeek）；换 provider 往那里加一行、并在 `src/core/llm.py` 对应分支开 `include_response_headers=True` 即可。

## 方案③ Wireshark + SSLKEYLOGFILE（真·抓包）

本机**已装 Wireshark**（`C:\Program Files\Wireshark`，只是不在 Git Bash 的 PATH 里）。Python 的 `ssl` 模块原生支持 keylog：

```sh
SSLKEYLOGFILE=D:\sslkeylog.txt uv run python src/run_service.py
```

Wireshark → Preferences → Protocols → TLS → `(Pre)-Master-Secret log filename` 填上面那个路径，之后自动解密 TLS。

`primp`（ddgs 底层）是否支持 keylog **未验证**——它是 Rust 客户端，keylog 支持情况未知。（但它对自定义 CA 是认的，见方案②的坑 1。）

**适用场景**：TCP 层问题——超时、连接复用、DNS、代理链路、重传。分析 agent 行为反而绕远。

## 方案④ 零成本土办法

| 手段 | 做法 | 能看到 |
| --- | --- | --- |
| httpx 日志 | 已有，`LOG_LEVEL=INFO` 即可 | 请求 URL，**没有 body** |
| **LangChain debug** | 代码里加一行 `langchain.debug = True` | **完整 prompt + 返回**，打到 stdout |
| PostgreSQL | `ALTER SYSTEM SET log_statement='all'` 后 `SELECT pg_reload_conf()` | SQL 明文 |

`langchain.debug = True` 是**性价比最高的一个**：一行代码、不装任何东西，所有 LLM 输入输出全打到控制台。

> LangChain 的 debug **没有环境变量**——`langchain_core/globals.py` 里只有 `set_debug()`，只能从代码里开。

## 方案⑤ 日志关联 id

2026-10-10 加入。不用抓包工具，日志行自带 `[run_id/thread_id]`——终端里先把范围缩到某一次请求：

```text
# chatbot 一次调用（实测）
16:59:40 INFO [01a1250a-…/final-demo] service.service:151: Run started: agent=chatbot
16:59:43 INFO [01a1250a-…/final-demo] service.utils:41: LLM call: deepseek-flash 34→181 tokens
16:59:43 INFO [01a1250a-…/final-demo] service.service:242: Run finished in 2.9s

# research-assistant（节点时序 + 每次 LLM 调用，各一行）
17:16:37 INFO [01a12518-…/node-filter-check] service.utils:75: Node: guard_input (0.00s)
17:16:38 INFO [01a12518-…/node-filter-check] service.utils:43: LLM call: deepseek-flash 628→95 tokens
17:16:38 INFO [01a12518-…/node-filter-check] service.utils:75: Node: model (0.88s)
17:16:38 INFO [01a12518-…/node-filter-check] service.utils:75: Node: tools (0.00s)
17:16:38 INFO [01a12518-…/node-filter-check] service.utils:43: LLM call: deepseek-flash 736→2 tokens
17:16:38 INFO [01a12518-…/node-filter-check] service.utils:75: Node: model (0.60s)
```

（`service.service:339` 这样的行号就是打日志那行的位置，出问题直接跳过去；库打的日志行号会指向 site-packages，属正常。）

**和 Langfuse 的对应关系**（同一批 callback 事件的两个消费者）：

| 日志行 | Langfuse 里的 |
| --- | --- |
| `Node: model (0.88s)` | `CHAIN model` |
| `LLM call: … tokens` | `GENERATION ChatOpenAI` |
| `Tool call: WebSearch(...)` | `TOOL WebSearch` |

**Langfuse 里多出来的 `check_safety` / `pending_tool_calls` 是条件边函数**（元数据带 `seq:step:*` 标签，不是 `graph:step:*`），不是图节点，日志里**故意不打**——每条消息多两行、信息量低（想加的话在对应 agent 的边函数里加一行 logger 即可）。判定「谁是节点」的条件：只有节点自己的 run，名字才等于元数据里的 `langgraph_node`。

- 一次 run 在终端能看到：起止 + 总耗时、每次 LLM 调用（模型 + token 数）、每次工具调用（名字 + 参数前 160 字符）。
- **`LLM call` 这行是自己打的**（`src/service/utils.py` 的 `LogLLMCalls`）。**langfuse 在 import 时会把 `httpx` logger 压到 WARNING**（`langfuse/logger.py:22`，"to reduce noise"），所以服务里拿不到 httpx 自己的 `HTTP Request: POST …` 行——别去 `.env` 或 logging 配置里找，原因是这个。
- 出现 `[-/-]` 的两种情况：① 启动阶段（还没请求）；② **线程池里跑的库**——ddgs 的搜索走 `primp:`，那些行没有 id，因为 contextvars 不跨线程传播（Java 的 MDC 在线程池里同样是这个毛病，要靠 TaskDecorator 之类手工传）。uvicorn 自己的 `INFO: ...` 行则是另一个原因：它有自己的 logger 和格式。
- LogRecord 上其实有三个字段——`run_id` / `thread_id` / `user_id`，默认格式只打前两个，要 user 就改 `LOG_FORMAT`。

**机制**（`src/core/log_context.py`）：Python 里 MDC 的对应物是 **contextvars**（一个线程跑多个协程，ThreadLocal 会串数据）+ 一个 `logging.Filter`。请求入口 bind（`service.py` 的 `_handle_input`、`agui.py` 的 `_base_config`），`run_service.py` 调 `configure_logging()` 装格式。contextvars 是任务级的，请求之间不会互相污染。

**和 Java 那套的分层对照**（两层别混为一谈）：

| 层 | Java 世界 | Python 世界 | 本仓库 |
| --- | --- | --- | --- |
| 应用层 id | MDC + logback `%X{traceId}` | `contextvars` + `logging.Filter` | ✅ 就是这个 |
| OTel 层 id | Micrometer/Sleuth **自动**把 traceId/spanId 塞进 MDC | `opentelemetry-instrumentation-logging` 把 `otelTraceID`/`otelSpanID` 塞进 LogRecord | ❌ 没做 |
| 后端的串联 | logback/Grafana 之类把它们串起来 | — | 用 `run_id` 串 |

为什么没做 OTel 层：本仓库的 OTel span 上下文**只在 langfuse 的回调窗口内才 attach**（回调外 `trace.get_current_span()` 是空的，实测过），自动注入会时有时无；而 `run_id` 全程有效，还能直接对上 checkpoint 和 LangSmith。要做那层得引 `opentelemetry-instrumentation-logging` 并自建 tracer provider，属另一件事。

**怎么开**：日志级别默认 `WARNING`（`src/core/settings.py:79`），两条 INFO 不会出来。

- VS Code：「Agent Service」启动配置（`.vscode/launch.json`，untracked）里已经带 `"env": { "LOG_LEVEL": "INFO" }`，F5 就有。
- 终端：`LOG_LEVEL=INFO uv run python src/run_service.py`。

> **别把 `LOG_LEVEL` 写进 `.env`**：`run_service.py` 用 `load_dotenv(override=True)`，`.env` 里的值会**盖掉 launch.json**（坑 5 的代价），「调试用 INFO、平时 WARNING」的按需切换就没了。

**四层怎么互相串**：

| 从哪 → 到哪 | 用什么 |
| --- | --- |
| 日志行 → Langfuse | 复制 `run_id`，在 Langfuse 按 metadata `run_id` 过滤（trace metadata 现在带这个键；Langfuse 自己的 trace id 是另一串，对不上） |
| 日志行 → 对话状态 | `thread_id` 查 checkpoints（见 [LOCAL_SETUP.md](LOCAL_SETUP.md) 的数据库一节） |
| Langfuse → 原始报文 | generation 的 `x-ds-trace-id`（见上一节） |

## 怎么选

| 目的 | 用哪个 |
| --- | --- |
| **调试 agent 行为**（prompt 对不对、工具调没调、为什么报错） | **① Langfuse** |
| 想先零成本看一眼 | **④ `langchain.debug = True`** |
| 看原始 HTTP 报文、验证请求体 | **② mitmproxy** |
| 网络层问题（超时、连接、代理） | **③ Wireshark** |
| 先定位「哪次请求」（不装任何工具） | **⑤ 日志关联 id**（已内建，`docker logs`/终端里直接看） |

**建议路径**：**① 和 ② 本机都已部署好，直接开箱用**——Langfuse 看语义，mitmproxy 看报文。只想临时瞟一眼时再用 ④。

> 注意 ① 和 ② 不冲突也不重叠：Langfuse 在 agent 内部打点（服务端），mitmproxy 在网线上拦（进程外）。两个都开会得到互补的两份视图。

## 本机现状（2026-10-04）

| 项 | 状态 |
| --- | --- |
| **Langfuse** | ✅ 已部署运行，UI <http://localhost:3000>，见 [LANGFUSE.md](LANGFUSE.md) |
| **mitmproxy** | ✅ 已装（12.2.3，`uv tool`），代理 `:8888`、UI `:8088`，flows 落盘 `~/.mitmproxy/flows.mitm` |
| **当前出站链路** | `服务 → mitmproxy:8888 → Clash:7890 → 外网`（`.env` 的代理已指向 8888） |
| 证书 | `~/.mitmproxy/combined-ca.pem`（certifi 121 张 + mitmproxy CA），由 `.env` 的 `SSL_CERT_FILE` 指定 |
| Wireshark | ✅ 已装，`C:\Program Files\Wireshark`（不在 Git Bash 的 PATH 里） |
| Clash 代理 | `127.0.0.1:7890`（mitmproxy 的 upstream 指着它） |
| C 盘可用空间 | 31 GB（Langfuse 镜像已占约 4.9 GB） |

### 退出 mitmproxy 时务必还原

```ini
HTTP_PROXY=http://127.0.0.1:7890
HTTPS_PROXY=http://127.0.0.1:7890
# SSL_CERT_FILE 可以留着，不影响直连（合并包里含完整公共 CA）
```
