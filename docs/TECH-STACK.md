# 技术栈：当前依赖与边界（Tech Stack）

> 本文以 `pyproject.toml` 和 `src/overfit/` 为准。整体图见
> [ARCHITECTURE.md](./ARCHITECTURE.md)，逐层实现见
> [PIPELINE.md](./PIPELINE.md)，模型配置见 [MODELS.md](./MODELS.md)。

Overfit 当前是一套轻量、手写、本地优先的 Python RAG CLI。这里的“本地优先”
指 source、索引和输出都在本地；模型通过 HTTP endpoint 调用，endpoint 可以是
本机 Ollama，也可以是其他 OpenAI-compatible 服务。

当前 package metadata 是 `overfit` **0.1.0**，console entry point 为
`overfit = overfit.cli:app`，build backend 为 `uv_build`。

## 运行时总表

| 类别 | 当前选型 | 代码中的用途 |
| --- | --- | --- |
| Python | **3.12+** | `pyproject.toml` 的硬要求 |
| 包/环境管理 | `uv` | lock、sync、运行、build backend (`uv_build`) |
| CLI | Typer `>=0.15` | 9 个 subcommand、类型化参数、终端输出 |
| 配置 | pydantic-settings `>=2.14.2` | 默认值、`.env`、环境变量、校验 |
| 内部数据 | frozen dataclasses | Page/Chunk/vector pipeline contracts |
| 不可信输出 | Pydantic `>=2.13.4` | LLM JSON schema 与运行时 validation |
| PDF（默认） | pypdf `>=6.15.0` | 轻量、逐页 text extraction |
| PDF（可选） | Docling Slim | layout/table/formula-aware extraction；OCR 关闭 |
| 文本输入 | Python stdlib | UTF-8 `.md` / `.markdown` / `.txt` 读取 |
| Chunking | 项目自有 Python 代码 | 4 chars/token 近似、自然切点、页界 continuity |
| 模型客户端 | OpenAI SDK `>=2.53.0` | `/embeddings` 与 streaming chat completions |
| Embedding | endpoint 配置；默认 `bge-m3` | ingestion/query 共用、dimension probe、单位化 |
| 向量存储 | SQLite + sqlite-vec `>=0.1.9` | 每课程单 DB、KNN、文档 hash、index profile |
| 检索 | 项目自有 Python 代码 | dense top-k cosine search |
| 生成选材 | 项目自有 Python 代码 | deterministic clustering、allocation、MMR |
| Prompt / Markdown | Jinja2 `>=3.1.6` | system/user prompts 与两个输出模板 |
| 生成模型 | endpoint 配置；默认 `qwen3.6:27b` | 当前仅 `mock` |
| 开发检查 | pytest `>=9.1.1`、Ruff `>=0.16.1` | dev dependency group |

最低版本是声明约束，不等于文档承诺只支持该精确版本；实际解析出的版本由
`uv.lock` 固定。

## 默认安装为什么轻

基础依赖只有：

```toml
jinja2
openai
pydantic
pydantic-settings
pypdf
sqlite-vec
typer
```

模型权重、Ollama、GPU runtime 和 Docling 不在基础 wheel 中。默认 parser 使用
pypdf；文本发现/排序、SHA-256、SQLite 连接、向量解包等大量功能直接使用 Python
stdlib。Clustering 与 MMR 也没有引入 NumPy、SciPy 或 scikit-learn。

```bash
uv sync          # 基础依赖 + 默认 dev group（pytest、ruff）
uv sync --no-dev # 只安装运行依赖
```

## 可选 Docling 安装

`pyproject.toml` 提供两种 extra：

### `docling`：针对当前 PDF pipeline 的窄安装

```bash
uv sync --extra docling
```

实际声明：

```toml
docling-slim[convert-core,format-pdf,format-latex,models-local] >= 2.119.0
docling-ibm-models[opencv-python-headless] >= 3.14.0
```

选择 headless OpenCV 是因为 CLI 不需要 GUI。这个 extra 支持当前代码使用的
layout、table 与可选 formula enrichment，不安装 office/email/audio reader 或
OCR engine。Parser 明确设置 `do_ocr = False`。

### `docling-full`：兼容性逃生口

```bash
uv sync --extra docling-full
```

它安装 `docling-slim[standard]>=2.119.0`，明显更重，仅用于窄 extra 因 upstream
隐式 import/metadata 差异而无法工作时。安装 full 并不会自动改变
`PDF_BACKEND`，也不会自动打开 OCR。

## 数据与存储技术

### SQLite + sqlite-vec

每门课一个 `INDEX_DIR/<course>.db`，包括：

- 普通 `chunks` 表：原文与 source/page metadata；
- `vec_chunks` virtual table：固定 dimension 的 float vectors；
- `documents` 表：每个 source 的 content hash 与 chunk count；
- `meta` 表：index compatibility profile。

