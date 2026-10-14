# Langfuse 自托管部署记录

本地跑一套 Langfuse 做 tracing，用于观察 agent 的完整调用链（prompt、模型返回、工具调用、耗时、token 数）。

这是 [NETWORK_CAPTURE.md](NETWORK_CAPTURE.md) 里的「方案①」，部署过程与踩到的坑记在这里。部署于 2026-10-04。

## CPU 排查：ClickHouse 系统日志 + MinIO 探活（2026-10-12）

**症状**：`docker stats` 里 `langfuse-clickhouse-1` 老在 4–20% 之间跳，`langfuse-minio-1` 稳定 4–7%。

**排查**（都实测过）：

- 全容器数值排序：只有 clickhouse（4–20%）、minio（4–7%）、dify 的 postgres（2%）有量，其余 <1%。16 核机器整机负载 14–22%，Docker 全部加起来 ≈ 整机 1%。
- 进 ClickHouse 看 `system.parts`：**真实数据只有 157 条 trace**，而 `system.asynchronous_metric_log` 有 **1.94 亿行**、`text_log` 545 万、`trace_log` 724 万 —— 这些是 ClickHouse **自己的系统日志**，Langfuse 一个字节都不读。
- `system.merges` 正忙的就是它；`system.query_log` 显示 5 分钟里查询总耗时几十毫秒 → **20% 的尖峰 = 这些日志持续写入 → 小 part → 后台 merge 爆发**。

**修法**：新增 `clickhouse-system-logs.xml`，挂到容器 `/etc/clickhouse-server/config.d/`（compose 里加一行 volume），用 `remove="1"` 关掉八个用不到的日志：`asynchronous_metric_log`、`metric_log`、`text_log`、`trace_log`、`processors_profile_log`、`opentelemetry_span_log`、`part_log`、`background_schedule_pool_log`。`query_log` 保留（小、排查有用）。

生效方式：**`docker compose restart clickhouse`** —— 纯配置文件改动用 `restart` 就够；只有 compose 里的端口/挂载变更才需要 `up -d`（见下节）。

**效果（实测）**：**尖峰消失**（`system.merges` 归零，`text_log`/`asynchronous_metric_log` 的行数 8 秒内纹丝不动）。但**稳态 4–5% 一分没降** —— 把它自己的计数器拉出来对质：20 秒内查询 CPU 只用了 **1.4 毫秒**，而 cgroup 记了 **0.91 秒**，说明那 4–5% 全是 ClickHouse 的**后台线程开销**（跟系统日志无关）。换算：单核 4–5% ≈ 整机 0.3%，不值得再调。

### MinIO：1 秒一次的探活

同一轮排查里更划算的一刀。compose 里 minio 的 healthcheck 是：

```yaml
      test: ["CMD", "mc", "ready", "local"]
      interval: 1s        # ← 元凶
```

`mc` 是个 Go 二进制，**每启动一次 27 毫秒 CPU**（实测：连跑 20 次用 553 ms），而它**每秒跑一次** → 恒定 **单核 2.7%**，纯粹在做探活。改成：

```yaml
      interval: 30s
      start_interval: 2s   # 刚启动那 10 秒内仍然每 2 秒探一次，依赖方不用干等
      start_period: 10s
      timeout: 5s
      retries: 3
```

**实测效果：4–7% → 0.00–0.05%**（顺带推翻了我原先「MinIO 在跑数据扫描器」的猜测 —— 扫描器其实几乎不花钱，全是探活）。

> 另外三个服务的 healthcheck 分别是 clickhouse 5s（`wget --spider`）、redis 3s（`redis-cli`）、postgres 3s（`pg_isready`），都是小的 C 二进制，单次成本比 `mc` 低一个量级，合计 <0.5%，没动。想统一拉长也可以，但对 postgres/redis 意味着要重建容器（Langfuse 会闪一下），不划算。

**两个坑**：

