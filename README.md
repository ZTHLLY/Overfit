# 📚 Overfit

> **Overfit is a CLI tool that transforms course materials into structured, shareable study artifacts — mock exams, summaries, and analyses — all grounded in your source documents.**

> *Don't underfit your exams.*

---

## 🧠 概述（Overview）

Overfit 是一个本地运行的命令行工具，面向学生的真实复习工作流。它把散落的课程资料（PDF / Markdown / slides）变成**结构化、可复用、可分享**的学习产物。

当前大多数同学的复习链路长这样：

1. 打开 ChatGPT / Gemini / NotebookLM 出题
2. 手动复制生成的内容
3. 粘贴到 Markdown 文件
4. 自己整理格式与结构
5. 再分享给同学

这个流程存在几个痛点：

- ❌ 重复劳动，每门课都要重来一遍
- ❌ 输出格式不稳定，每次都要人工整理
- ❌ 资料难以复用，散落在各个聊天记录里
- ❌ 不同课程之间难以统一风格

Overfit 的答案很简单：

> ✅ **一条命令 → 直接生成结构化的学习资料，可保存、可分享、可复用。**

Overfit 不是一个聊天工具，而是一条**内容生产流水线（content generation pipeline）**。它关心的不是"聊得顺不顺"，而是"产出的文件能不能直接拿去用"。

---

## ⚖️ 与 NotebookLM 的差异

| 能力              | NotebookLM | Overfit |
| ----------------- | :--------: | :-----: |
| 问答              |     ✅     |   ✅    |
| 模拟题生成        |     ✅     |   ✅    |
| 结构化输出        |     ❌     |   ✅    |
| 批量生成          |     ❌     |   ✅    |
| 自动化 / 脚本化   |     ❌     |   ✅    |
| 与本地项目结合    |     ❌     |   ✅    |
| 可复用 pipeline   |     ❌     |   ✅    |

**核心区别一句话：** NotebookLM 是"对话工具"，Overfit 是"内容生产工具"。

---

## 🎯 核心能力

Overfit 围绕四个基本能力构建：

- **Retrieval** — 从课程资料中检索相关片段
- **Generation** — 基于检索到的内容生成新产物
- **Structuring** — 输出严格结构化的 Markdown
- **Traceability** — 每条答案都能追溯到具体的 lecture / page / section

---

## 🚀 实现效果（What you get）

### 🟣 1. Mock Exam 生成

```bash
overfit mock --course IFN636
```

输出：

```
/outputs/
  IFN636_mock_exam.md
  IFN636_answers.md
```

**题目文件示例：**

```markdown
# IFN636 Mock Exam

## Section A: Multiple Choice
1. What is overfitting?

## Section B: Short Answer
2. Explain bias-variance tradeoff.

## Section C: Applied Questions
3. How to avoid overfitting?
```

**答案文件示例：**

```markdown
# IFN636 Mock Exam Answers

## Q1
Answer: ...
Source: lecture3.pdf (page 12)

## Q2
Explanation: ...
Source: lecture5.pdf
```

亮点：

- ✔️ 题目与答案分离，可以先自测再对答案
- ✔️ 每一题都带源文件定位，方便回看原文
- ✔️ 直接是 Markdown，复制粘贴就能分享

---

### 🔵 2. 结构化总结

```bash
overfit summary --course IFN636
```

输出一份按固定骨架组织的复习笔记：

```markdown
## Key Topics
## Important Concepts
## Common Pitfalls
## Exam Focus
```

---

### 🟢 3. 跨课程分析

```bash
overfit compare "overfitting" --course IFN636,CAB432
```

对比同一个概念在不同课程中的讲法、侧重与考点。

---

### 🟡 4. Assignment / Project 关联分析

```bash
overfit relate --course IFN636 --project ./assignment1
```

能力：

- 分析作业中用到了哪些课程知识点
- 指出**缺失**的知识点（Underfit）
- 判断实现是否覆盖课程重点

这是 Overfit 与其他学习工具最大的差异化能力 —— 把"学"和"做"连起来。

---

## 🧭 典型使用场景

**📌 期末冲刺**

```bash
overfit mock --courses IFN636,CAB432
```

**📌 平时复习**

```bash
overfit summary --course IFN636
```

**📌 作业辅助**

```bash
overfit relate --course IFN636 --project ./assignment
```

---

## 🌱 项目价值

- **技术上** — RAG 与 AI 工程实践的完整落地
- **实用上** — 真正解决"复习资料怎么来"的问题
- **社交上** — 产出即 Markdown，天然可分享

---

## 🧠 设计理念

