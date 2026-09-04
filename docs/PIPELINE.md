# RAG 七层：当前实现（Pipeline）

> 本文描述 `src/overfit/` **现在实际运行的代码**，不是未来设计稿。整体图见
> [ARCHITECTURE.md](./ARCHITECTURE.md)，依赖选型见
> [TECH-STACK.md](./TECH-STACK.md)，模型边界见 [MODELS.md](./MODELS.md)。

Overfit 把建库和使用分开，但二者共享相同的数据契约与 Embedder：

```text
Ingestion
  1 Loader → 2 Parser → 3 Chunker → 4 Embedder → 5 VectorStore
                                                        │
Query / artifact production                             │
  user query → 6 Retriever ─────────────────────────────┘
                     │
              material selection
                     │
                7 Generator → validated exam → Markdown
```

SQLite 文件是两半之间的持久化边界。当前代码没有 LangChain、LlamaIndex
或常驻服务；命令启动进程后直接组合这些模块。

## 共享数据契约

`src/overfit/models.py` 是层间契约：

| 类型 | 关键字段 | 产生/使用位置 |
| --- | --- | --- |
| `Page` | `number`, `text`, `furniture`, `structured` | Parser 内部与输出 |
| `ParsedDocument` | `source`, `pages` | Parser → Chunker |
| `Chunk` | `id`, `text`, `source`, `page`, `page_end`, `section` | Chunker → Embedder/Store/Prompt |
| `EmbeddedChunk` | `chunk`, `embedding` | Embedder → Store；Store → Selection |
| `RetrievedChunk` | `chunk`, `score` | Store/Retriever → CLI |
| `GeneratedItem` | `topic`, `question`, `answer`, `source`, `page` | LLM → Pydantic → renderer |
| `GeneratedExam` | required `items` list（允许显式空列表） | Generator 输出 |

管道内部使用 frozen dataclass；不可信的 LLM 输出使用 Pydantic。`source`
是课程根目录下的 POSIX 相对路径，例如 `week03/lecture.pdf`。PDF 页码从 1
开始；跨页 chunk 用 `page` + `page_end`。数据库中虽然有 `section` 字段，
当前 Chunker 没有给它赋值，因此不能声称已经支持 section 级引用。

---

## 1. Loader：只发现文件

**实现：** `ingestion/loader.py`

**输入：** 课程目录、扩展名过滤器
**输出：** 稳定排序的 `list[Path]`

当前行为：

1. 验证输入是目录，否则抛 `CourseNotFoundError`；
2. 用 `rglob("*")` 递归遍历；
3. 跳过所有隐藏路径，以及 `__MACOSX`、`node_modules`、`.git`、
   `.obsidian`、`__pycache__`；
4. 按相对于课程根目录的路径（忽略大小写）排序，保证不同文件系统上的顺序稳定；
5. 没有匹配文件时抛 `NoDocumentsError`。

代码具有 `.pdf`、`.md`、`.markdown`、`.txt` 四种解析能力，但运行时默认
`EXTENSIONS=.pdf`。过滤器的意义不仅是格式支持：如果同一讲义同时存在 PDF
和 Markdown 转换版，全部建库会产生重复材料。`inspect` 和 `ingest` 会报告被
当前扩展名配置略过的文件后缀数量。

Loader 不读取文件内容，也没有 URL、Drive、压缩包或 Notion 连接器；这些仍是
Roadmap。

---

## 2. Parser：提取、保留页码、记录清洗判断

**实现：** `ingestion/parser.py`
**输入：** 单个文件、引用用的 `source`、`PDF_BACKEND`
**输出：** `ParsedDocument`

### 2.1 后端

| 输入 | 后端 | 当前行为 |
| --- | --- | --- |
| PDF | `pypdf`（默认） | 逐页 `extract_text()`；轻量但不恢复视觉表格/公式结构 |
| PDF | `docling`（可选） | 布局/表格感知，表格尽量导出为 Markdown；不做公式 enrichment |
| PDF | `docling+formula`（可选） | 同上，并打开公式 enrichment |
| `.md` / `.markdown` / `.txt` | UTF-8 文本 reader | 整个文件作为 `Page(number=1)`，无真实分页 |

Docling 通过 `uv sync --extra docling` 安装并按配置缓存 converter。它同时遍历
BODY 与 FURNITURE layer：不是提前悄悄删除页眉页脚，而是把 verdict 放进
`Page.furniture`，交给统一清洗阶段删除并审计。非普通 prose 的 body block
会放进 `Page.structured`，防止已识别的表格、标题或公式被后续 debris 规则
误删。OCR 当前明确关闭；没有文本层的扫描 PDF 不会被自动识别。

### 2.2 清洗顺序