1. **XML 注释里不能出现 `--`**。第一版注释写了 "kept -- useful"，ClickHouse 直接 `SAXParseException` 起不来、容器进崩溃循环（日志里刷 `Failed to merge config`）。
2. `remove="1"` 只**停采集、不删表**：三张遗留表（194M + 5.5M + 7.2M 行，合计 ~460 MB）还在原地。回收空间要手动：

   ```sql
   DROP TABLE system.asynchronous_metric_log;   -- 同理 text_log / trace_log
   ```

## 目录结构

```text
langfuse-stack/
├── docker-compose.yml          # 官方 v3.225.5 的 compose（加了 name: langfuse，见下）
├── clickhouse-system-logs.xml  # 关掉 ClickHouse 自己的系统日志（见「ClickHouse CPU」一节）
└── .env                        # 本实例专属：种子变量 + 密钥
```

**必须放在独立目录**，原因见下文「坑 1」。仓库根目录的 `.env`（agent 服务的配置）**不能**被这套 compose 读到。

**目录别叫 `langfuse`**：那样会在仓库根目录留下一个和依赖同名的目录，ruff 的 isort 会把 `from langfuse import ...` 误判成本地模块，从而把 `src/service/service.py` 的 import 顺序改错（实测确认过）。所以叫 `langfuse-stack`。

改名后 Compose 的项目名默认会跟着目录名变，导致现有容器和数据卷失联，所以 compose 文件顶部钉了一个字段：

```yaml
name: langfuse
```

## 启动 / 停止

```sh
cd langfuse-stack
docker compose up -d        # 启动（首次要拉约 4.9 GB 镜像）
docker compose ps           # 看状态
docker compose logs -f web  # 跟踪日志
docker compose down         # 停止，保留数据
docker compose down -v      # 停止并清空数据（重置 Langfuse）
```

健康检查：

```sh
curl -s http://localhost:3000/api/public/health
# {"status":"OK","version":"3.225.11"}
```

## 访问与凭据

| 项 | 值 |
| --- | --- |
| Web UI | <http://localhost:3000> |
| 登录邮箱 | `admin@local.dev` |
| 登录密码 | `langfuselocal123` |
| Public key | `pk-lf-local` |
| Secret key | `sk-lf-local` |

这些是启动时用 `LANGFUSE_INIT_*` **种子化**出来的——首boot 自动创建组织/项目/用户/密钥，**不需要注册**。种子值写在 `langfuse-stack/.env` 里。

> 如果改了 `langfuse-stack/.env` 里的种子值，必须 `docker compose down -v` 清空数据后重启，否则旧数据里的凭据仍然生效。

## 接入 agent 服务

仓库根目录的 `.env` 加四行，然后**重启服务**：

```ini
LANGFUSE_TRACING=true
LANGFUSE_HOST=http://localhost:3000
LANGFUSE_PUBLIC_KEY=pk-lf-local
LANGFUSE_SECRET_KEY=sk-lf-local
```

对应 `src/core/settings.py:117-120` 的四个字段；`src/service/service.py:156-160` 在 `LANGFUSE_TRACING` 为真时挂上 `CallbackHandler`。

顺带确认：`.env` 里**不要**在值后面写 `#` 注释（`python-dotenv` 会把注释当成值的一部分，见 LOCAL_SETUP.md 坑 3）。

## 已接入的 Langfuse 能力（2026-10-14）

| 能力 | 状态 | 怎么接的 |
| --- | --- | --- |
| **Traces** | ✅ 一直有 | `CallbackHandler` 挂在 RunnableConfig（`service.py:_handle_input`、`agui.py:_base_config`） |
| **Sessions** | ✅ 2026-10-14 | config metadata 加 `langfuse_session_id: thread_id` —— handler **只认这个键**（`CallbackHandler.py:496`），普通 `thread_id` 它不看 |
| **Users** | ✅ 2026-10-14 | 同上，`langfuse_user_id: user_id` |
| **Scores（人工反馈）** | ✅ 2026-10-14 | `POST /feedback` 除 LangSmith 外，再 `create_score(trace_id=run_id)`；因为 trace id 就是 run_id（见下），**不需要任何映射表** |
| **trace id = run_id** | ✅ 2026-10-14 | handler 构造时传 `trace_context={"trace_id": run_id.hex}`（`src/service/tracing.py:run_id_as_trace_id`）—— 日志里的 run_id 拿去就是 trace id |

