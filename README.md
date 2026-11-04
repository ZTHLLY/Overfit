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
| Dense Top-40 retrieval plus OpenRouter `voyageai/rerank-3`, topic clustering and coverage preview | Hybrid sparse+dense retrieval |
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
                    Retriever → reranker / selection
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
- An OpenRouter key for `search`, topic-filtered `mock`/`coverage`, and
  `eval rerank --execute`

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
uv run overfit mock --course IFN580
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
| `search QUERY` | Dense Top 40 → OpenRouter rerank → up to 10; prints both vector and rerank ranks/scores | embedding + reranker |
| `mock` | Generates auditable `mcq-v1` A–D exams in batches of at most 10; `--judge` explicitly adds independent MCQ review | generation; `--topic` also embeds and reranks; Judge only with `--judge` |
| `eval replay RUN_DIR` | Revalidates a local trace and writes a separate replay report; no settings, index or provider access | none |
| `eval rerank --course COURSE --execute` | Runs the explicit 47-question, two-track dense Top-40 → rerank Top-10 evaluation; supports `--no-cache` and explicit `--resume` | embedding + real reranker; no generator or judge |
| `eval generator-challenge SUITE_DIR` | Prepares a controlled Generator fixture offline; `--execute` requires an explicit hard provider-call budget | none in prepare; generation only in execute |
| `topics` | Clusters stored vectors and ranks topics by distinct source-file coverage | none |
| `coverage` | Shows the passages `mock` would receive; optional topic focus uses dense Top 40, reranking and diversity constraints | none without a topic; `--topic` embeds and reranks; no LLM |
| `status` | Shows index profile, counts and sources | none |
| `embed-check` | Prints vector dimension and similarity probes, including Chinese-to-English retrieval | embedding |

Current command signatures (defaults in parentheses):

```text
overfit inspect     --course/-c COURSE [--path/-p PATH] [--samples 2]
                    [--chars 600] [--show-removed]
overfit chunks      --course/-c COURSE [--path/-p PATH] [--show 5]
                    [--spanning]
overfit ingest      --course/-c COURSE [--path/-p PATH] [--force] [--rebuild]
overfit search      QUERY --course/-c COURSE [--top-k/-k 10] [--chars 300]
overfit mock        --course/-c COURSE [--questions/-q 30] [--topic TEXT]
                    [--material 0] [--trace] [--judge]
overfit eval replay RUN_DIR
overfit eval rerank --course/-c COURSE [--dataset PATH] --execute
                    [--no-cache] [--resume RUN_DIR] [--rerank-* overrides]
overfit eval generator-challenge SUITE_DIR [--execute --max-calls N]
                    [--max-attempts 2] [--max-items N] [--output-dir PATH]
overfit topics      --course/-c COURSE [--count/-n 12] [--questions 10]
overfit coverage    --course/-c COURSE [--count/-n N] [--topic TEXT]
                    [--chars 180]
overfit status      --course/-c COURSE
overfit embed-check
```

Running `overfit` without a subcommand currently prints `hello overfit`;
running it with `--help` lists the commands above.

Opening an existing index reads its persisted profile, including vector width,
without contacting the embedding endpoint. `status`, `topics`, whole-course
`coverage`, and whole-course `mock` therefore use only stored vectors. Creating
an index still probes the configured embedding model for its real width;
query/topic paths embed their query as usual.

`eval rerank` is a live, paid path: it does nothing unless `--execute` is
present. To force fresh provider calls, run:

```bash
uv run overfit eval rerank \
  --course IFN580_machine_learning \
  --execute \
  --no-cache
```

If a run ends incomplete, inspect its report and resume only by explicit
request:

```bash
uv run overfit eval rerank \
  --course IFN580_machine_learning \
  --execute \
  --resume /absolute/path/to/outputs/eval/rerank-RUN_ID
```

This evaluation calls the configured embedding and reranker services only; it
does not call the Generator or judge.

`eval generator-challenge` is prepare-only unless `--execute` is supplied. Prepare
validates and snapshots the complete controlled suite without loading runtime
settings, keys, or providers:

```bash
uv run overfit eval generator-challenge \
  eval/generator_challenges/ifn580_mcq_v1
```

Live execution additionally requires a positive hard call budget. Every provider
dispatch counts, including a structured-output fallback; once exhausted, the next
dispatch is rejected before it is sent:

```bash
uv run overfit eval generator-challenge \
  eval/generator_challenges/ifn580_mcq_v1 \
  --execute \
  --max-calls 20 \
  --max-attempts 2
```

The historical `eval/generator_challenges/ifn580_v1` short-answer suite remains
unchanged and prepare-readable as baseline evidence; it is read-only and cannot
be executed by the MCQ Generator challenge runner.