清洗始终保留原 `Page.number` 和页面槽位，即使某页被清成空字符串：

1. **running furniture / page number**
   - Docling 已标注的 furniture 直接采用；
   - 未标注时，仅对至少 4 页的文件统计每页第一/最后非空行，出现于至少
     60% 页面即视为 running line；
   - 在同一 guard 生效时删除裸页码（如 `12`、`Page 3`、`4 / 20`）。
2. **figure debris**
   - 连续至少 6 行、每行不超过 32 字符、无终止标点且不是 bullet/Markdown
     table row 的片段会被删除；
   - backend 标为 `structured` 的行受保护。
3. **单页规范化**
   - 统一换行；
   - 把 `over-\nfitting` 一类断行连字符重新拼接；
   - 压缩连续空格、尾随空格和三行以上空行，同时保留段落空行。

`cleaning_report()` 与正式 `parse()` 调用同一个 `_clean()`，因此
`inspect --show-removed` 展示的是 ingestion 真正会删除的内容，按
`furniture`、`page-number`、`figure-debris` 标明页面和阶段。它还检查清洗前后
页码序列是否完全一致。

如果 backend 报错，会转换为 `ExtractionError`。清洗后所有页面都为空则抛
`EmptyExtractionError`，而不是让空文档进入索引。`is_probably_scanned()` 只是
`inspect` 的提示性 heuristic（至少 80% 页面少于 20 字符），不会触发 OCR 或
自动控制流程。

---

## 3. Chunker：按局部连续性和自然边界切分

**实现：** `ingestion/chunker.py`
**输入：** `ParsedDocument`、`CHUNK_SIZE`、`CHUNK_OVERLAP`
**输出：** `list[Chunk]`

配置以“近似 token”表示，但实现固定用 `4 chars/token`，不调用 tokenizer。
默认目标是 `250 × 4 = 1000` 字符，overlap 是 `25 × 4 = 100` 字符。

当前算法不是旧文档所说的单纯固定窗口：

1. **逐个页面边界判断是否连续。** 仅当上一页没有终止标点、下一页不是
   bullet 开头、且下一页首字符为小写时合并。证据不明确就保留页界；这样幻灯片
   通常独立，跨页未完句可形成连续 segment。
2. **在 segment 中选切点。** 在目标位置前最多回看目标尺寸的 35%，优先级是
   段落 > 行 > 句子 > 单词；没有候选才硬切。
3. **overlap 对齐。** 下一个窗口从 `end - overlap` 附近的真实边界开始，尽量
   避免半个单词开头。
4. **处理短尾和噪声。** 小于目标 15% 的尾部并入前一块；最终不足 40 字符的
   chunk 丢弃。
5. **恢复 provenance。** segment 保存每页起始 offset，所以每块可以映射回
   `page`/`page_end`。ID 由完整相对 source path、起始页和文档内 chunk 序号
   确定，例如 `week03_lecture_p12_c2`。

`chunks` 命令直接运行 Parser + Chunker，报告每个文件的最小/中位/最大尺寸、
跨页数量，并打印真实 chunk。它不访问 embedding 或数据库。

`Chunk.make_id()` 会把相对路径各段用下划线扁平化。大多数正常课程目录中这很稳定，
但它不是无碰撞编码：例如 `a_b/c.pdf` 与 `a/b_c.pdf` 会得到相同 stem，并可能在
相同 page/chunk 序号上产生相同 ID。`source` 字段本身仍保存无歧义的相对路径。

---

## 4. Embedder：OpenAI-compatible API、批量、重试和归一化

**实现：** `embedding.py`（同时服务建库和查询）
**输入：** `Sequence[str]`
**输出：** 与输入顺序一致的 `list[list[float]]`

当前实现：

- 通过 OpenAI Python SDK 调用 `client.embeddings.create()`；
- 默认每批 16 条；API 返回后按 response item 的 `index` 排序；
- 请求失败最多尝试 4 次，退避为 1.5、3、6 秒；
- 空白字符串用一个空格代替，保持 batch 索引对齐；
- 首次以 `"dimension probe"` 实际请求模型，得到真实 vector width；
- 每个向量归一化为单位长度，使余弦相似度可由 dot product/L2 距离稳定转换。

Embedding endpoint、key 和模型名来自 `EMBED_*`。当前默认是 Ollama +
`bge-m3`，但代码契约是 OpenAI-compatible endpoint，并未把“必须 Ollama”或
“必须永久本地”写死。

---

## 5. VectorStore：每门课一个 SQLite 文件

**实现：** `storage/store.py`
**文件：** `INDEX_DIR/<safe-course>.db`

实际 schema 有四部分：

