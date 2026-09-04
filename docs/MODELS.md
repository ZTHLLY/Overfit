# 模型配置与兼容性边界（Models）

> Overfit 使用两个独立的 HTTP model capabilities：embedding 与 generation。
> 本文描述当前代码实际调用的协议、配置和护栏。层级流程见
> [PIPELINE.md](./PIPELINE.md)，依赖见 [TECH-STACK.md](./TECH-STACK.md)。

## 两类模型，不可互换

| | Embedding | Generation |
| --- | --- | --- |
| 输入 → 输出 | 文本 → float vector | prompt → JSON text |
| 代码位置 | `embedding.py` | `query/generator.py` |
| 使用层 | ingestion 第 4 层、query 第 6 层 | 第 7 层 |
| 当前默认 ID | `bge-m3` | `qwen3.6:27b` |
| 当前 API | OpenAI-compatible `/embeddings` | OpenAI-compatible streaming Chat Completions |
| 是否写入 index profile | model ID + 实测 dimension | 否 |
| 能否不重建就更换 | **不能** | **可以** |

安装 Python 包不会下载或启动这两个模型。默认 endpoint 是本机 Ollama，但任何
满足当前请求/响应契约的 OpenAI-compatible endpoint 都可以通过配置替换。
“本地优先”是默认部署方式，不是 Embedder/Generator 的 provider 硬编码。

---

## Embedding：索引坐标系

### 当前调用流程

`Embedder` 的实际行为：

1. 延迟创建 `openai.OpenAI` client；
2. 以默认 batch size 16 调用 `client.embeddings.create(model=..., input=...)`；
3. 最多请求 4 次，按 1.5 秒指数退避；
4. 按 response item 的 `index` 排序，确保 vector 仍对应原 input；
5. 验证返回数量；
6. 将每个 vector 归一化为单位长度。

首次访问 `embedder.dimension` 会 embed `"dimension probe"`，用实际返回值长度
建/校验 sqlite-vec 的固定宽度表。项目没有 `EMBED_DIM` 配置项。

直接检索时 query 调用同一个 `Embedder.embed_one()`，然后 sqlite-vec 执行 KNN。
这保证代码路径相同；数据库 profile 负责保证 model ID/dimension 与建库时相同。

### 当前兼容性护栏

每个 index 的 `meta` 表记录：

```text
schema_version
embed_model
embed_dim
chunk_size
chunk_overlap
parser
```

打开 index 时，live profile 与上述每个值逐项比较；不一致抛
`IndexMismatchError`，提示：

```bash
uv run overfit ingest --course IFN580 --rebuild
```

为什么必须重建：已存 document vectors 与新 query vector 若来自不同坐标系，
距离没有可解释意义；保留旧 vector 只会产生看似正常但错误的结果。

当前比较为了兼容旧库，会把**缺失的** meta key 当成 live value；缺字段本身不
报错，只有数据库中已存在且不同的值才报错。因此老 index 可能没有后来新增的
profile 字段而仍可打开。

这套护栏的**现有边界**同样重要：

- 没有记录 `EMBED_BASE_URL`；
- 没有记录 runtime（Ollama/vLLM/其他实现）；
- 没有记录模型 revision、权重 hash、精度或量化；
- 只凭相同 model string 和 dimension 无法证明两个 endpoint 生成完全兼容的
  vectors。

因此即使 `EMBED_MODEL` 文本没变，只要实际权重/runtime/量化发生改变，也应主动
`--rebuild`。旧文档中“已经记录 embed_runtime”的说法不符合当前 schema。

### 当前 operational constraint

所有 index command 都通过 `_open_store()` 先探测 live embedding dimension，再
打开 SQLite。结果是：`status`、`topics`、whole-course `coverage` 和没有 topic 的
`mock` 虽然不需要新的 semantic query embedding，当前仍要求 embedding endpoint
可达。把 dimension 先从 DB 读取、仅在必要时 probe 是可能的未来优化，但尚未
实现。

### `embed-check`

```bash
uv run overfit embed-check
```

此命令：

- 打印 endpoint、model 和实测 dimension；
- 以英文 overfitting 句子为 anchor；
- 打印 paraphrase、same-topic、underfitting、中文问题、无关行政文字、shopping
  list 的 dot-product similarity。

它是人工 sanity check，没有自动阈值，也不会因排序不理想以非零状态退出。
Dimension 正确只证明 API shape 正确；相似度 probes 才提供一点语义证据。

---

## Generation：可替换的产物 writer

Generation 只在当前唯一的生成命令 `mock` 中使用。切换 `LLM_*` 不改变任何
已存 chunk/vector，因此不需要重建 index。

### 当前请求

Generator 调用：

```python
client.chat.completions.create(
    model=...,
    messages=...,
    temperature=...,
    response_format=...,
    stream=True,
)
```

它没有调用 Responses API，也没有设置 `max_tokens`/`max_completion_tokens`。
`REQUEST_TIMEOUT` 传给 OpenAI client；client 自带 retry 被关闭，schema/capability
重试由项目控制。

stream 中普通 `content` 与常见 `reasoning_content`/`reasoning` 字段都会用于进度
显示，但只有 `content` 被拼成最终 JSON body。

### Structured-output ladder

1. 第一次优先发送 strict `json_schema`（schema 来自 Pydantic）；
2. endpoint 拒绝时尝试 `json_object`；
3. 再拒绝时不提供 response format，从 prose/code fence 中提取 JSON object；
4. JSON 解析后由 `GeneratedExam` 验证；
5. validation 失败会把第一条错误和模型原回复反馈给模型，再次请求。

