# 📚 Overfit

[English](README.md) | **简体中文**

> *Don't underfit your exams.*

Overfit 是把一个装满课件和课程笔记的文件夹变成一套模拟考试的小工具。在课程文件夹里运行一条命令，就会得到两个 Markdown 文件：一份试卷，一份答案。答案会解释每个选项，并注明出自哪个文件的哪一页。

每个答案都能追溯到你自己的课程材料，可以自己核对，不必盲目相信。

## 生成效果

下面的题目由一门真实的机器学习课程生成。试卷只显示题目和选项：

```markdown
## Question 2

A company wants a generative AI system that can retrieve relevant information
from private, unstructured documents such as HR policies and legal documents
and produce holistic summaries. Which application does this describe?

**A.** Support bots
**B.** Personalization engines
**C.** Knowledge summaries
**D.** Model accuracy estimation
```

答案是单独的文件，包括错误选项在内，每个选项都有解释和引用页码：

```markdown
**Correct answer:** C. Knowledge summaries

**C.** Knowledge summaries
Knowledge summaries use generative AI to create holistic summaries by quickly
retrieving relevant information from private, unstructured documents such as
HR policies and legal documents.
> Source: **week10/gen_ai.pdf p78**

**D.** Model accuracy estimation
Model accuracy estimation compares known test labels with predicted results to
estimate classification performance; it is a model-evaluation step, not a
generative AI application for document summaries.
> Source: **week3/supervised_ml_basic.pdf p17**
```

## 快速开始

**1. 安装**（需要先装好 [uv](https://docs.astral.sh/uv/)）：

```bash
uv tool install git+https://github.com/ZTHLLY/Overfit
```

**2. 配置模型。** 课程材料用本地 [Ollama](https://ollama.com) 建立索引，题目由 DeepSeek 生成：

```bash
ollama pull bge-m3

mkdir -p ~/.config/overfit
echo "LLM_API_KEY=sk-你的deepseek-key" > ~/.config/overfit/.env
```

**3. 进入课程文件夹，生成试卷：**

```bash
cd ~/Documents/IFN580
overfit mock
```

完成后，文件夹里会多出 `IFN580_mock_exam.md` 和 `IFN580_answers.md`。默认出 30 题，想少出一些可以用 `overfit mock -q 10`。

说明：

- 子文件夹（`week1/`、`week2/` 等）会递归读取。
- 第一次运行时会在隐藏文件夹 `.overfit/` 里建立检索索引。之后再运行，只会重新读取有改动的文件，基本马上就能开始出题。
- 重复运行会覆盖上一次的试卷。

## 分步运行

`overfit mock` 会一次跑完整条流程。下面这些命令把每个阶段拆开单独运行，方便你在进入下一阶段（并为它付费）之前，先检查当前阶段的产出。每条命令都可以在课程文件夹里直接运行，也可以在任意位置加上 `--course NAME` 运行（读取 `COURSES_DIR/NAME`）。

| 步骤 | 命令 | 作用 |
| --- | --- | --- |
| 1. 解析 | `overfit inspect --show-removed` | 查看每个 PDF 提取出的文本，以及清洗时删掉了什么 |
| 2. 分块 | `overfit chunks --spanning` | 查看文档如何切分，包括跨页的块 |
| 3. 建索引 | `overfit ingest` | 对文本块做嵌入并入库，内容没变的文件会跳过 |
| 4. 检查 | `overfit status` | 查看索引内容，以及建索引时用的配置 |
| 5. 探索 | `overfit topics` | 列出课程的主要主题，按覆盖的文件数排序 |
| 6. 检索 | `overfit search "bias variance"` | 查看检索结果，以及向量和 rerank 两种得分 |
| 7. 预览 | `overfit coverage` | 查看 `mock` 实际会拿到的材料段落 |
| 8. 生成 | `overfit mock` | 生成试卷和答案 |

常用选项：

- `overfit mock --topic "decision trees" -q 10`：只围绕一个主题出题。这个选项和 `search` 都会通过 OpenRouter 做 rerank，需要在 `~/.config/overfit/.env` 里设置 `OVERFIT_RERANK_API_KEY`。
- `overfit embed-check`：检查嵌入模型能否连通，以及跨语言检索是否正常。
- `overfit ingest --rebuild`：更换嵌入模型或分块参数后，需要用它重建索引。

每条命令的完整选项见 `overfit <command> --help`，全部配置项见 [`.env.example`](.env.example)。