| 表 | 内容 |
| --- | --- |
| `chunks` | 文本、ID、source、page、page_end、section |
| `vec_chunks` | `sqlite-vec` 的固定宽度 float vector virtual table，与 `chunks.rowid` 对齐 |
| `documents` | 每个 source 的 SHA-256、chunk 数和索引时间 |
| `meta` | schema/profile 兼容性字段 |

`meta` 当前写入并校验：

```text
schema_version, embed_model, embed_dim,
chunk_size, chunk_overlap, parser
```

任一**已存值**不匹配都会抛 `IndexMismatchError` 并要求
`overfit ingest --course <course> --rebuild`。`top_k` 和任何 LLM 设置不会写入
索引，因为它们不改变已存文本/向量。当前也**没有**记录 embedding endpoint、
runtime、模型版本或量化方式；相同模型名来自不同实现时仍可能绕过校验，这是
现有护栏的边界。

兼容旧库时，当前 `_verify()` 把缺失的 meta key 当成 live value，因此“字段缺失”
本身不会触发 mismatch；只有已存在且不同的值会触发。这是当前向后兼容行为，
也意味着很旧的 index profile 可能缺少新护栏字段。

写入时先验证维度。相同 chunk ID 会连同旧 vector 一起替换；重新索引某个
source 前会删除该 source 的全部旧 chunks，避免文件缩短后留下 orphan。
`VectorStore.stats()` 会计算 `chunks`/`vec_chunks` 是否一致；当前只有 `ingest`
在结束时读取该值并在不一致时警告，`status` 命令并不显示 vector 数或
`consistent` 字段。

搜索由 `sqlite-vec` 做 KNN。因为写入的向量已单位化，返回的 L2 distance
`d` 被转换为 cosine similarity：

```text
score = 1 - d² / 2
```

数据库启用 WAL。

### 增量缓存的准确边界

Ingestion 用“**相对 source path + 当前内容 hash**”查询 `documents`：同一路径且
hash 未变才标记 `cached`。内容改变只重建该文件；`--force` 忽略缓存。

当前 pipeline 不扫描数据库以删除磁盘上已经消失的 source。因此删除或重命名
文件后，旧 source 会留在索引中；需要 `--rebuild` 才能得到与磁盘完全一致的
集合。

---

## 6. Retriever 与 material selection：两种不同问题

**实现：** `query/retriever.py`、`query/selection.py`

### 6.1 直接检索

`search QUERY` 和 `retrieve()`：

```text
query string
  → 同一个 Embedder.embed_one()
  → VectorStore.search(top_k, fetch_k)
  → RetrievedChunk(chunk, cosine score)
```

当前是 dense vector KNN，没有 lexical/hybrid search、metadata filter、query
rewriting 或 cross-encoder re-ranking。`fetch_k` 已预留宽候选接口，但目前直接
截为 `top_k`。向量相近表示“谈论相近主题”，不保证 passage 真正回答 query；
这也是 `search` 命令存在的原因。

### 6.2 为整份产物选材

“回答一个 query”与“覆盖整门课”不是同一个任务。`mock`/`coverage` 使用
`gather_material()`：

- **无 `--topic`：**
  1. 读取索引中的全部 vector；
  2. 最多聚成 12 个 topic；
  3. topic weight 是涉及它的 distinct source 文件数，次排序才看 chunk 数；
  4. 能容纳时先给每个 topic 一个 slot，剩余 slot 按 weight 用 largest
     remainder 分配；
  5. 每组内继续选相互有距离的代表性材料，减少重复。
- **有 `--topic`：**
  1. embed topic；
  2. 先从 vector index 取默认 40 个候选（至少覆盖请求 count）；
  3. 在候选内用 MMR，按 relevance 与 redundancy 的 min-max 值组合；
  4. `gather_material` 当前传入 `diversity=0.4`。

Clustering 使用 deterministic farthest-point seeds + 最多 6 轮 Lloyd assignment，
每组选择接近 centroid 且内容足够长的 member。通常先排除不足 200 字符的生成
候选；若因此不够用，会退回最长的材料。代表性中的长度权重在 400 字符饱和，
避免短标题成为 topic representative。最后按 `(source, page)` 排序交给模型。

`topics` 输出 cluster representative、chunk 数、distinct file weight，以及指定
question 数会如何分配。`coverage` 打印生成器会看到的选材；两者都不调用 LLM。

---

## 7. Generator：prompt、结构化输出、校验、引用过滤、渲染

**实现：** `query/generator.py`、`templates/*.j2`
**当前唯一产物命令：** `mock`

### 7.1 Prompt

`mock_prompt.j2` 把每个 passage 标为 `[source pN]` 或 `[source ppN-M]`，要求
模型生成最多指定数量的问题，并允许跳过行政通知、碎片或不适合出题的材料。
`system.j2` 要求只使用所给材料、问题自包含、答案引用 file/page、宁缺毋滥。