Embedding 会在写入前归一化；sqlite-vec 返回 L2 distance，再由 Store 转为 cosine
similarity。SQLite 使用 WAL。当前规模假设是“几千个 course chunks 可以整库读入
内存做 clustering”；代码没有分布式/服务端 vector database 抽象。

缓存不是单独的 Parser cache：数据库只缓存最终 chunks/vectors 和每个 source 的
hash。相同 source + hash 的文件在下一次 ingestion 中完全跳过 Parse/Chunk/Embed；
删除或重命名 source 不会被增量过程自动清理。

## 模型协议，而不是 provider class hierarchy

`openai.OpenAI(base_url=..., api_key=...)` 同时服务：

- `client.embeddings.create()`；
- `client.chat.completions.create(..., stream=True)`。

所以当前 provider 边界只有 endpoint、key、model 三个配置值。Ollama 是默认值，
不是代码层的唯一 provider。具体兼容度仍有两层现实限制：

1. embedding endpoint 必须接受批量字符串并返回带 `index` 的 vectors；
2. generation endpoint 最好支持 `json_schema`，但代码能降级到
   `json_object` 或无约束 JSON extraction。

详见 [MODELS.md](./MODELS.md)。

## 不依赖框架的算法

| 能力 | 当前自有实现 |
| --- | --- |
| Parser cleaning | running-line 统计、label protection、figure-debris heuristic |
| Chunking | page continuity、边界 rank、overlap snapping、page offset mapping |
| Dense retrieval | query embedding → sqlite-vec KNN |
| Topic inference | farthest-point seed + 最多 6 轮 Lloyd assignment |
| Topic weight | distinct source 文件数，chunk 数为次级排序 |
| Slot allocation | 每 topic 保底 + largest remainder |
| Topic-focused diversity | relevance/redundancy min-max scaling + MMR |
| Citation check | 将 LLM 的 source/page 与 supplied chunks 比对 |

这种实现可直接调试每一步，但也意味着项目自己承担数值正确性、规模化和测试覆盖。

## 当前刻意没有引入

以下是现状，不代表永远拒绝：

- **LangChain / LlamaIndex**：七层直接组合，没有 callback/chain abstraction；
- **NumPy / scikit-learn**：现有小规模 clustering 用纯 Python；
- **远程 vector database**：没有 pgvector/Qdrant/Pinecone adapter；
- **lexical/hybrid search、re-ranker、query rewriting**：direct search 仍是 dense KNN；
- **OCR dependency**：scanned PDF 当前不支持；
- **内嵌 embedding runtime**：没有 fastembed/sentence-transformers，必须有 HTTP
  embeddings endpoint；
- **web/UI framework**：只有 Typer CLI 和 Markdown files；
- **provider-specific SDK**：没有 Ollama/DeepSeek/vLLM 专用 Python client。

## 配置技术细节

Pydantic Settings 读取根目录 `.env`（UTF-8）和 process environment。路径值会
`expanduser().resolve()`；course name 会清理为安全路径片段；`CHUNK_SIZE >= 50`、
`CHUNK_OVERLAP >= 0` 且 overlap 必须小于 size；temperature、timeout 等也有范围
校验。

完整变量表见 [README](../README.md#configuration) 和
[MODELS.md](./MODELS.md)。`.env` 被 `.gitignore` 排除，`.env.example` 不含真实
credential。

## 测试与质量现状

```bash
uv run pytest -q
uv run ruff check .
```

当前分支的验证结果是 pytest `5 passed`、Ruff `All checks passed!`。
pytest 当前只收集 `tests/test_parser.py` 的 5 个 invariant tests：四个使用 synthetic
`Page`，一个读取临时空文本文件。它们覆盖 Parser 最容易静默损坏
provenance/content 的场景。没有 Loader、Chunker、
Embedder、Store、Retriever/Selection、Generator、CLI 或端到端自动测试；也没有
coverage gate 或仓库 CI workflow。Ruff 已配置为开发依赖，但 `pyproject.toml`
当前没有项目特定 Ruff 规则段。

手动 diagnostic commands 是当前质量工具的一部分：

- `inspect --show-removed`：extraction/cleaning；
- `chunks`：chunk boundaries；
- `embed-check`：endpoint/dimension/semantic sanity；
- `search`：direct retrieval；
- `topics` / `coverage`：generation material selection；
- `status`：index profile/counts。

## Roadmap（未实现）

只有在真实需求出现并配套测试后，才考虑引入 OCR、re-ranker、hybrid search、
remote loader、内嵌 embedding runtime、远程 vector store 或多种 artifact pipeline。
当前依赖表不应把这些候选技术写成已交付能力。