实测过的链路：

```text
POST /chatbot/invoke → run_id 01a138ac-6807-7312-99cb-3381098732fa
   → GET /api/public/traces/01a138ac6807731299cb3381098732fa → 200 ✓
   → trace.userId = 请求的 user_id ✓   trace.sessionId = thread_id ✓
POST /feedback（同一 run_id）→ Langfuse 出现 score: human-feedback-stars = 0.8 ✓
```

- **trace id 必须是 32 位 hex**：传的是 `UUID.hex`（无横杠）。AG-UI 的 run_id 由客户端给、不一定是 UUID —— 代码用 `run_id_as_trace_id()` 过滤，不合法就不指定（那条 trace 用 Langfuse 自己生成的 id）。
- **评委别给报错的 trace 打分**：2026-10-14 实测一条 `Conciseness 0` 打在**连接失败**（`level=ERROR`）的 trace 上，评论却在批"生成内容只是重复了问题"——那次根本没有生成。用 LLM-as-a-judge 前先看 trace 的 level。
- **还没接**：Datasets/Experiments、Prompt Management。

### 成本追踪（2026-10-14 配好）

Langfuse 内置的 50 个模型定义里**没有 DeepSeek**，所以 `totalCost` 一直是 0。用公开 API 建了两个定义（`POST /api/public/models`，凭据就是 `.env` 里的那对 key）：

| 模型 | 匹配规则 | input /1M | output /1M | cache 读 /1M |
| --- | --- | --- | --- | --- |
| `deepseek-flash` | `(?i)^(deepseek-flash\|deepseek-v4-flash)$` | $0.30 | $1.20 | $0.006 |
| `deepseek-v4-pro` | `(?i)^(deepseek-v4-pro\|deepseek-pro)$` | $1.32 | $3.96 | $0.044 |

（价目取自官方 pricing 页，2026-10-14；这里是**峰时价**，谷时对半。匹配规则两个名字都要写，因为请求用的是 `deepseek-v4-flash`、而官方后来把正式名改成了 `deepseek-flash`。）

**三个坑**（都实测过）：

1. **推理 token 必须单独配价**。flash 默认 thinking，`usageDetails` 里 `output` 和 `output_reasoning` 是**两个键**；只配 `output` 的话推理部分按 0 算 —— 实测一次调用 `input=55, output=2, output_reasoning=37`，只配 output 时算 $1.59e-05，补上 `output_reasoning` 后 $6.33e-05（手算逐位吻合）。
2. **公开 API 只收扁平价格或 `pricingTiers`**：直接传 `prices` 对象会被 400（`Must provide either flat prices ... OR pricingTiers`）；但 `pricingTiers[].prices` **接受任意用量键**（`output_reasoning`、`input_cache_read` 都能塞）。
3. **不回溯**：成本在入库时计算一次，改完定义只影响**新** trace；历史 trace 会一直是 0。另外 API **没有更新接口**（`PUT` 405），改价目只能 `DELETE /api/public/models/{id}` 再重建。

### LLM 评委（LLM-as-a-judge，2026-10-14 配的）

评估页（`/project/local/evals/<id>`）上配了一个 `Conciseness` 评委：全量评（sampling 1.0）、延迟 30s、filter = `type:GENERATION` 且排除 Langfuse 自己的内部环境。

**两个必须配对的地方**（都踩过）：

1. **评委模型不能是 thinking 模型**。默认评估模型是**全局**的（UI 在 Settings → Evaluations，存在 `default_llm_models` 表）：
   - `deepseek-flash` ✗ —— 直接报 `{"error":"Thinking mode does not support this tool_choice"}`；
   - `deepseek-chat` ✓ —— 支持强制 tool_choice。

   （同一个坑仓库里也踩过：`interrupt_agent` 的结构化输出，见 [LOCAL_SETUP.md](LOCAL_SETUP.md) 坑 8。）