- 不做聊天工具，做内容生成 pipeline
- 强调结构化输出胜过自由对话
- 强调可控性与可复用性
- 所有产物必须可追溯到源文件

---

## 🛠️ 技术栈（Tech Stack）

Overfit 是一个轻量、自己手写、透明可控的 RAG pipeline。整套栈的气质是：**单 provider、单文件存储、不依赖重型框架**，确保每一层都看得见、改得动。

| 层 | 选型 | 说明 |
| --- | --- | --- |
| **语言** | Python 3.11+ | RAG 生态最完整 |
| **包管理** | `uv` | 现代、快、替代 pip/poetry |
| **CLI 框架** | Typer | 基于类型注解的命令行 |
| **配置管理** | pydantic-settings + `.env` | 管理 Gemini API key 等配置 |
| **PDF 解析** | pypdf | 轻量，输出带 page 元数据的文本 |
| **Chunking** | 自己写 | 按段落 / 固定 token 切，保留 source + page 元数据 |
| **Embedding** | Gemini `text-embedding-004` | 与生成模型同生态，减少 provider |
| **向量存储** | SQLite + `sqlite-vec` | 单文件、零运维、可随项目 commit |
| **检索** | 余弦相似度 top-k | MVP 不做 re-ranking |
| **LLM 生成** | Gemini 2.5 Pro / Flash | `--cheap` flag 切 Flash |
| **结构化输出** | Gemini JSON schema | 保证题目 / 答案 / 来源三段式稳定 |
| **Prompt 模板** | Jinja2 | 模板与代码分离 |
| **缓存** | SQLite（与向量库共用一个 db 文件） | 解析结果 + embedding 都缓存，避免重复烧钱 |

**刻意不用的东西：**

- ❌ **LangChain / LlamaIndex** — 过度抽象，挡住 RAG 细节，也会挡住"溯源"这个核心能力
- ❌ **多 LLM provider 抽象** — MVP 只有 Gemini，YAGNI
- ❌ **re-ranking / hybrid search / query rewriting** — 留给 MVP 之后的进阶路线

---

## 🧩 RAG 架构分层（RAG Pipeline Layers）

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

**职责：** 把每个 chunk 的文本 → **一个向量**（一串数字，比如 768 维）。

**为什么要向量：** 向量可以做"相似度计算"。两段文字语义越相近，它们的向量在空间里就越接近。这是 RAG 能"找到相关内容"的数学基础。

**输入：** `Chunk.text`（字符串）
**输出：** `list[float]`，比如 `[0.023, -0.451, 0.118, ...]`（768 个数字）

**在 Overfit 里：** 调 Gemini 的 `text-embedding-004` API，把 chunk 文本发过去，拿回向量。

**关键点：**
- Embedding 模型**建库时用哪个，查询时必须用同一个**，否则向量空间对不上
- Embedding 只跑一次，结果存起来 —— 这是省钱的关键

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
    ↓ (发给 Gemini，带 JSON schema 约束)
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

**在 Overfit 里：** Gemini 2.5 Pro + JSON schema + Jinja2 模板。

**关键点：**
- **JSON schema 是溯源的保险** —— 强制模型输出 source 字段，不能偷懒
- **Prompt 模板和生成逻辑分离** —— 以后 `summary` / `compare` / `relate` 只换模板，代码不动

---

### 📊 整条链路对应到技术栈

| RAG 层 | 对应技术栈 |
| --- | --- |
| 1. Loader | Python stdlib（`pathlib`）|
| 2. Parser | **pypdf** |
| 3. Chunker | 自己写 |
| 4. Embedder | **Gemini `text-embedding-004`** |
| 5. Vector Store | **SQLite + sqlite-vec** |
| 6. Retriever | 自己写（余弦相似度 top-k） |
| 7. Generator | **Gemini 2.5 Pro** + **Jinja2** + **JSON schema** |

---

### 🎯 MVP 范围对照这 7 层

MVP = **7 层全部实现，但每层只做最简单的一版**：

- Loader：只支持本地 PDF 目录
- Parser：只支持 PDF，只抽文本 + page
- Chunker：固定大小 + 固定重叠
- Embedder：单次调用 Gemini，不做批量优化
- Vector Store：一张 SQLite 表
- Retriever：纯 top-k，没有过滤 / 重排
- Generator：只实现 `mock` 一个命令，一个 prompt 模板

**跑通这 7 层 = RAG 入门毕业。** 之后的所有进阶（re-ranking、hybrid search、Ragas 评测、LangChain 对照版）都是在这 7 层上做增强或替换，骨架不变。

---

> Overfit = AI-powered study content generator (CLI)