The fixture sends only its ordered chunks and request fields to the Generator;
annotations stay local. It does not open the production index and does not call
retrieval or the reranker. Judge evaluation of retained traces remains a separate,
explicit command. See the [Generator/Judge challenge evidence index](docs/evaluation/GENERATOR-JUDGE-CHALLENGE.md).

### Mock selection and output

Without `--topic`, `mock` clusters stored vectors into at most 12 topics,
allocates passages by topic weight, and chooses representative, non-duplicate
chunks. With `--topic`, it retrieves dense Top 40, removes material shorter
than 200 characters, reranks every remaining candidate, then scans by rerank
priority while removing exact/near duplicates and allowing at most two chunks
per source file. Topic mode uses 10 passages by default and requires an explicit
`--questions`; without a topic the material budget remains twice the question
count. Selection may return fewer passages rather than refill with low-quality
material. Override with `--material`.

Requests are planned in batches of at most 10 questions. Each batch has a hard
ceiling of three provider dispatches. Complete batches are checkpointed; a failed
batch stops later batches and no incomplete exam is published. After all batches,
clear cross-batch duplicates are removed without top-up, whole option objects are
reordered to balance A–D correct labels, and questions are deterministically mixed.
The 2:1 and 3:1 material ratios remain evaluation tracks; no live comparison has
yet frozen a winner.

The provider is asked for a portable JSON-object envelope,
`{"questions": [...]}`; the adapter normalizes that wire shape to the internal
`GeneratedMCQExam.items` model after mechanical page/label/prefix normalization.
The model may honestly return fewer questions. Each accepted question has exact
A–D option objects; every option includes `text`, `explanation`, `source`, `page`,
and `page_end`, plus one `correct_option` at item level:

```json
{
  "topic": "Bias and variance",
  "question": "Which statement is supported?",
  "options": {
    "A": {"text": "...", "explanation": "...", "source": "week03/lecture.pdf", "page": 12, "page_end": null},
    "B": {"text": "...", "explanation": "...", "source": "week04/tutorial.pdf", "page": 3, "page_end": null},
    "C": {"text": "...", "explanation": "...", "source": "week03/lecture.pdf", "page": 13, "page_end": null},
    "D": {"text": "...", "explanation": "...", "source": "week05/practical.pdf", "page": 7, "page_end": null}
  },
  "correct_option": "A"
}
```

The paper hides topics, explanations, and citations. The answer key retains the
topic, identifies the correct letter/text, and shows feedback plus a citation for
all four options. Any invalid option citation drops the whole item; semantic fields
and nearby pages are never repaired. Ordinary `mock` explicitly reports semantic
uniqueness and distractor correctness as **not judged**. `mock --judge` is the
additional authorization boundary for the independent finite MCQ Judge.

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
| `OVERFIT_RERANK_PROVIDER` | `openrouter` | First-version reranker adapter |
| `OVERFIT_RERANK_BASE_URL` | `https://openrouter.ai/api/v1` | Rerank API base URL |
| `OVERFIT_RERANK_API_KEY` | *(none)* | Required only for commands with a query |
| `OVERFIT_RERANK_MODEL` | `voyageai/rerank-3` | OpenRouter rerank model slug |
| `OVERFIT_RERANK_TIMEOUT` | `30` | Per-attempt rerank timeout in seconds |
| `LLM_BASE_URL` | `http://localhost:11434/v1` | Chat-completions API |
| `LLM_API_KEY` | `ollama` | Generation API credential |
| `LLM_MODEL` | `qwen3.6:27b` | Generation model identifier |
| `TEMPERATURE` | `0.3` | Generation sampling temperature |
| `MAX_RETRIES` | `3` | Validation retries; total attempts are normally 4 |
| `REQUEST_TIMEOUT` | `900` | Generation request timeout in seconds |

`TOP_K=10` and `NUM_QUESTIONS=30` exist in the settings model, but current CLI
defaults are hard-coded to the same values; use `search --top-k` and
`mock --questions` to override them reliably.

## Generation trace and offline replay

Every new `mock` records an auditable `generation_contract=mcq-v1` run under
`outputs_dir/eval/<run_id>/`; `--trace` additionally prints that directory and
leaves course-level output files untouched. Ordinary `mock` copies the completed
paper/answer artifacts to their normal course-level paths. Historical short-answer
traces remain readable and replayable without synthetic options.

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
[challenge spec](docs/specs/EVAL-CHALLENGE-SPEC.md)
and [plan](docs/plans/EVAL-CHALLENGE-PLAN.md).
The [IFN580 suite](eval/judge_challenges/ifn580_v1/README.md)
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
of 90 baseline files. The offline prepare report (local artifact: `outputs/eval/challenges/47c16ac8-faed-450b-ae21-6acf7365272e/report.md`)
is historical evidence containing seven cases, 0/14 evaluated phases and zero
calls. A later authorized challenge-v2 / judge-v4 run (local artifact: `outputs/eval/challenges/87378834-46cb-42dd-8e1e-e558cc95353e/report.md`)
completed 14/14 phases with 14/14 responses; this small controlled run is not a
semantic calibration. Formal v3 replay compatibility was independently checked
without changing old records.

