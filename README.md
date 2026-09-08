# 📚 Overfit

> **A local-first CLI that turns course documents into searchable indexes and
> grounded practice exams.**

> *Don't underfit your exams.*

Overfit is a hand-written, seven-layer RAG pipeline. It recursively reads a
course directory, preserves file/page provenance while cleaning and chunking
the text, stores normalized embeddings in a per-course SQLite database, and
uses selected passages to produce a question paper plus a separate answer key.

The project is a **content-production CLI**, not a chat interface. The files it
writes are intended to be inspected, saved and shared. A citation makes an
answer checkable; it does not make the answer automatically correct.

## Current status

The table below separates shipped behavior from product direction. Commands in
the Roadmap column do **not** exist in the current CLI.

| Implemented now | Roadmap (not implemented) |
| --- | --- |
| Recursive local document discovery | `summary` study notes |
| PDF parsing with `pypdf`; optional Docling PDF backends | `compare` across course indexes |
| Opt-in Markdown and plain-text parsing | `relate` course material to a project/assignment |
| Cleaning diagnostics and chunk previews | Multi-course generation in one command |
| OpenAI-compatible embedding and generation endpoints | OCR for scanned PDFs |
| SQLite + `sqlite-vec` indexes, one file per course | Remote sources such as Drive/Notion/URLs |
| Direct semantic search, topic clustering and coverage preview | Hybrid search and re-ranking |
| Grounded mock exams with Markdown question/answer files | More generated artifact types |

## How it works

```text
courses/<course>/
    │
    ├─ Loader   ── recursive, deterministic file discovery
    ├─ Parser   ── page-aware extraction and auditable cleaning
    ├─ Chunker  ── boundary-aware chunks with source/page metadata
    ├─ Embedder ── normalized vectors from an OpenAI-compatible API
    └─ Store    ── index/<course>.db (SQLite + sqlite-vec)
                           │
                    Retriever / selection
                           │
                    Generator + validation
                           │
             outputs/<course>_mock_exam.md
             outputs/<course>_answers.md
```

Important boundaries:

- The index records the embedding model, vector dimension, chunk size,
  overlap, PDF backend and schema version. A mismatch stops the command and
  requires `ingest --rebuild` rather than silently mixing incompatible data.
  For backward compatibility, a meta key that is completely absent from an
  older index is currently accepted; only a stored value that differs fails.
- The generation model is not part of the index and can be changed without
  rebuilding it.
- Provenance currently means a relative source path and a 1-based page or
  page range. Although the storage schema has a `section` column, the current
  parser/chunker does not populate it.

See [PIPELINE.md](./docs/PIPELINE.md) for the exact algorithms and data
contracts.

## Requirements and installation