默认 `MAX_RETRIES=3`，实现中的 total attempt 是 `3 + 1 = 4`。一次 attempt 内如果
endpoint 拒绝某种 response format，可能继续发起较弱格式的请求。

代码还接受几种意图明确的 wrapper 差异：bare array、单个完整 item、或 object
中唯一的 list 字段都可规范为 `{"items": [...]}`。这不是绕过字段验证；每个 item
仍必须具有：

```text
topic, question, answer, source, page
```

### 引用不是事实验证器

LLM schema 只能保证“给了 source/page 字段”。项目随后再把它与本次 supplied
chunks 对照：未知 source 和偏离过大的 page 会被丢弃，最多 2 页的近误差会修正
到最近允许页。

现有检查证明的是：**引用指向模型确实看到过的 passage**。它不能证明 answer
中的每句话都受该 passage 支持，也没有第二个模型做 entailment/fact-check。
最终 Markdown 因此明确要求用户回到 cited page 检查。

### Prompt 的当前下限设计

- schema 是一层 `items` + flat item，不做复杂嵌套；
- 模型可以显式返回空 `items` 或少于目标数，避免被迫从行政/碎片材料出题；
- 问题必须自包含，不允许“根据上面的材料”一类指代；
- 题目/答案输出由 Jinja2 完成，不让模型自由设计 Markdown；
- 当前不要求题型、难度、分值或固定 section。

---

## 完整配置

```dotenv
# Generation：可随时换，不影响已建 index
LLM_BASE_URL=http://localhost:11434/v1
LLM_API_KEY=ollama
LLM_MODEL=qwen3.6:27b

# Embedding：改变实际坐标系后必须 --rebuild
EMBED_BASE_URL=http://localhost:11434/v1
EMBED_API_KEY=ollama
EMBED_MODEL=bge-m3

# Generation runtime
TEMPERATURE=0.3
MAX_RETRIES=3
REQUEST_TIMEOUT=900
```

默认值来自 `config.py`，`.env.example` 提供同一组本地示例。Provider-specific
模型名称和 endpoint 可用性由对应服务决定；Overfit 不在启动时维护一份远程模型
catalog。

### Ollama 默认配置

```bash
ollama pull bge-m3
ollama pull qwen3.6:27b
uv run overfit embed-check
```

Ollama 的 OpenAI-compatible base URL 需要 `/v1`。本地服务通常忽略 API key，
但 OpenAI SDK 要求传入非空值，所以默认写 `ollama`。

### 切换 generation endpoint

只覆盖 `LLM_*`：

```bash
LLM_BASE_URL=https://provider.example/v1 \
LLM_API_KEY=... \
LLM_MODEL=provider-model \
uv run overfit mock --course IFN580
```

Embedding index 不受影响。Endpoint 必须兼容 streaming Chat Completions；如果不
支持 `json_schema`，当前代码会自动尝试较弱格式。

### Docker 中访问宿主机模型

容器内的 `localhost` 指向容器自身，不是宿主机。若 Docker 环境提供
`host.docker.internal`，可只对容器进程覆盖环境变量，不改为宿主机工作保留的
`.env`：

```bash
EMBED_BASE_URL=http://host.docker.internal:11434/v1 \
LLM_BASE_URL=http://host.docker.internal:11434/v1 \
uv run overfit embed-check
```

Linux 上是否存在该 hostname 取决于容器启动配置；核心要求只是 endpoint 对容器
可路由。

---

## 哪些命令需要哪种模型

| 命令 | Embedding endpoint | Generation endpoint |
| --- | :---: | :---: |
| `inspect`, `chunks` | 否 | 否 |
| `embed-check` | 是 | 否 |
| `ingest` | 是 | 否 |
| `search` | 是 | 否 |
| `status`, `topics` | **当前是**（dimension probe） | 否 |
| `coverage` | **当前是**（open probe；topic 还会 embed query） | 否 |
| `mock` | **当前是**（open probe；topic 还会 embed query） | 是 |

数据库已建好并不代表可以完全离线于 embedding endpoint 使用，这是当前
`_open_store()` 的实现事实。

## 选择模型时需要满足的合同

### Embedding endpoint

- 支持 OpenAI-compatible batch embeddings；
- 为每个 input 返回一个 vector 和可靠的 `index`；
- 输出 dimension 稳定；
- 相同 model/runtime/revision 对 ingestion/query 稳定；
- 若需要中文 query 检索英文课件，应自行用 `embed-check` 验证跨语言效果。

### Generation endpoint

- 支持 streaming Chat Completions；
- 能返回 JSON object（支持 JSON schema 最好，但不是硬要求）；
- 能在 timeout 内处理选材 prompt；
- 能遵守 flat Pydantic schema 和 grounding prompt。

强模型不能修复错误 extraction/retrieval。调试顺序应是
`inspect` → `chunks` → `search`/`coverage` → `mock`，先确认模型看到的材料。

## Roadmap / 开放问题（未实现）

以下均不是当前能力：

- 内嵌 fastembed/sentence-transformers runtime；
- 自动下载 embedding weights；
- 记录 runtime/revision/weight hash 形成更强 index profile；
- 在 endpoint 不可达时仅用 DB profile 打开只读命令；
- 自动模型 benchmark、quality threshold 或 provider discovery；
- 第二阶段 citation entailment/fact-check；
- 多 generation provider 的能力探测与统一 feature matrix。