2. **变量映射必须是 `query ← input`、`generation ← output`**。配成 `generation ← input` 时，评委拿"用户的问题"当"模型的回答"，分数清一色 `0.1`、评论全是"只是把问题原样回显"——而且**它照样会给 `level=ERROR` 的失败 trace 打分**，评论纯属编造。

**但它仍不稳定（DeepSeek 兼容层的锅，不是配置）**：实测评估任务约 **40% 成功**，失败信息恒为 `No object generated: could not parse the response`。手动复刻同一请求：

| 复刻项 | 结果 |
| --- | --- |
| 真实模板 + 真实回答 | ✅ 正常返回 tool_use，评语合理 |
| + AI-SDK 风格的 `$schema` / `additionalProperties` | ✅ |
| + `stream: true` | ✅ |
| 点名式 `tool_choice:{type:tool,name:X}` × 5 次 | ✅ 5/5 |
| AI-SDK 式 `tool_choice:{type:any}` × 3 次 | ⚠️ 2/3，其中一次**响应体不是合法 JSON** |

Langfuse 的 `generateObject` 走的是 `type:any` 那条路，所以撞上了 DeepSeek 的 `/anthropic` 兼容层不稳定。**当前选择：接受失败率，失败的在 UI 里对单条 trace 重跑**；想彻底稳就把评委换成 OpenAI/Anthropic 的模型（本机容器实测能直连 `api.openai.com`，评委调用很小、成本可忽略）。

**排查用的三张表**（都在 `langfuse-postgres-1`）：

```sql
-- 每个评估任务的成败与错误
SELECT to_char(created_at,'HH24:MI:SS') t, status, left(error,120), left(job_input_trace_id,14)
FROM job_executions ORDER BY created_at DESC LIMIT 5;
-- 评估器配置：filter / 变量映射 / 采样率 / 延迟
SELECT score_name, status, sampling, delay, variable_mapping::text, filter::text FROM job_configurations;
-- 评委用的默认模型（只在 UI 里改，没有公开 API）
SELECT provider, adapter, model FROM default_llm_models;
```

**评委是"又花一份钱"，但比例差别巨大**（都用真实 trace 数据算的）：

| 一次运行 | 成本 |
| --- | --- |
| chatbot（单轮 1 次调用） | $0.00006 |
| research-assistant（4 次调用 + 工具） | $0.0038 |
| **一次评委调用**（实测：in 227 + cache 384 + out 184） | **$0.00029** |

- 对 **research-assistant** ≈ **+7.6%**（可以忽略）；对极小的 chatbot ≈ **+450%**（评委比 agent 本身还贵 4 倍）。
- **关键杠杆是 `sampling`**（`job_configurations` 里，当前 `1.0` 全量）：本地流量小、全量也就一天几分钱；**上生产建议 5–10%**；A/B / 回归用固定数据集跑几十条，一次实验几分钱。
- 这笔钱买的是：替代人工看 trace（一条一分钟）、替代盲目上线、同一把尺子可反复用于 prompt / 模型对比。评委输入的大头是模板示例，DeepSeek 的 cache 已经命中一部分。

**怎么看到评委返回了什么**（mitmproxy 抓不到 —— 评委跑在 Langfuse 容器里直连 `api.deepseek.com`，容器没有代理配置，且 Node 的 fetch 默认不认 `HTTP_PROXY`）：评委**自己的调用也记在 Langfuse 里**，`environment = langfuse-llm-as-a-judge`（评估器的 filter 把它从默认视图排掉了，所以 UI 里看不见）。直接查 ClickHouse：

```sql
SELECT toDateTime(start_time) t, type, level, left(status_message,60), left(output,120), usage_details
FROM observations WHERE environment='langfuse-llm-as-a-judge' ORDER BY start_time DESC LIMIT 6;
```

失败 vs 成功长这样（2026-10-14 实测）：

```text
❌ GENERATION  output=[{"role":"assistant","content":[]}]   usage: input 248, cached 384, output 42
   SPAN        ERROR  "No object generated: could not parse the response."
✅ GENERATION  content=[{"type":"text","text":"{\"reasoning\":\"…"}]   ← 工具调用没来，JSON 是当**文本**回的，SDK 照样接受了
   SPAN        output={"reasoning":"…","score":0.1}
```

