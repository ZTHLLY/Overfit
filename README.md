# 📚 Overfit

**English** | [简体中文](README.zh-CN.md)

> *Don't underfit your exams.*

Overfit is a small tool that turns a folder of lecture slides and course notes
into a practice exam.
Run one command inside a course folder and you get two Markdown files: a
question paper, and an answer key that explains every option and cites the
page it came from.

Every answer points back to your own course material, so you can check it
instead of taking it on trust.

## What you get

Generated from a real machine-learning unit. The question paper shows only the
question and the options:

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

The answer key, a separate file, explains every option, the wrong ones
included, and cites a page for each:

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

## Quickstart

**1. Install** (requires [uv](https://docs.astral.sh/uv/)):

```bash
uv tool install git+https://github.com/ZTHLLY/Overfit
```

**2. Set up the models.** Course material is indexed locally with
[Ollama](https://ollama.com), and questions are written by DeepSeek:

```bash
ollama pull bge-m3

mkdir -p ~/.config/overfit
echo "LLM_API_KEY=sk-your-deepseek-key" > ~/.config/overfit/.env
```

**3. Generate an exam** from inside a course folder:

```bash
cd ~/Documents/IFN580
overfit mock
```

That's it. The folder now contains `IFN580_mock_exam.md` and
`IFN580_answers.md`. The default is 30 questions; use `overfit mock -q 10` for
fewer.

Notes:

- Sub-folders (`week1/`, `week2/`, …) are read recursively.
- The first run builds a search index in a hidden `.overfit/` folder. Later
  runs only re-read files that changed, so they start almost immediately.
- Running again overwrites the previous exam.

## Step by step

`overfit mock` runs the whole pipeline in one go. The commands below run each
stage separately, so you can inspect what every stage produces before paying
for the next one. Each command works inside a course folder, or anywhere with
`--course NAME` (which reads `COURSES_DIR/NAME`).

| Step | Command | What it shows |
| --- | --- | --- |
| 1. Parse | `overfit inspect --show-removed` | The text extracted from each PDF, and what cleaning removed |
| 2. Chunk | `overfit chunks --spanning` | How documents are split, including chunks that cross a page |
| 3. Index | `overfit ingest` | Embeds and stores the chunks; unchanged files are skipped |
| 4. Check | `overfit status` | What the index contains and the settings it was built with |
| 5. Explore | `overfit topics` | The course's main topics, ranked by how many files cover them |
| 6. Search | `overfit search "bias variance"` | Retrieval results with vector and rerank scores |
| 7. Preview | `overfit coverage` | The exact passages `mock` would be given |
| 8. Generate | `overfit mock` | The question paper and answer key |

Useful options:

- `overfit mock --topic "decision trees" -q 10` focuses the exam on one subject.
  This option and `search` rerank results through OpenRouter, so they need
  `OVERFIT_RERANK_API_KEY` in `~/.config/overfit/.env`.
- `overfit embed-check` checks that the embedding model is reachable and works
  across languages.
- `overfit ingest --rebuild` is needed after changing the embedding model or the
  chunk settings.

Run `overfit <command> --help` for every option. All settings are documented in
[`.env.example`](.env.example).
