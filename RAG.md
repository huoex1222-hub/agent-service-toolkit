# RAG 助手（rag-assistant）配置记录

让 `rag-assistant` 跑起来所需的 embedding 配置、建库流程，以及踩到的坑。整理于 2026-10-04。

## 现状

**已跑通。** embedding 走**硅基流动**（OpenAI 兼容的国内服务，直连即可，不需要代理）。

## 依赖链

两个阶段都要 embedding，而且**必须用同一个模型**：

| 阶段 | 位置 | 作用 |
| --- | --- | --- |
| 建库 | `scripts/create_chroma_db.py` | 把 `data/` 下的 PDF / DOCX 切块、向量化、写入 `chroma_db/` |
| 查询 | `src/agents/tools.py` 的 `load_chroma_db()` | 打开同一个库，把用户问题向量化后检索 |

两处都通过 `core.get_embeddings()`（定义在 `src/core/llm.py`）取 embedding 实例。

## 为什么不用 OpenAI

`rag-assistant` 原本写死 `OpenAIEmbeddings()`。本机没有 OpenAI key，而 **DeepSeek 不提供 embeddings 接口**——实测：

```sh
curl -X POST https://api.deepseek.com/v1/embeddings
→ HTTP 404
```

所以需要额外的 embedding 来源。硅基流动的 API 是 **OpenAI 兼容**的，因此不用换库，`OpenAIEmbeddings` 改 `base_url` + `api_key` 即可。

## 配置

`.env`：

```ini
EMBEDDING_API_KEY=sk-...
EMBEDDING_BASE_URL=https://api.siliconflow.cn/v1
EMBEDDING_MODEL=BAAI/bge-m3
```

设计成**可选**：`EMBEDDING_BASE_URL` 为空时回退到 OpenAI proper，不影响仓库原有设计。三个字段声明在 `src/core/settings.py`，由 `get_embeddings()` 消费。

## 建库

```sh
python scripts/create_chroma_db.py
```

产物是仓库根目录的 `chroma_db/`（`.gitignore:196` 已忽略，不会误提交）。

本机结果：**3 个 chunk**，258 KB，向量维度 **1024**（bge-m3）。

## 坑 1：脚本用了已被移除的 import 路径（存量 bug）

`create_chroma_db.py` 原本写的是：

```python
from langchain.text_splitter import RecursiveCharacterTextSplitter
```

在 langchain 1.x 下直接报：

```text
ModuleNotFoundError: No module named 'langchain.text_splitter'
```

正确路径是 `langchain_text_splitters`（该包随 langchain 一起安装）。**也就是说，这个仓库自带的灌数据脚本，在它自己钉的 langchain 版本下本来就是坏的**——值得往上游提。

## 坑 2：换 embedding 模型必须删库重建

不同模型的向量**不在同一个空间**，维度也可能不同。用 A 模型建库、用 B 模型查询时：

- **不会报错**——静默失败，最要命的一点
- 只会检索出一堆不相关的 chunk
- 表现出来像是"模型在胡说"，极难定位

```sh
rm -rf chroma_db && python scripts/create_chroma_db.py
```

`get_embeddings()` 的 docstring 里也写了这条。

## 验证

走 API：

```sh
curl -s -X POST http://localhost:8081/rag-assistant/invoke \
  -H 'Content-Type: application/json' \
  -d '{"message":"What benefits does AcmeTech offer?","thread_id":"rag-check","user_id":"me"}'
```

或在 [Langfuse](LANGFUSE.md) 里看 trace 的观测点结构，一眼能判断检索有没有真的发生：

```text
[TOOL] Database_Search              <- 工具被调用
[RETRIEVER] VectorStoreRetriever    <- 触发向量检索
[CHAIN] model                       <- 基于检索结果作答
```

本机实测：HTTP 200 / 7.3 秒，零异常；回答引用了手册里的四类福利和 PTO 天数等细节，无编造。

## 语料

当前是 `data/AcmeTech_Employee_Handbook.pdf`（英文，4.4 KB）。

换语料：把文件放进 `data/`（支持 PDF 和 DOCX），重新跑建库脚本。`bge-m3` 是多语言的，中文语料同样可用——但换语料后**务必重新建库**。

## 待办

- [ ] `create_chroma_db.py` 的旧 import 路径值得提上游（影响所有跑 RAG 的人）
