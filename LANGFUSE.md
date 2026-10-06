# Langfuse 自托管部署记录

本地跑一套 Langfuse 做 tracing，用于观察 agent 的完整调用链（prompt、模型返回、工具调用、耗时、token 数）。

这是 [NETWORK_CAPTURE.md](NETWORK_CAPTURE.md) 里的「方案①」，部署过程与踩到的坑记在这里。部署于 2026-10-04。

## 目录结构

```text
langfuse-stack/
├── docker-compose.yml    # 官方 v3.225.5 的 compose（加了 name: langfuse，见下）
└── .env                  # 本实例专属：种子变量 + 密钥
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
| langfuse-web | `3000:3000`（全网卡） | `127.0.0.1:3000:3000` |
| minio | `9090:9000`（全网卡） | `127.0.0.1:9090:9000` |

**minio 的宿主端口不能删**——compose 里有两个变量指向它，是给浏览器用的预签名 URL 地址：

```yaml
LANGFUSE_S3_MEDIA_UPLOAD_ENDPOINT: http://localhost:9090
LANGFUSE_S3_BATCH_EXPORT_EXTERNAL_ENDPOINT: http://localhost:9090
```

顺手把 web 和 minio 从「所有网卡」收紧到「仅本机」。

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
