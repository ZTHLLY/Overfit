# Mock MCQ live-gate record

This page is the compact, append-only index for user-authorized `mcq-v1` live
gates. It records identities and bounded outcomes without copying credentials,
provider payloads, ignored output bundles, or machine-local paths into tracked
documentation. The frozen contract and gate order remain in
[MOCK-MCQ-SPEC.md](../specs/MOCK-MCQ-SPEC.md) and
[MOCK-MCQ-PLAN.md](../plans/MOCK-MCQ-PLAN.md).

## Gate status

| Gate | Status | Result |
| --- | --- | --- |
| A — four controlled cases | Complete | 4/4 cases terminal; 4/8 provider calls used; retained `0/1/2/3` |
| B — five real MCQs | Not started | Awaiting explicit user initiation |
| C — 2:1 versus 3:1 | Not started | No material ratio selected |
| D — 30-question mock | Exploratory run recorded; formal gate incomplete | User authorized an out-of-order 30-question run; generation completed, but Gates B–C remain outstanding |
| E — explicit MCQ Judge | Attempted; incomplete | 29/30 items evaluated; one technical truncation prevented judged publication |

## 2026-11-04 — Gate A

The configured Generator model was `deepseek-v4-flash`. The controlled suite
isolated Generator behavior from retrieval, embedding, reranking, and Judge.
Execution used this explicit bounded command:

```console
.venv/bin/overfit eval generator-challenge \
  eval/generator_challenges/ifn580_mcq_v1 \
  --execute --max-calls 8 --max-attempts 2
```

Parent challenge run ID: `d03d14d4-5b1e-4297-8982-88196cb0697a`.

| Case | Status | Calls | Kept | Latency | Tokens | Child run ID | Replay ID |
| --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| `unrelated-only` | `empty` | 1 | 0 | 2.62 s | 1,356 | `e6a4895b-5fd5-4a4f-b986-8183682edc85` | `732960ee-ee62-4cbc-a1a9-1de8332611f9` |
| `partial-context` | `completed` | 1 | 1 | 9.50 s | 2,670 | `6667c34d-c56f-49ce-b9b1-6f62ab1c8c3d` | `d3d0f4ad-c7d0-40c1-bc17-47a5da1def80` |
| `gold-hard-negative` | `completed` | 1 | 2 | 15.21 s | 3,580 | `4e5003fe-d141-4236-98cd-1e97f5cd6307` | `264cb3b1-aa5e-4d6f-b669-9fd3481ff86f` |
| `multi-source` | `completed` | 1 | 3 | 9.82 s | 3,137 | `726e0498-d16a-44d8-8751-29b8e2cbd3f0` | `dfef2061-aec2-44c3-8ef7-c74e85edb715` |

All four child replays reported complete integrity. The unrelated-only case
returned no question, the partial-context case returned fewer than requested,
and the two supported cases retained their requested counts. There were no
provider errors, validation retries, citation drops, or format fallbacks. The
four calls took 37.14 seconds in total and recorded 4,541 prompt tokens plus
6,202 completion tokens, or 10,743 tokens overall.

An independent item-level semantic audit accepted Gate A: all six retained
questions had a single supported answer, accurate option feedback and citations,
and no negative stem, calculation, or all/none construction. The fabricated
hard-negative did not displace the trusted course source. Non-blocking
observations were that some distractors were easy, a few explanations were
repetitive, and correct letters were D-heavy across child exams even though
balancing was valid within each exam. Monetary cost was not available from the
recorded metadata. This controlled result is not general semantic calibration,
and no later gate was started automatically.

## 2026-11-04 — out-of-order full simulation

The user explicitly authorized one full-course simulation before Gates B and C.
This is recorded as exploratory evidence, not as formal completion of Gate D or
E and not as a 2:1 material-ratio decision. The command used the no-topic
whole-course path, so reranking was correctly skipped; the read-only index path
also made no embedding request.

```console
.venv/bin/overfit mock \
  --course IFN580_machine_learning \
  --questions 30 --trace --judge
```

### Generator result

Generator run ID: `e7b0d033-b90e-4399-a8ab-0232635410e2`. Replay ID:
`e3904cdd-4c42-41e5-ac67-c3d9ba59022f`.

- Model: `deepseek-v4-flash`.
- Three batches each received 20 frozen chunks and requested 10 questions.
- All three calls completed once, retaining `30/30`; there were no validation
  errors, citation drops, format fallbacks, or cross-batch duplicate removals.
- Correct letters were balanced `A=7, B=7, C=8, D=8`, with 30 distinct
  model-provided topic labels.
- Calls took 157.47 seconds and recorded 14,480 prompt tokens plus 30,342
  completion tokens, or 44,822 total.
- Offline replay reconstructed the complete generation record.

### Judge result

Judgment ID: `fe7e49c6-63df-498b-82b0-af8bd07a4e2b`. Replay ID:
`f0121e2f-6bcc-4dcb-89c2-33147031e26b`.

- Model: `qwen/qwen3.7-plus`.
- The Judge used 31 calls from a ceiling of 60. One schema-invalid response was
  retried successfully.
- It evaluated 29/30 items: 22 were model-positive on all mandatory dimensions,
  six were left for human review (items 4, 7, 14, 15, 17, and 27), and item 19
  was rejected automatically.
- Item 3 reached `finish_reason=length` at its output limit and returned no
  usable content. The current implementation did not spend its remaining
  attempt, so the Judge run correctly finished `incomplete` and produced no
  formal judged publication.
- Calls took 1,019.87 seconds and recorded 196,888 prompt tokens plus 60,252
  completion tokens, or 257,140 total. Judge replay reproduced the same
  incomplete state.

The complete simulation recorded 301,962 model tokens. Monetary cost was not
available from the artifacts.

### Independent item audit

An independent item-level audit against the exact per-item supplied materials
classified the 30 generated questions as 19 pass, nine pending, and two fail.
Items 17 and 24 should not be published: item 17 treated an unanswered quiz as
an answer key, while item 24 committed to one side of contradictory extracted
table/prose evidence. The pending set primarily reflected distractors not
clearly derived from the supplied material or explanations whose citations did
not fully support their extra rebuttal claims.

The audit also found Judge calibration signals in both directions. It correctly
identified several weak option-level citations, but sometimes required a source
to contain the deliberately false distractor itself even though `mcq-v1`
requires the citation to support the corrective explanation. Conversely, it
missed the contradictory extraction behind item 24. The run therefore provides
useful failure evidence, not a broad quality acceptance claim. Gates B and C
remain not started, no ratio winner is frozen, and Gates D and E remain formally
incomplete.