## Development and verification

```bash
uv sync  # the default dev group includes pytest and Ruff
uv run pytest -q
uv run ruff check .
```

当前分支的提交前全量回归为 **743 passed**；Ruff 和 `git diff --check`
同时通过。下列较小数字均是对应历史阶段当时保留的验收证据，并非当前测试总数。

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
were made; calibration remains incomplete. Evidence is maintained in [EVAL-JUDGE-PLAN.md](docs/plans/EVAL-JUDGE-PLAN.md);
the earlier [PR 1 record](docs/plans/EVAL-TRACE-PLAN.md) remains historical evidence.

## Documentation

| Document | Purpose |
| --- | --- |
| [ARCHITECTURE.md](./docs/ARCHITECTURE.md) | Current system and stage diagrams |
| [PIPELINE.md](./docs/PIPELINE.md) | Exact layer behavior, algorithms and data flow |
| [TECH-STACK.md](./docs/TECH-STACK.md) | Dependencies, optional components and deliberate exclusions |
| [MODELS.md](./docs/MODELS.md) | Embedding/generation contracts, safeguards and configuration |
| [EVALUATION.md](docs/EVALUATION.md) | Trace and judge workflows; pilot findings, judge-v3 responsibility revision and pending calibration; existing 47-item gold and later diagnostics |
| [EVAL-TRACE-SPEC.md](docs/specs/EVAL-TRACE-SPEC.md) | PR 1 contract: opt-in generation traces, strict citation checks, offline replay and acceptance criteria |
| [EVAL-TRACE-PLAN.md](docs/plans/EVAL-TRACE-PLAN.md) | PR 1 implementation milestones, offline test matrix and verification evidence |
| [EVAL-JUDGE-SPEC.md](docs/specs/EVAL-JUDGE-SPEC.md) | PR 2 contract: separate judge configuration, evidence, budgets and fixed-denominator metrics |
| [EVAL-CHALLENGE-SPEC.md](docs/specs/EVAL-CHALLENGE-SPEC.md) | Separate control/mutant execution contract and information isolation |
| [EVAL-CHALLENGE-PLAN.md](docs/plans/EVAL-CHALLENGE-PLAN.md) | Challenge implementation, offline acceptance and preservation evidence |
| [EVAL-GENERATOR-CHALLENGE-SPEC.md](docs/specs/EVAL-GENERATOR-CHALLENGE-SPEC.md) | Controlled Generator fixture, isolation, hard-budget and trace-interoperability contract |
| [EVAL-GENERATOR-CHALLENGE-PLAN.md](docs/plans/EVAL-GENERATOR-CHALLENGE-PLAN.md) | Offline implementation, authorized live run, replay and Judge follow-up evidence |
| [EVAL-JUDGE-PLAN.md](docs/plans/EVAL-JUDGE-PLAN.md) | PR 2 milestones, offline test evidence and real-pilot boundary |
| [RETRIEVAL-RERANK-SPEC.md](docs/specs/RETRIEVAL-RERANK-SPEC.md) | Frozen query-triggered reranker behavior, logging and evaluation contract |
| [RETRIEVAL-RERANK-PLAN.md](docs/plans/RETRIEVAL-RERANK-PLAN.md) | OpenRouter implementation and offline/live verification stages |
| [RERANK-EVALUATION.md](docs/evaluation/RERANK-EVALUATION.md) | Index of the formal rerank evaluation JSON and human-readable report |
| [GENERATOR-JUDGE-CHALLENGE.md](docs/evaluation/GENERATOR-JUDGE-CHALLENGE.md) | Compact index of controlled Generator and Judge challenge artifacts and findings |
| [MOCK-MCQ-SPEC.md](docs/specs/MOCK-MCQ-SPEC.md) | Frozen MCQ v1 product, schema, batching, feedback, Judge and compatibility contract |
| [MOCK-MCQ-PLAN.md](docs/plans/MOCK-MCQ-PLAN.md) | Planned TDD milestones, migration, offline matrix and user-authorized live gates for MCQ v1 |
| [MOCK-MCQ-LIVE-GATES.md](docs/evaluation/MOCK-MCQ-LIVE-GATES.md) | Append-only record of authorized MCQ live gates, replay identities and remaining gates |
