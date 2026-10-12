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