即：**失败时模型返回的是空内容** —— 有 42 个输出 token 的计费，却没有产出任何可用的内容块（多半是思考内容被兼容层吞了）。这与 `deepseek-*` 默认开 thinking 的推断一致；手动实测**关掉思考**（`{"reasoning": {"effort": "none"}}`）后，同样的模板/内容能稳定拿到 tool_use。

**想修**：给默认评估模型加模型参数 `{"reasoning": {"effort": "none"}}`（存在 `default_llm_models.model_params`，现在还是 `{}`）。UI 里有模型参数输入框就填进去，没有的话一条 UPDATE 即可。

> 已知改进项：给 filter 加一条 `level != ERROR`，省得评委给失败调用编评论。

### Prompt Management（2026-10-14 试点：research-assistant）

**试点选了 `research-assistant` 的 instructions**（不是 safeguard —— 那台机器上 safeguard 空转，搬了看不到效果）。默认 agent、每次调用都跑、也确实需要反复调。

**代码怎么接的**（三处）：

1. `src/core/prompts.py`：`get_prompt(name, default, **variables)` —— 有 Langfuse 就用它，没有/拉不到/名字不存在就**回落本地常量**（`settings.LANGFUSE_TRACING` 关掉时直接返回本地）。所以 clone 下来没 Langfuse 也能跑 ✓
2. `src/agents/research_assistant.py`：`acall_model` 里取一次 prompt，文本喂给 `wrap_model`，并把 prompt 对象挂到 run metadata 的 `langfuse_prompt` 键上（handler 只认这个键，`CallbackHandler.py:665`）→ **trace 关联到 prompt 版本**
3. Langfuse 侧：`POST /api/public/prompts` 建了 `research-assistant-instructions`（text 类型、label `production`），正文用 `{{current_date}}` 占位、由 `get_prompt(...)` 的变量填

**实测效果**：

```text
generation 上出现:   promptName: research-assistant-instructions | promptVersion: 1
在 Langfuse 里发 v2（加了条 "回答末尾必须输出 [PROMPT-V2-LIVE]"）
   → 等 60 秒（SDK 缓存过期）→ 同一个请求
   → 回答里出现了 [PROMPT-V2-LIVE]，trace 显示 promptVersion: 2   ✓ 服务没重启
（演示后已发 v3 把 production 恢复成原样；版本历史里 v1/v2/v3 都在）
```

**几个要知道的点**：

- **缓存 60 秒**：Langfuse SDK 客户端缓存拉到的 prompt（`LANGFUSE_PROMPT_CACHE_DEFAULT_TTL_SECONDS`，默认 60），所以改词后最多 60 秒生效 —— 这就是"不用重启"的代价上限。`get_prompt` 每次都调是没问题的（命中缓存时只是一次字典查找）。
- **API 建 prompt 必须带 `isActive: true`**（text 类型），否则 400 `invalid_union`。改 prompt = 再 POST 一次同名，会**新建一个 version** 并把 `production` label 挪过去。
- **回落是静默的**：Langfuse 拉不到只打一条 warning 就用本地文本（`core/prompts.py`）。想确认当前生效的是哪个版本，看 trace 的 `promptVersion` —— 是空的就说明走的是本地兜底。
- 想给别的 agent 也接：import `get_prompt`，把常量当 default 传进去即可（`rag_assistant.py` 的 instructions 是下一个自然候选）。**safeguard 的 policy 也适合**（策略型、需要反复调），但要先有 Groq key 才看得见效果。

### Datasets + Experiments（2026-10-14 搭好）

**被测对象就是服务本身**：脚本把数据集里的问题逐条打到 `POST /{agent}/invoke`，再把结果挂回 Langfuse 的数据集运行 —— 因为**响应的 run_id 就是 trace id**，数据集运行条目直接引用 **agent 自己的 trace**（完整 span 树、成本、prompt 版本），不是脚本造的壳。这一点是之前把 trace id 对齐成 run id 才顺理成章的。

脚本：`scripts/experiment.py`（+ 示例题集 `scripts/example_dataset.json`）

