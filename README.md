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
| Opt-in traces, strict citations, independent judge runner and offline replay | Judge calibration and quality baseline |

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
| `mock` | Selects material, calls the LLM, validates it and writes two Markdown files; opt-in `--trace` records a run-specific audit bundle | generation; index opening probes embedding dimension, and `--topic` also embeds the topic |
| `eval replay RUN_DIR` | Revalidates a local trace and writes a separate replay report; no settings, index or provider access | none |
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
                    [--material 0] [--trace]
overfit eval replay RUN_DIR
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

## Generation trace and offline replay

`mock --trace` is opt-in. It retains actual materials, visible requests/responses,
validation and citation changes under the configured `outputs_dir/eval/<run_id>/`.
Default `mock` output remains unchanged; traced exams do not overwrite it.

```bash
# Online: still calls embedding/generation services; run only when intended.
uv run overfit mock --course COURSE --questions 5 --trace
# Offline: use an existing recorded run; no original index or model required.
uv run overfit eval replay /absolute/path/to/outputs/eval/RUN_ID
```

The trace contains local copies of course content and must not be committed or
uploaded automatically. Replay writes into a new `replays/<replay_id>/` directory.
A report or exam file alone does not prove completion: replay validates the
terminal event and artifact hashes. A complete failed/interrupted run may replay
successfully while retaining its original failure status. PR 1 reports retain
`not_evaluated` semantic fields. PR 2 independently judges final retained items;
its default command only prepares tasks without loading model configuration or
making network calls. The existing 47-item golden set is unchanged.

```bash
# Offline preparation: writes a readable report, all semantic tasks unreviewed.
uv run overfit eval judge /absolute/path/to/outputs/eval/RUN_ID
# Explicit online execution only after choosing a judge and call budget.
# Requires independent JUDGE_MODEL, JUDGE_BASE_URL and JUDGE_API_KEY.
uv run overfit eval judge /absolute/path/to/outputs/eval/RUN_ID --execute --max-calls 6
# Offline recomputation from saved judge responses.
uv run overfit eval judge-replay /absolute/path/to/outputs/eval/RUN_ID/judgments/JUDGMENT_ID
```

Judge runs write separate `judgments/<UUID>/` directories, never replace the
generation trace, and print the report path. Each retained item has two separate
requests: answerability sees no generated-answer field, while support checks the
answer and cited page. Unknown, unreviewed and errored items remain in the metric
denominator. Optional `--max-items N` selects the first N retained items without
hiding unselected items. SDK retries are disabled; the global `--max-calls` cap
includes configured runner retries. A valid negative verdict is not retried.

The user completed a real three-question trace, offline replay, local-model pilots,
and an OpenRouter pilot. Raising the output budget to 4096 resolved truncation;
v2 still withheld complete model verdicts because two quotations used ellipses.
The **judge-v3 responsibility revision passed independent offline verification**: the model owns semantic
verdicts; code records technical completion and source diagnostics; substantive
ambiguity is routed to humans. Quotations are optional explanatory excerpts:
exact/whitespace matches may add source spans, but unmatched/ambiguous quotations
alone are informational, not reasons to erase verdicts or require manual review.

Reports separate **model verdicts**, **technical status**, and **human review**.
Unknown/partial verdicts, contradictions and substantive source problems create
pending review entries without changing model verdicts or completed-task counts.
A clear negative verdict does not automatically require human review. Model-wide
positive counts are not final approval. `--max-items 1` can finish **2/2 selected
tasks, 2/6 overall** with a completed technical run; unselected items stay visible.
`review_queue.jsonl` exports pending phases; adjudication entry is not implemented.
V3 replay rejects incompatible v1/v2 records and does not rewrite them. No model
is started as part of this offline revision; calibration remains incomplete.


### Independent judge challenges

Artificial control/mutant suites are **not generation traces or calibrated gold**.
The dedicated `eval judge-challenge` entry point is implemented and independently
verified under the
[challenge spec](/Users/silver/Documents/github/Overfit/docs/specs/EVAL-CHALLENGE-SPEC.md)
and [plan](/Users/silver/Documents/github/Overfit/docs/plans/EVAL-CHALLENGE-PLAN.md).
The [IFN580 suite](/Users/silver/Documents/github/Overfit/eval/judge_challenges/ifn580_v1/README.md)
contains three unchanged controls and four single-factor mutants. Usage:

```bash
# Offline preparation only: no model configuration/client/network.
uv run overfit eval judge-challenge eval/judge_challenges/ifn580_v1
# Explicit execution only after model and budget confirmation.
JUDGE_MAX_ATTEMPTS=1 uv run overfit eval judge-challenge \
  eval/judge_challenges/ifn580_v1 --execute --max-calls 14
```

