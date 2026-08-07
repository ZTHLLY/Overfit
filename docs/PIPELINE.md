# 🧩 RAG 七层详解（Pipeline Layers）

> 逐层讲清楚每一层做什么、为什么需要它、以及它的输入输出。配图见 [ARCHITECTURE.md](./ARCHITECTURE.md)，选型理由见 [TECH-STACK.md](./TECH-STACK.md)。


Overfit 的核心是一条完整的 RAG 链路。RAG = **Retrieval-Augmented Generation**，核心思想是：**不让 LLM 凭空回答，而是先从你自己的资料里检索相关内容，再让 LLM 基于这些内容生成答案。**

整条链路可以拆成 **7 个层**，每一层都是 RAG 的标准组件：

```
┌─────────────────────────────────────────────────────────────┐
│                       Ingestion（建库阶段）                   │
├─────────────────────────────────────────────────────────────┤
│  1. Loader      →  读取 PDF / Markdown / slides              │
│  2. Parser      →  提取带 page / section 元数据的文本         │
│  3. Chunker     →  切成小块，保留来源信息                      │
│  4. Embedder    →  每块文本 → 向量（数字数组）                 │
│  5. Vector Store→  向量 + 原文 + 元数据存起来                 │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│                      Query（使用阶段）                        │
├─────────────────────────────────────────────────────────────┤
│  6. Retriever   →  query → 向量 → 找最相似的 top-k 块          │
│  7. Generator   →  把 top-k 块拼进 prompt → LLM → 结构化输出  │
└─────────────────────────────────────────────────────────────┘
```

这 7 层分两个阶段：**建库阶段**（Ingestion）只跑一次，结果缓存；**使用阶段**（Query）每次跑命令都会触发。下面一层一层讲清楚。

---

### 🟣 1. Loader（加载层）

**职责：** 把课程资料从磁盘读进内存。

**输入：** 一个文件路径或目录（比如 `courses/IFN636/`）
**输出：** 原始文件字节流

这一层只负责"找到文件、读进来"，不做任何解析。听起来废话，但单独分出来是为了以后支持多种来源（本地目录 / URL / Google Drive / Notion 导出）时，只改这一层，下面的都不动。

**在 Overfit 里：** 就是一个简单的"遍历目录、收集 PDF 文件"的函数。

---

### 🟣 2. Parser（解析层）

**职责：** 把原始文件（PDF 字节流）转成**带元数据的结构化文本**。

**输入：** PDF 文件
**输出：** 一个数据结构，大致是：

```python
ParsedDocument(
    source="lecture3.pdf",
    pages=[
        Page(number=1, text="Lecture 3: Overfitting..."),
        Page(number=2, text="Bias-variance tradeoff..."),
        ...
    ]
)
```

**关键点：** 元数据（source、page、section）必须从这一层就带上，而且一路跟到最后的答案里。**这就是 Overfit "溯源" 能力的起点。** 如果这一层丢了 page 号，后面任何层都补不回来。

**在 Overfit 里：** 用 pypdf 把每一页文本抽出来，每页都带 page number。

---

### 🟣 3. Chunker（分块层）

**职责：** 把一整份文档切成**更小的块**（chunks），供后续做 embedding 和检索。

**为什么要切：**
- LLM 的 context window 有限，不可能每次把整本书塞进去
- 检索需要"粒度" —— 如果整本书是一个向量，你搜"什么是 overfitting"会命中整本书，毫无意义
- 小块 = 检索更精准，大块 = 上下文更完整。这是 RAG 调优的核心 tradeoff 之一

**输入：** `ParsedDocument`
**输出：** 一堆 `Chunk`：

```python
Chunk(
    id="lecture3_p12_c2",
    text="Overfitting happens when a model learns noise...",
    source="lecture3.pdf",
    page=12,
    section="3.2 Bias-Variance"  # 可选
)
```

**切分策略**（MVP 里自己写一个简单的就行）：
- **固定大小**：每 500 tokens 一块，重叠 50 tokens（最简单）
- **按段落**：按空行切（更自然）
- **递归切分**：优先按段落，段落过长就再切（工业标准）

MVP 先用最简单的固定大小切分，跑通再说。

---

### 🟣 4. Embedder（向量化层）

**职责：** 把每个 chunk 的文本 → **一个向量**（一串数字，1024 维）。

**为什么要向量：** 向量可以做"相似度计算"。两段文字语义越相近，它们的向量在空间里就越接近。这是 RAG 能"找到相关内容"的数学基础。

**输入：** `Chunk.text`（字符串）
**输出：** `list[float]`，比如 `[0.023, -0.451, 0.118, ...]`（1024 个数字）

**在 Overfit 里：** 调本地 Ollama 的 `bge-m3`，走 OpenAI 兼容的 `/v1/embeddings` 端点。

**为什么选 bge-m3：**
- **8K 上下文窗口** —— 很多 embedding 模型只吃 512 tokens，会把超长 chunk 直接截断且不报错，等于把切分策略锁死在 512 以内。8K 把这个约束解除了
- **跨语言检索** —— 课件是英文，但提问经常是中文（"出一套关于过拟合的题"）。bge-m3 覆盖 100+ 语言且专为跨语言检索训练，英文向的模型在这里会明显吃亏
- **本地免费** —— 1.2GB，MIT 协议，不联网

**关键点：**
- Embedding 模型**建库时用哪个，查询时必须用同一个**，否则向量空间对不上。因此第 4 层和第 6 层在代码里**共用同一个 `embed()` 函数**，让错误的写法根本不方便写出来
- Embedding 只跑一次，结果按文件 content hash 缓存 —— 这是省时间的关键