```sh
# 1) 建数据集（或追加题目）
PYTHONPATH=src uv run python scripts/experiment.py --dataset research-basics --seed scripts/example_dataset.json
# 2) 每套配置跑一次；在 Langfuse 里并排对比
PYTHONPATH=src uv run python scripts/experiment.py --dataset research-basics --model deepseek-v4-flash --run-name flash-1
PYTHONPATH=src uv run python scripts/experiment.py --dataset research-basics --model deepseek-v4-pro  --run-name pro-1
```

（`AGENT_URL` 从 `.env` 读；服务要先起着。`--agent/--model/--limit/--base-url` 都可选。）

**实测（flash vs pro，各 5 题）**：

| run | n | 平均延迟 | 总成本 | 模型 | promptVersion |
| --- | --- | --- | --- | --- | --- |
| flash-1 | 5 | 2.1s | **$0.001334** | deepseek-v4-flash | 4 |
| pro-1 | 5 | 2.1s | **$0.002679** | deepseek-v4-pro | 4 |

pro 成本正好是 flash 的 2 倍、延迟持平 —— 这就是「改配置 → 跑同一批题 → 拿数据决定」的闭环。Langfuse UI 的 Datasets 页能并排看两次运行的输出，点进任一条就是 agent 的真实 trace。

**几个设计点**：

- **thread_id 按「run + item」隔离**（`{run_name}-{item_id}`）：否则第二次实验会接着第一次的对话历史，结果就被污染了。
- 建数据集/写运行结果走 **公开 API**（SDK 没有 `create_dataset_run_item`）：`POST /datasets`、`POST /dataset-items`、`POST /dataset-run-items{runName,datasetItemId,traceId}`；查询运行条目要 `datasetId` + `runName`（不是 datasetName，给错会 400）。
- 数据集是**要维护的东西**（题集要更新、结论要记），不像 Sessions/成本那样配一次就完事 —— 没具体改动要验时可以先放着。
- 遗留：探针数据集 `zz-api-probe` 删不掉（`DELETE /api/public/datasets/{name}` 返回 405），在 UI 里删一下即可。

## 坑 1：compose 放在仓库根目录会导致 P1000 认证失败

最初把 `langfuse.yml` 放在仓库根目录，结果 `langfuse-web` 无限重启：

```text
Error: P1000: Authentication failed against database server,
the provided database credentials for `postgres` are not valid.
Datasource "db": PostgreSQL database "postgres" at "postgres:5432"
```

**根因**：Docker Compose 会自动读取 **compose 文件所在目录**的 `.env` 做变量替换。仓库根目录那份 `.env` 里有：

```ini
POSTGRES_PASSWORD=<dify 的 postgres 密码>
```

于是 compose 里 `POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-postgres}` 被替换成了 dify 的密码；而 langfuse-web 的 `DATABASE_URL` 是**字面量** `postgresql://postgres:postgres@postgres:5432/postgres`（没有 `${}`），仍是 `postgres`。两边对不上 → 认证失败。

**修法**：把 compose 挪进独立目录（本机是 `langfuse-stack/`），并在那里放一个只含种子变量的 `.env`。

> 通用教训：**任何放在仓库根目录的第三方 compose 都会继承项目的 `.env`**。要么挪进子目录，要么用 `--env-file` 显式指定。

## 坑 2：minio 报 `Permission denied`

```text
mkdir: cannot create directory '/data/langfuse': Permission denied
```

对比镜像内与卷的权限：

| | 权限 |
| --- | --- |
| 镜像里的 `/data` | `drwxrwxrwx root root`（777）——上游正是靠 777 让非 root 用户可写 |
| 实际创建的命名卷 | `drwxr-xr-x root root`（755） |

**Docker Desktop for Windows 创建命名卷时没保留镜像里的 777**，而 `cgr.dev/chainguard/minio` 默认以非 root 运行（uid 65532），于是写不进去。

**修法**：给 minio 服务加 `user: root`（该实例只绑 127.0.0.1，风险可接受）：

```yaml
  minio:
    image: cgr.dev/chainguard/minio
    user: root
```

## 端口改动（避免与 dify 冲突）