`--max-items N` selects the first N cases; unselected cases remain visible and
paired controls are not automatically added. Each case requires two independent
requests. Output defaults to `outputs/eval/challenges/<UUID>/` relative to the
working directory; `--output-dir PATH` changes the parent. Annotations, IDs,
control/mutant identity and expected targets never enter model requests. Reports
compare raw model verdicts with separately labeled design intentions; they do not
automatically score detection success or accuracy. This record type is separate
from formal trace judges. Challenge replay/resume is not implemented, and formal
`judge-replay` rejects it. Independent verification passed **519 offline tests**,
Ruff and diff checks, eight additional blocked-network checks, and preservation
of 90 baseline files. The [offline prepare report](/Users/silver/Documents/github/Overfit/outputs/eval/challenges/47c16ac8-faed-450b-ae21-6acf7365272e/report.md)
contains seven cases, 0/14 evaluated phases and zero calls. No real challenge
model run or semantic calibration was performed; formal v3 replay compatibility
was independently checked without changing old records.

## Development and verification

```bash
uv sync  # the default dev group includes pytest and Ruff
uv run pytest -q
uv run ruff check .
```

The offline suite now covers parser invariants, generation tracing and fallback,
strict citation checks, writer failures, replay integrity, and fake-provider CLI
flows, plus judge schema/evidence validation, isolated inputs, global budgets,
fixed denominators and offline judgment replay. Tests do not establish real-model
generation quality or semantic support.
Real PDF backends, retrieval quality, and live provider behavior still require
separate verification. `inspect`, `chunks`, `search`, `coverage`, and `embed-check`
remain useful manual diagnostics.

Historical PR 1 verification used `/private/tmp/overfit-eval-trace-venv` with
Python 3.12.14 while the project environment was broken. The user subsequently
ran `uv run`, which rebuilt `.venv` with Python 3.12.13. Historical judge-v1 verification used this repaired
project environment: **371 offline tests passed**, Ruff and independent review
passed. The 2026-10-06 judge-v2 revision passed **404 offline tests**, Ruff,
independent review, and input-artifact preservation checks. These are historical
evidence. V3 independently passed **439 offline tests**, Ruff and diff checks,
plus 17 additional boundary checks and preservation of 65 baseline files. A
read-only diagnostic of saved responses yielded 2/2 selected tasks complete
(2/6 overall), one model-all-positive item and zero pending human reviews; two
ellipsis quotations remained informational. This is not a new online pilot or
semantic calibration, and old reports were not rewritten. No new model calls
were made; calibration remains incomplete. Evidence is maintained in [EVAL-JUDGE-PLAN.md](/Users/silver/Documents/github/Overfit/docs/plans/EVAL-JUDGE-PLAN.md);
the earlier [PR 1 record](/Users/silver/Documents/github/Overfit/docs/plans/EVAL-TRACE-PLAN.md) remains historical evidence.

## Documentation

| Document | Purpose |
| --- | --- |
| [ARCHITECTURE.md](./docs/ARCHITECTURE.md) | Current system and stage diagrams |
| [PIPELINE.md](./docs/PIPELINE.md) | Exact layer behavior, algorithms and data flow |
| [TECH-STACK.md](./docs/TECH-STACK.md) | Dependencies, optional components and deliberate exclusions |
| [MODELS.md](./docs/MODELS.md) | Embedding/generation contracts, safeguards and configuration |
| [EVALUATION.md](/Users/silver/Documents/github/Overfit/docs/EVALUATION.md) | Trace and judge workflows; pilot findings, judge-v3 responsibility revision and pending calibration; existing 47-item gold and later diagnostics |
| [EVAL-TRACE-SPEC.md](/Users/silver/Documents/github/Overfit/docs/specs/EVAL-TRACE-SPEC.md) | PR 1 contract: opt-in generation traces, strict citation checks, offline replay and acceptance criteria |
| [EVAL-TRACE-PLAN.md](/Users/silver/Documents/github/Overfit/docs/plans/EVAL-TRACE-PLAN.md) | PR 1 implementation milestones, offline test matrix and verification evidence |
| [EVAL-JUDGE-SPEC.md](/Users/silver/Documents/github/Overfit/docs/specs/EVAL-JUDGE-SPEC.md) | PR 2 contract: separate judge configuration, evidence, budgets and fixed-denominator metrics |
| [EVAL-CHALLENGE-SPEC.md](/Users/silver/Documents/github/Overfit/docs/specs/EVAL-CHALLENGE-SPEC.md) | Separate control/mutant execution contract and information isolation |
| [EVAL-CHALLENGE-PLAN.md](/Users/silver/Documents/github/Overfit/docs/plans/EVAL-CHALLENGE-PLAN.md) | Challenge implementation, offline acceptance and preservation evidence |
| [EVAL-JUDGE-PLAN.md](/Users/silver/Documents/github/Overfit/docs/plans/EVAL-JUDGE-PLAN.md) | PR 2 milestones, offline test evidence and real-pilot boundary |