默认选材数是 `questions × 2`；`--material` 非零时直接采用该数。`--topic` 同时
影响 selection 和 prompt 的 focus 行。这里的数是 selection budget，不是返回数量
保证；候选不足或组内 slot 超过 member 数时，实际 passages 可以更少。

### 7.2 响应约束和重试

Generator 使用 streaming chat completions，按以下强度逐级退化：

1. 首次优先请求 strict `json_schema`；
2. endpoint 不支持时尝试 `json_object`；
3. 最后不带 response format，从回复文字中提取 JSON object。

返回值会经过 JSON 解析和 Pydantic `GeneratedExam` 校验。为了兼容信息完整但
wrapper 不同的回复，代码可把 bare list、单个完整 item、或唯一 list 字段
（如 `questions`）规范成 `{"items": [...]}`。真正缺字段/类型不符则把第一条
校验错误连同原回复反馈给模型重试。`MAX_RETRIES=3` 表示正常最多 4 个 attempt。
显式 `{"items": []}` 是合法的诚实输出；缺少 `items` 不是。
`items` 当前没有 schema-level 最大长度，Generator 也不截断，因此 prompt 虽要求
“up to count”，不遵守该上限的模型仍可能返回并写出更多题。

### 7.3 Citation 过滤的保证范围

Schema 强制每题有 `source` 和 `page`，随后代码将它们与**本次实际提供给模型的
passages**比对：

- source 不存在：丢弃；
- page 不在提供范围，且相差超过 2 页：丢弃；
- 相差不超过 2 页：修正为最近的允许页；
- page-spanning chunk 的整个 `[page, page_end]` 都允许。

这能保证引用指向某个已提供 passage，不能证明答案语义上确实由该 passage
支持。模板因此明确提示读者回看原文。

### 7.4 Markdown 输出

合法 items 先按 LLM 给出的 `topic`（空字符串变 `General`）分组，再写：

```text
outputs/<safe-course>_mock_exam.md   # 问题，不含答案
outputs/<safe-course>_answers.md     # 同样分组，含答案和 source/page
```

当前模板没有 multiple-choice/short-answer/applied 三段固定 section，也不会自动
生成题型或难度元数据。

---

## 命令与实际经过的层

| 命令 | 经过的主要层 | 写文件 |
| --- | --- | --- |
| `inspect` | Loader → Parser / cleaning report | 否 |
| `chunks` | Loader → Parser → Chunker | 否 |
| `ingest` | 1 → 2 → 3 → 4 → 5 | index DB |
| `search` | 4（query）→ 5 → 6 | 否 |
| `topics` | 5 → clustering | 否 |
| `coverage` | 5 → selection；有 topic 时也经过 4/6 | 否 |
| `mock` | 5 → selection → 7；有 topic 时也经过 4/6 | 两个 Markdown |
| `status` | 读取 5 的 profile/stats/sources | 否 |
| `embed-check` | 4 的 dimension + semantic probes | 否 |

当前 `_open_store()` 会先向 embedding endpoint 探测 dimension，再打开并校验
SQLite schema。因此表中不需要 embed query 的 index 命令（包括 `status`、
`topics`、无 topic 的 `coverage/mock`）目前仍要求 embedding endpoint 可达。

## 失败与可观察性

- Parser 的 `OverfitError` 在 ingestion 中按文件隔离，其他文件继续，结束时汇总；
- 某文件产生零个可用 chunk 也记为该文件失败；
- `inspect`、`chunks`、`ingest` 不会仅因这些已隔离的单文件失败自动返回非零；
- embedding/store 失败不在 per-file parser catch 内，会终止本次命令；
- `inspect --show-removed`、`chunks`、`search`、`coverage` 分别观察 extraction、
  chunking、retrieval 和 generation selection；
- `embed-check` 只打印相似度供人判断，没有自动 pass/fail threshold；
- `status` 显示 profile、文件/chunk 数，但不检查引用语义正确性。

## 自动化测试现状

`tests/test_parser.py` 当前只有 5 个 parser invariant tests：前四个用 synthetic
`Page` 覆盖页码保留、结构化 block 保护、labelled furniture 优先与断行连字符恢复；
第五个读取临时空文本文件并检查空文档报错。Loader、
Chunker、Embedder、Store、Retriever/Selection、Generator、CLI、真实 PDF backend
和端到端链路都尚无自动化覆盖。

## Roadmap（不是现状）

- OCR；
- lexical/hybrid search、re-ranking；
- `summary`、`compare`、`relate`；
- 多课程批量产物；
- section 提取与 section citation；
- remote source loader；
- 覆盖所有层的 unit/integration/end-to-end tests。