- Python **3.12 or newer**
- [`uv`](https://docs.astral.sh/uv/)
- An OpenAI-compatible embeddings endpoint
- An OpenAI-compatible chat-completions endpoint only when running `mock`

The default configuration expects Ollama on `http://localhost:11434/v1` with
`bge-m3` for embeddings and `qwen3.6:27b` for generation. Model availability is
managed by the endpoint, not by this Python package.

```bash
uv sync
cp .env.example .env

# If the default Ollama configuration is being used:
ollama pull bge-m3
ollama pull qwen3.6:27b
```

The normal install stays lightweight. For layout-aware PDF conversion:

```bash
uv sync --extra docling
# Use the broad fallback only if the narrow optional install is insufficient:
uv sync --extra docling-full
```

Docling OCR is deliberately disabled in the current parser, including with
the optional extra.

## Prepare a course

By default, put files below `courses/<course>/`:

```text
courses/
└── IFN580/
    ├── week01/introduction.pdf
    └── week02/regression.pdf
```

Discovery is recursive. Hidden paths and known junk directories are skipped.
The default `EXTENSIONS=.pdf` indexes PDFs only. The code also supports
`.md`, `.markdown` and `.txt`; opt into them explicitly if the directory does
not contain duplicate PDF/text versions:

```dotenv
EXTENSIONS=.pdf,.md,.markdown,.txt
```

Markdown and text files have no real page structure, so each is represented as
page 1 and cited that way. A command that reads source files can use
`--path /some/directory` instead of `COURSES_DIR/<course>`; the generated index
and output names still come from `--course`.

## Recommended workflow

Inspect each lossy stage before paying for embeddings or generation:

```bash
# 1. Check endpoint reachability, vector width and rough semantic behavior.
uv run overfit embed-check

# 2. Read real parser output; optionally audit every removed line.
uv run overfit inspect --course IFN580 --show-removed

# 3. Preview chunk sizes, boundaries, provenance and page-spanning chunks.
uv run overfit chunks --course IFN580 --spanning

# 4. Build or incrementally update the per-course index.
uv run overfit ingest --course IFN580

# 5. Inspect the index and retrieval/selection before generating.
uv run overfit status --course IFN580
uv run overfit search "bias variance tradeoff" --course IFN580 --top-k 8
uv run overfit topics --course IFN580
uv run overfit coverage --course IFN580 --count 12

# 6. Generate the only currently implemented artifact.
uv run overfit mock --course IFN580 --questions 10
```

`ingest` skips a source path only when its current SHA-256 content hash matches
the stored hash. Use `--force` after changing cleaning behavior that is not
represented in the index profile. Use `--rebuild` after changing the embedding
model, chunk settings or PDF backend, and when source files have been renamed
or removed: incremental ingestion does not prune sources that disappeared from
disk.

## CLI reference

Run `uv run overfit <command> --help` for every option.

| Command | Current behavior | Model calls |
| --- | --- | --- |
| `inspect` | Reports extraction statistics, samples the longest pages, and can show cleaning removals | none |
| `chunks` | Parses and previews actual chunks before indexing | none |
| `ingest` | Parses, chunks, embeds and writes/updates one course index | embedding |
| `search QUERY` | Embeds a query and prints nearest chunks with cosine scores | embedding |
| `mock` | Selects material, calls the LLM, validates it and writes two Markdown files | generation; index opening probes embedding dimension, and `--topic` also embeds the topic |
| `topics` | Clusters stored vectors and ranks topics by distinct source-file coverage | embedding dimension probe only; no query embedding or LLM |
| `coverage` | Shows the passages `mock` would receive; optional topic focus uses vector search + MMR | dimension probe; `--topic` also embeds the query; no LLM |
| `status` | Shows index profile, counts and sources | embedding dimension probe only; no LLM |
| `embed-check` | Prints vector dimension and similarity probes, including Chinese-to-English retrieval | embedding |

Current command signatures (defaults in parentheses):

```text
overfit inspect     --course/-c COURSE [--path/-p PATH] [--samples 2]
                    [--chars 600] [--show-removed]
overfit chunks      --course/-c COURSE [--path/-p PATH] [--show 5]
                    [--spanning]
overfit ingest      --course/-c COURSE [--path/-p PATH] [--force] [--rebuild]
overfit search      QUERY --course/-c COURSE [--top-k/-k 5] [--chars 300]
overfit mock        --course/-c COURSE [--questions/-q 10] [--topic TEXT]
                    [--material 0]
overfit topics      --course/-c COURSE [--count/-n 12] [--questions 10]
overfit coverage    --course/-c COURSE [--count/-n 12] [--topic TEXT]
                    [--chars 180]
overfit status      --course/-c COURSE
overfit embed-check
```

Running `overfit` without a subcommand currently prints `hello overfit`;
running it with `--help` lists the commands above.

Current implementation detail: opening any existing index first probes the
configured embedding model for its dimension. Therefore even `status`,
`topics`, whole-course `coverage`, and `mock` currently require the embedding
endpoint to be reachable, despite not embedding a semantic query themselves.

### Mock selection and output

Without `--topic`, `mock` clusters stored vectors into at most 12 topics,
allocates passages by topic weight, and chooses representative, non-duplicate
chunks. With `--topic`, it retrieves a candidate pool and applies Maximal
Marginal Relevance. By default it requests a material budget twice the question
count; selection may return fewer passages when the index has too little
usable material. Override the budget with `--material`.

The model may honestly return fewer questions. Each accepted item has this
validated shape:

```json
{
  "topic": "Bias and variance",
  "question": "...",
  "answer": "...",
  "source": "week03/lecture.pdf",
  "page": 12
}
```

Questions are grouped by the model-provided topic in the paper. Answers are
grouped the same way and carry `Source: <file>, page <n>`. The generator drops
items whose source/page is not among the supplied passages (and repairs page
near-misses of at most two pages). This verifies that a citation refers to
provided material; users should still check whether the cited page truly
supports the answer. The current schema permits fewer questions but does not
set a maximum list length, so a model that ignores the requested upper bound is
not truncated by code.

## Configuration

Configuration comes from field defaults, then `.env`, then process environment
variables. Per-command flags override only the corresponding behaviors exposed
by that command.

| Variable | Default | Used for |
| --- | --- | --- |
| `COURSES_DIR` | `courses` | Root containing one directory per course |
| `OUTPUTS_DIR` | `outputs` | Generated Markdown |
| `INDEX_DIR` | `index` | Per-course SQLite files |
| `EXTENSIONS` | `.pdf` | Comma-separated discovery filter |
| `PDF_BACKEND` | `pypdf` | `pypdf`, `docling`, or `docling+formula` |
| `CHUNK_SIZE` | `250` | Approximate tokens (implemented as 4 characters/token) |
| `CHUNK_OVERLAP` | `25` | Approximate overlapping tokens |
| `EMBED_BASE_URL` | `http://localhost:11434/v1` | Embeddings API |
| `EMBED_API_KEY` | `ollama` | Embeddings API credential |
| `EMBED_MODEL` | `bge-m3` | Embedding model identifier |
| `LLM_BASE_URL` | `http://localhost:11434/v1` | Chat-completions API |
| `LLM_API_KEY` | `ollama` | Generation API credential |
| `LLM_MODEL` | `qwen3.6:27b` | Generation model identifier |
| `TEMPERATURE` | `0.3` | Generation sampling temperature |
| `MAX_RETRIES` | `3` | Validation retries; total attempts are normally 4 |
| `REQUEST_TIMEOUT` | `900` | Generation request timeout in seconds |

`TOP_K=5` and `NUM_QUESTIONS=10` exist in the settings model, but current CLI
defaults are hard-coded to the same values; use `search --top-k` and
`mock --questions` to override them reliably.

## Development and verification

```bash
uv sync  # the default dev group includes pytest and Ruff
uv run pytest -q
uv run ruff check .
```

Current verified result: `5 passed` and `All checks passed!` from Ruff.
The automated suite currently contains **five parser-cleaning invariant
tests**: four use synthetic `Page` objects, while the blank-extraction case
reads a temporary text file. There are no automated tests yet for
the loader, chunker, embedding client, store, selection/retrieval, generator,
CLI, real PDF backends, or an end-to-end run. `inspect`, `chunks`, `search`,
`coverage`, and `embed-check` are therefore important manual diagnostics, not
substitutes for a broader test suite.

## Documentation

| Document | Purpose |
| --- | --- |
| [ARCHITECTURE.md](./docs/ARCHITECTURE.md) | Current system and stage diagrams |
| [PIPELINE.md](./docs/PIPELINE.md) | Exact layer behavior, algorithms and data flow |
| [TECH-STACK.md](./docs/TECH-STACK.md) | Dependencies, optional components and deliberate exclusions |
| [MODELS.md](./docs/MODELS.md) | Embedding/generation contracts, safeguards and configuration |
