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

## 📖 文档（Documentation）

README 只讲「这是什么、能干嘛」。技术细节全部在 [`docs/`](./docs/) 下，按你想知道的问题挑一份看：

| 文档 | 回答什么问题 |
| --- | --- |
| [ARCHITECTURE.md](./docs/ARCHITECTURE.md) | 整体长什么样？四张图，一张总览 + 三个阶段各一张 |
| [PIPELINE.md](./docs/PIPELINE.md) | RAG 七层各自做什么、为什么需要、输入输出是什么 |
| [TECH-STACK.md](./docs/TECH-STACK.md) | 每一层用了什么技术、为什么这么选、刻意不用什么 |
| [MODELS.md](./docs/MODELS.md) | 两类模型的区别、怎么配、本地和 API 怎么切、开源分发怎么办 |

**第一次接触这个项目，推荐顺序：** ARCHITECTURE（看图建立整体印象）→ PIPELINE（理解每一层）→ TECH-STACK / MODELS（要动手时再看）。

---

> Overfit = AI-powered study content generator (CLI)