dify 栈占着 **5432**（postgres）和 **6379**（redis），Langfuse 默认也要用，必须改。改法是把纯内部服务的宿主映射**整个删掉**（它们只用 Docker 内网通信，配置里引用的是服务名 `postgres:5432`、`clickhouse:8123`、`minio:9000`）：

| 服务 | 官方默认 | 改后 |
| --- | --- | --- |
| langfuse-worker | `127.0.0.1:3030` | 删除 |
| clickhouse | `127.0.0.1:8123`、`127.0.0.1:9000` | 删除 |
| redis | `127.0.0.1:6379` | 删除 |
| postgres | `127.0.0.1:5432` | 删除 |
| langfuse-web | `3000:3000`（全网卡） | `0.0.0.0:3000:3000`（2026-10-12 改回全网卡；此前是仅本机） |
| minio | `9090:9000`（全网卡） | `127.0.0.1:9090:9000` |

**minio 的宿主端口不能删**——compose 里有两个变量指向它，是给浏览器用的预签名 URL 地址：

```yaml
LANGFUSE_S3_MEDIA_UPLOAD_ENDPOINT: http://localhost:9090
LANGFUSE_S3_BATCH_EXPORT_EXTERNAL_ENDPOINT: http://localhost:9090
```

顺手把 web 和 minio 从「所有网卡」收紧到「仅本机」。

### 局域网访问（2026-10-12）

要把 UI 开放给同一局域网的其他设备（手机、另一台电脑），把 web 的映射改成 `0.0.0.0:3000:3000` 即可 —— 之后访问 `http://<本机局域网IP>:3000`。

两个坑：

1. **改端口映射必须 `docker compose up -d`**（会重建 web 容器），`docker compose restart` 只重启进程、**不应用端口变更**——文件改了但 `ps` 里还是老绑定，多半就是这个原因。
2. 语法的坑：`- :3000:3000`（host IP 留空）在 Docker 里**也等于全网卡**（`docker compose config` 渲染出来没有 `host_ip` 字段），但看起来像笔误；写显式的 `0.0.0.0:3000:3000` 更清楚。反过来，「仅本机」必须显式写 `127.0.0.1:3000:3000`。

> **安全提醒**：全网卡 = 同一局域网（含访客 WiFi）上任何人都能打开这个 UI，而本文档上面就写着登录邮箱和密码；trace 里是**完整的 prompt 和对话内容**。开放期间建议至少改掉默认密码，或者只在需要时开。

## 验证 tracing 真的生效

发一次真实请求，再查 Langfuse 的 API：

```sh
# 1. 制造一条 trace
curl -s -X POST http://localhost:8081/chatbot/invoke \
  -H 'Content-Type: application/json' \
  -d '{"message":"hi","thread_id":"trace-verify-001","user_id":"verify"}'

# 2. 等几秒（Langfuse 异步批量上报），然后查
curl -s -u "pk-lf-local:sk-lf-local" "http://localhost:3000/api/public/traces?limit=3"
```

正常情况下应看到一条名为 `LangGraph` 的 trace。取它的 id 再查观测点：

```sh
curl -s -u "pk-lf-local:sk-lf-local" \
  "http://localhost:3000/api/public/observations?traceId=<trace_id>&limit=10"
```

本机实测的观测点结构：

```text
[GENERATION] ChatOpenAI     ← LLM 调用，含完整 prompt 与返回
[CHAIN] chatbot             ← agent 节点
[CHAIN] LangGraph           ← 根 span
```

## 磁盘占用

| 镜像 | 大小 |
| --- | --- |
| clickhouse/clickhouse-server | 1.26 GB |
| langfuse/langfuse | 1.75 GB |
| langfuse/langfuse-worker | 1.65 GB |
| cgr.dev/chainguard/minio | 235 MB |
| **合计** | **约 4.9 GB** |

外加 5 个命名卷（`langfuse_postgres_data`、`langfuse_clickhouse_data`、`langfuse_clickhouse_logs`、`langfuse_minio_data`、`langfuse_redis_data`）。

`docker compose down -v` 会连卷一起删掉。