---

### 🟣 5. Vector Store（向量存储层）

**职责：** 把 chunks + 向量 + 元数据**一起存起来**，并支持"给我一个向量，找出最相似的 top-k"。

**存什么：**
```
chunk_id | text | source | page | embedding (向量列)
---------|------|--------|------|------------------
l3_p12_c2| ...  |lect3..|  12  | [0.023, -0.451,...]
```

**支持的操作：**
- `add(chunks, embeddings)` — 写入
- `search(query_embedding, top_k=5)` — 查询最相似

**在 Overfit 里：** 用 SQLite + sqlite-vec 扩展。一个 `.db` 文件装下一切，零运维。工业界对应的是 pgvector / Pinecone / Qdrant，但 API 模式是完全一样的。

**到这里 Ingestion 阶段结束。** 1–5 层只跑一次，建完库就不动了。

---

### 🔵 6. Retriever（检索层）

**职责：** 拿到用户的 query（比如 `"出一套关于 overfitting 的题"`），找出课程里**最相关**的 top-k 个 chunks。

**流程：**

```
query 字符串
    ↓ (用同一个 Embedder)
query 向量
    ↓ (传给 Vector Store)
top-5 最相似的 chunks（带原文 + 来源元数据）
```

**输入：** `query: str`
**输出：** `list[Chunk]`，比如 5 个最相关的片段

**在 Overfit 里：** 简单的余弦相似度 top-k。MVP 不做 re-ranking、不做 hybrid search。

**关键点：** 这是 RAG 效果好坏的**第一个瓶颈**。如果检索层找错了 chunks，后面 LLM 再强也没用 —— 因为它根本没看到对的材料。

---

### 🔵 7. Generator（生成层）

**职责：** 把检索到的 chunks + 用户意图（"出一套 mock exam"）拼成一个 prompt，发给 LLM，拿回结构化答案。

**流程：**

```
top-k chunks + prompt 模板 (Jinja2)
    ↓
最终 prompt:
    "你是一个出题助手。基于以下课程材料出 10 道题。
     每道题必须引用 source + page。
     材料：
     [chunk 1: from lecture3.pdf page 12] Overfitting happens...
     [chunk 2: from lecture5.pdf page 8]  Bias-variance...
     ..."
    ↓ (发给 LLM，带 JSON schema 约束)
结构化输出:
    {
      "questions": [
        {"q": "What is overfitting?", "answer": "...", "source": "lecture3.pdf p12"},
        ...
      ]
    }
    ↓ (渲染到 Markdown 模板)
IFN636_mock_exam.md + IFN636_answers.md
```

**在 Overfit 里：** 本地 27B（Ollama）或 DeepSeek + JSON schema + Jinja2 模板，通过 `.env` 里的 `base_url` 切换。

**关键点：**
- **JSON schema 是溯源的保险** —— 强制模型输出 source 字段，不能偷懒。比在 prompt 里写"请不要编造"可靠得多：前者是格式层面的硬约束，后者只是一句祈使句
- **本地模型遵守 schema 的能力较弱**，所以生成后必须走一遍 pydantic 校验，失败就重试。这层兜底写了一劳永逸
- **Prompt 模板和生成逻辑分离** —— 以后 `summary` / `compare` / `relate` 只换模板，代码不动
- **生成模型是唯一可以自由更换的组件** —— 同一套材料、同一个 prompt，本地和 DeepSeek 各跑一遍直接对比，这个对比本身就是项目里很有说服力的一部分

---

### 📊 整条链路对应到技术栈

| RAG 层 | 对应技术栈 | 联网？ |
| --- | --- | :---: |
| 1. Loader | Python stdlib（`pathlib`）| ❌ |
| 2. Parser | **pypdf**（纯本地库） | ❌ |
| 3. Chunker | 自己写（按字符数近似） | ❌ |
| 4. Embedder | **Ollama + `bge-m3`** | ❌ 本地 |
| 5. Vector Store | **SQLite + sqlite-vec** | ❌ |
| 6. Retriever | 自己写（余弦 top-k）+ **同一个 `embed()`** | ❌ 本地 |
| 7. Generator | **本地 27B / DeepSeek** + **Jinja2** + **JSON schema** | ⚠️ 可选 |

**整条链路只有第 7 层可能联网**，而且是可选的。前六层全部离线，意味着建库、检索、调试都不花钱、不限流、不依赖网络。

---

### 🎯 MVP 范围对照这 7 层

MVP = **7 层全部实现，但每层只做最简单的一版**：

- Loader：只支持本地 PDF 目录
- Parser：只支持 PDF，只抽文本 + page
- Chunker：固定大小 + 固定重叠，按字符数近似（不做精确 token 计数）
- Embedder：串行调用 Ollama，不做批量优化
- Vector Store：一张 chunks 表 + 一张 meta 表
- Retriever：纯 top-k，没有过滤 / 重排
- Generator：只实现 `mock` 一个命令，一个 prompt 模板，全程本地模型

**推进方式：先打通最细的端到端链路**（1 个 PDF、最笨的切法、top-k=3、2 道题），七层全部跑通看到真实产物，再回头加厚每一层。集成风险最早暴露，好过每层各自做到完美却在最后才发现接不上。

**跑通这 7 层 = RAG 入门毕业。** 之后的所有进阶（re-ranking、hybrid search、Ragas 评测、LangChain 对照版）都是在这 7 层上做增强或替换，骨架不变。
