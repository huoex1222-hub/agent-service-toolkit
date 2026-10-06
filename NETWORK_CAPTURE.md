# 抓包与流量分析方案

记录如何观察这个项目发出去的流量。整理于 2026-10-04。

## 先分清抓哪一层

项目的流量分五块，每块适合的工具完全不同：

| # | 链路 | 协议 | 说明 |
| --- | --- | --- | --- |
| 1 | Streamlit:8501 → Agent 服务:8081 | 明文 HTTP | 最简单，curl / 浏览器 devtools 就能看 |
| 2 | Agent 服务 → **DeepSeek** | HTTPS 出站 | 多数人真正想抓的就是这个 |
| 3 | Agent 服务 → ddgs 搜索 | HTTPS 出站，**走 Clash 代理** | 并发十几个搜索引擎 |
| 4 | Agent 服务 → GitHub MCP (`api.githubcopilot.com`) | HTTPS 出站 | |
| 5 | Agent 服务 → PostgreSQL:5432 | PG 协议，非 HTTP | 要用 `log_statement`，见下文 |

出站流量（2/3/4）由 `.env` 里的 `HTTP_PROXY` / `HTTPS_PROXY` 决定走不走代理，见 [LOCAL_SETUP.md](LOCAL_SETUP.md) 的坑 6。

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

## 怎么选

| 目的 | 用哪个 |
| --- | --- |
| **调试 agent 行为**（prompt 对不对、工具调没调、为什么报错） | **① Langfuse** |
| 想先零成本看一眼 | **④ `langchain.debug = True`** |
| 看原始 HTTP 报文、验证请求体 | **② mitmproxy** |
| 网络层问题（超时、连接、代理） | **③ Wireshark** |

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
